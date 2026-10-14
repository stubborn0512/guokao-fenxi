#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取中公湖北省税务局 2021—2025 国考职位级报名/过审数据。

数据纪律：
1. 只访问公开页面，低频串行请求；遇到 403/429/验证页面即停止该年度，不绕过限制。
2. 只有列表表头明确为“过审人数/合格人数”，或岗位详情页在职位代码对应的局部区域明确出现过审字段，才填写逐岗位过审人数。
3. 2025 列表中的“报考人数”只写入报名字段；绝不当作过审人数。
4. 年度汇总过审总数与逐岗位人数分开保存，不把年度总数分摊到岗位。
5. 每条记录保存源 URL、抓取时间、原始行文本与字段证据。

Windows:
  py -m pip install -r requirements-offcn.txt
  py scripts/offcn_hubei_tax_scraper.py --out offcn_hubei_tax_data
"""
from __future__ import annotations
import argparse, csv, json, logging, math, re, sys, time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SEEDS = {
    2021: "https://zhiwei.offcn.com/gj/2021/bmall7905.html",
    2022: "https://zhiwei.offcn.com/gj/2022/bmall9423_1.html",
    2023: "https://zhiwei.offcn.com/gj/2023/bmall9217.html",
    2024: "https://zhiwei.offcn.com/gj/2024/bmall10893.html",
    2025: "https://zhiwei.offcn.com/gj/2025/bmall11865_2.html",
}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
CODE_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
BMALL_RE = re.compile(r"(bmall\d+)(?:_(\d+))?\.html$", re.I)
APPROVED_LABEL = r"(?:过审人数|资格审查通过人数|审核通过人数|合格人数)"
APPLICANT_LABEL = r"(?:报考人数|报名人数|报名总人数)"
ALIASES = {
    "dept_code": ["部门代码"], "unit": ["用人司局", "招录机关", "招录单位"],
    "position_name": ["职位名称", "招考职位"], "position_code": ["职位代码"],
    "recruit_count": ["招考人数", "招录人数", "计划招录人数"],
    "approved_count": ["过审人数", "资格审查通过人数", "审核通过人数", "合格人数"],
    "applicant_count": ["报考人数", "报名人数", "报名总人数"],
    "major_requirement": ["专业"], "education": ["学历"], "location": ["工作地点", "工作地"],
}

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def norm(value):
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ").replace("\u3000", " ")).strip()

def to_int(value):
    s = str(value or "").replace(",", "").replace("，", "").strip()
    return int(s) if re.fullmatch(r"\d+", s) else ""

def setup_logging(outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(outdir / "scrape_log.txt", encoding="utf-8"), logging.StreamHandler(sys.stdout)], force=True)

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6", "Referer": "https://zhiwei.offcn.com/"})
    retry = Retry(total=2, connect=2, read=1, status=1, backoff_factor=1.2,
        status_forcelist=(500, 502, 503, 504), allowed_methods=frozenset(["GET"]), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter); session.mount("http://", adapter)
    return session

def fetch(session, url, delay, raw_dir, tag):
    time.sleep(max(0.0, delay))
    try:
        response = session.get(url, timeout=(12, 30))
    except requests.RequestException as exc:
        logging.warning("请求失败 %s | %s", url, exc)
        return None, "request_error", str(exc)
    if response.status_code in (403, 429):
        msg = f"HTTP {response.status_code}; 按访问限制停止，不进行规避"
        logging.warning("%s: %s", msg, url)
        return None, f"http_{response.status_code}", msg
    if response.status_code != 200:
        return None, f"http_{response.status_code}", f"HTTP {response.status_code}"
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    title = norm(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if any(k in title for k in ("访问过于频繁", "安全验证", "验证码")):
        return None, "access_challenge", f"可能存在访问验证：{title}"
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        safe_path = re.sub(r"[^A-Za-z0-9_-]+", "_", urlparse(url).path.strip("/"))
        (raw_dir / f"{tag}_{safe_path}.html").write_text(html, encoding="utf-8")
    return html, "ok", ""

def bmall_stem(url):
    match = BMALL_RE.search(urlparse(url).path.rsplit("/", 1)[-1])
    return match.group(1) if match else None

def same_list_page(url, year, stem):
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and parts.netloc.lower() in ("zhiwei.offcn.com", "www.zhiwei.offcn.com") and f"/gj/{year}/" in parts.path and bmall_stem(url) == stem

def clean_detail_url(href, current_url):
    if not href or href.strip().lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    url = urljoin(current_url, href).split("#", 1)[0]
    parts = urlparse(url); host = parts.netloc.lower().split(":", 1)[0]
    if parts.scheme not in ("http", "https") or (host != "offcn.com" and not host.endswith(".offcn.com")):
        return None
    return url

def discover_pages(html, current_url, year, stem):
    soup = BeautifulSoup(html, "html.parser"); output = set()
    for anchor in soup.find_all("a", href=True):
        url = urljoin(current_url, anchor["href"]).split("#", 1)[0]
        if same_list_page(url, year, stem): output.add(url)
    return output

def header_index(header, aliases):
    compact = [norm(cell).replace(" ", "") for cell in header]
    for alias in aliases:
        alias = norm(alias).replace(" ", "")
        for index, text in enumerate(compact):
            if text == alias: return index
    for alias in aliases:
        alias = norm(alias).replace(" ", "")
        for index, text in enumerate(compact):
            if alias and alias in text: return index
    return None

def parse_summary(text):
    compact = norm(text)
    patterns = {
        "site_position_count": r"职位个数\s*[:：]?\s*([\d,，]+)",
        "site_recruit_count": r"(?:招录人数|招考人数)\s*[:：]?\s*([\d,，]+)",
        "site_applicant_count": r"(?:报名人数|报考人数)\s*[:：]?\s*([\d,，]+)",
        "site_approved_count": APPROVED_LABEL + r"\s*[:：]?\s*([\d,，]+)",
    }
    result = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, compact); result[key] = to_int(match.group(1)) if match else ""
    return result

def parse_list_page(html, url, year):
    soup = BeautifulSoup(html, "html.parser")
    summary = parse_summary(norm(soup.get_text(" ", strip=True)))
    for table in soup.find_all("table"):
        trs = table.find_all("tr"); header_row_idx = None; header = []
        for idx, tr in enumerate(trs[:10]):
            labels = [norm(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"], recursive=False)]
            joined = "|".join(labels)
            if "职位代码" in joined and ("用人司局" in joined or "招录机关" in joined) and ("招考人数" in joined or "招录人数" in joined):
                header_row_idx, header = idx, labels; break
        if header_row_idx is None: continue
        indexes = {key: header_index(header, aliases) for key, aliases in ALIASES.items()}
        records, detail_links = [], []
        for tr in trs[header_row_idx + 1:]:
            cells = tr.find_all(["th", "td"], recursive=False)
            values = [norm(cell.get_text(" ", strip=True)) for cell in cells]
            row_text = norm(tr.get_text(" ", strip=True)); match = CODE_RE.search(" ".join(values))
            if not match: continue
            idx_code = indexes["position_code"]
            code_cell = values[idx_code] if idx_code is not None and idx_code < len(values) else ""
            code = code_cell if CODE_RE.fullmatch(code_cell) else match.group(0)
            def cell(field):
                idx = indexes[field]
                return values[idx] if idx is not None and idx < len(values) else ""
            row = {
                "year": year, "position_code": code, "dept_code": cell("dept_code"), "unit": cell("unit"),
                "position_name": cell("position_name"), "recruit_count": to_int(cell("recruit_count")),
                "approved_count_list": to_int(cell("approved_count")), "applicant_count_list": to_int(cell("applicant_count")),
                "list_metric_header": header[indexes["approved_count"]] if indexes["approved_count"] is not None else (
                    header[indexes["applicant_count"]] if indexes["applicant_count"] is not None else ""),
                "major_requirement": cell("major_requirement"), "education": cell("education"), "location": cell("location"),
                "source_url": url, "fetched_at": now_iso(), "raw_row_text": row_text,
                "raw_cells_json": json.dumps(values, ensure_ascii=False), "detail_applicant_count": "",
                "detail_approved_count": "", "detail_source_url": "", "detail_match_status": "not_crawled",
                "detail_approved_evidence": "", "detail_applicant_evidence": "",
            }
            records.append(row)
            # Skip detail pages when the list already has verified per-position approval counts.
            if row["approved_count_list"] != "":
                continue
            for anchor in tr.find_all("a", href=True):
                label = norm(anchor.get_text(" ", strip=True))
                if not any(k in label for k in ("报名大数据", "职位详情", "历年情况")): continue
                detail_url = clean_detail_url(anchor.get("href", ""), url)
                if detail_url:
                    detail_links.append({"year": year, "position_code": code, "unit": row["unit"],
                        "position_name": row["position_name"], "href": detail_url, "link_label": label, "source_list_url": url})
        return records, detail_links, summary, "ok" if records else "empty_position_rows"
    return [], [], summary, "no_position_table"

def parse_count(text, label_pattern):
    match = re.search(label_pattern + r"\s*[:：]?\s*([\d,，]+)", text)
    return (to_int(match.group(1)), match.group(0)) if match else ("", "")

def parse_detail_page(html, url, expected):
    """只读职位代码对应的小范围节点；不能从整页取第一个数字，避免误用年度汇总数。"""
    soup = BeautifulSoup(html, "html.parser"); code = expected["position_code"]
    if code not in soup.get_text(" ", strip=True):
        return {"detail_match_status": "position_code_not_found", "detail_source_url": url,
            "detail_approved_count": "", "detail_applicant_count": "", "detail_approved_evidence": "", "detail_applicant_evidence": ""}
    # 优先：职位代码与计数字段位于同一表格行，且该行没有其它职位代码。
    for table in soup.find_all("table"):
        trs = table.find_all("tr"); header = []
        for idx, tr in enumerate(trs[:10]):
            labels = [norm(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"], recursive=False)]
            if "职位代码" not in "|".join(labels): continue
            header = labels
            for row_tr in trs[idx + 1:]:
                cells = [norm(c.get_text(" ", strip=True)) for c in row_tr.find_all(["th", "td"], recursive=False)]
                row_text = norm(row_tr.get_text(" ", strip=True))
                if code not in row_text or set(CODE_RE.findall(row_text)) - {code}: continue
                approved = applicant = ""; approved_ev = applicant_ev = ""
                ai = header_index(header, ALIASES["approved_count"]); pi = header_index(header, ALIASES["applicant_count"])
                if ai is not None and ai < len(cells):
                    approved = to_int(cells[ai]); approved_ev = f"同一表格行；列头={header[ai]}；原始行={row_text}"
                if pi is not None and pi < len(cells):
                    applicant = to_int(cells[pi]); applicant_ev = f"同一表格行；列头={header[pi]}；原始行={row_text}"
                if approved != "" or applicant != "":
                    return {"detail_match_status": "position_code_verified_table_row", "detail_source_url": url,
                        "detail_approved_count": approved, "detail_applicant_count": applicant,
                        "detail_approved_evidence": approved_ev, "detail_applicant_evidence": applicant_ev}
            break
    # 次选：包含目标代码、明确计数字段、且不含其它职位代码的最小 HTML 节点。
    candidates = []
    for tag in soup.find_all(True):
        text = norm(tag.get_text(" ", strip=True))
        if code not in text or len(text) > 2500 or set(CODE_RE.findall(text)) != {code}: continue
        if re.search(APPROVED_LABEL + "|" + APPLICANT_LABEL, text): candidates.append(text)
    if not candidates:
        return {"detail_match_status": "position_code_found_no_local_count", "detail_source_url": url,
            "detail_approved_count": "", "detail_applicant_count": "", "detail_approved_evidence": "", "detail_applicant_evidence": ""}
    context = min(candidates, key=len)
    approved, approved_ev = parse_count(context, APPROVED_LABEL)
    applicant, applicant_ev = parse_count(context, APPLICANT_LABEL)
    return {"detail_match_status": "position_code_verified_local_context", "detail_source_url": url,
        "detail_approved_count": approved, "detail_applicant_count": applicant,
        "detail_approved_evidence": approved_ev or context, "detail_applicant_evidence": applicant_ev or context}

def dedupe_positions(records):
    by_key, conflicts = {}, []
    for row in records:
        key = (row["year"], row["position_code"])
        if key not in by_key: by_key[key] = row; continue
        first = by_key[key]
        compare_fields = ("unit", "position_name", "recruit_count", "approved_count_list", "applicant_count_list")
        if any(first.get(f) != row.get(f) for f in compare_fields):
            conflicts.append({"year": row["year"], "position_code": row["position_code"],
                "first_source_url": first["source_url"], "conflicting_source_url": row["source_url"],
                "first_row": first["raw_row_text"], "conflicting_row": row["raw_row_text"]})
    return list(by_key.values()), conflicts

def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows: path.write_text("", encoding="utf-8-sig"); return
    headers = list(rows[0].keys())
    for row in rows:
        for key in row:
            if key not in headers: headers.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=headers, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)

def crawl_year(session, year, outdir, delay, max_pages, save_html):
    seed = SEEDS[year]; stem = bmall_stem(seed)
    raw_dir = outdir / "raw_html" / str(year) if save_html else None
    queue = deque([seed]); visited = set(); all_rows = []; all_details = []; page_audit = []
    year_summary = {}; stop_status = ""
    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited or not same_list_page(url, year, stem): continue
        visited.add(url)
        html, status, error = fetch(session, url, delay, raw_dir, "list")
        audit = {"year": year, "url": url, "status": status, "error": error, "fetched_at": now_iso(), "position_rows": 0}
        page_audit.append(audit)
        if not html:
            if status in ("http_403", "http_429", "access_challenge"):
                stop_status = status; logging.warning("%s 年出现访问限制，停止该年度。", year); break
            continue
        rows, links, summary, parse_status = parse_list_page(html, url, year)
        audit.update({"parse_status": parse_status, "position_rows": len(rows)})
        all_rows.extend(rows); all_details.extend(links)
        for key, value in summary.items():
            if value != "" and key not in year_summary: year_summary[key] = value
        for page_url in discover_pages(html, url, year, stem):
            if page_url not in visited and page_url not in queue: queue.append(page_url)
        # 若分页链接没有覆盖全部页码，用当前页实际岗位行数估算范围；额外空页会被审计，不会填0。
        if rows and year_summary.get("site_position_count"):
            est = min(max_pages, math.ceil(year_summary["site_position_count"] / max(1, len(rows))) + 2)
            base = f"https://zhiwei.offcn.com/gj/{year}/{stem}"
            candidates = [base + ".html"] + [base + f"_{i}.html" for i in range(1, est + 1)]
            for candidate in candidates:
                if candidate not in visited and candidate not in queue: queue.append(candidate)
        logging.info("[%s] %s rows=%d queue=%d", year, url, len(rows), len(queue))
    rows, conflicts = dedupe_positions(all_rows)
    unique_details = {}
    for item in all_details: unique_details[(item["year"], item["position_code"], item["href"])] = item
    report = {
        "year": year, "seed_url": seed, "expected_positions": year_summary.get("site_position_count", ""),
        "expected_recruit_count": year_summary.get("site_recruit_count", ""),
        "site_total_applicant_count": year_summary.get("site_applicant_count", ""),
        "site_total_approved_count": year_summary.get("site_approved_count", ""),
        "pages_attempted": len(visited), "pages_ok": sum(p["status"] == "ok" for p in page_audit),
        "positions_unique": len(rows), "rows_with_position_level_approved_count": sum(r["approved_count_list"] != "" for r in rows),
        "rows_with_position_level_applicant_count": sum(r["applicant_count_list"] != "" for r in rows),
        "detail_links_unique": len(unique_details), "status": "blocked_or_challenged" if stop_status else "finished_or_page_limit",
        "row_conflicts": len(conflicts),
    }
    return rows, list(unique_details.values()), page_audit, report, conflicts

def main():
    parser = argparse.ArgumentParser(description="中公湖北税务国考岗位报名/过审数据抓取")
    parser.add_argument("--out", default="./offcn_hubei_tax_data")
    parser.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数，默认1秒")
    parser.add_argument("--max-pages-per-year", type=int, default=120)
    parser.add_argument("--years", nargs="*", type=int, choices=sorted(SEEDS), default=sorted(SEEDS))
    parser.add_argument("--skip-details", action="store_true", help="只抓列表页")
    parser.add_argument("--no-raw-html", action="store_true", help="不保存原始HTML")
    args = parser.parse_args()
    outdir = Path(args.out).resolve(); setup_logging(outdir); session = make_session()
    save_html = not args.no_raw_html
    all_positions = []; all_detail_links = []; all_page_audits = []; annual_reports = []; all_conflicts = []
    for year in args.years:
        logging.info("开始抓取 %s：%s", year, SEEDS[year])
        rows, links, audits, report, conflicts = crawl_year(session, year, outdir, args.delay, args.max_pages_per_year, save_html)
        all_positions.extend(rows); all_detail_links.extend(links); all_page_audits.extend(audits); annual_reports.append(report); all_conflicts.extend(conflicts)
        logging.info("年度报告：%s", json.dumps(report, ensure_ascii=False))
    detail_audit = []
    if not args.skip_details:
        unique = {}
        for link in all_detail_links: unique[(link["year"], link["position_code"], link["href"])] = link
        for index, link in enumerate(unique.values(), 1):
            html, status, error = fetch(session, link["href"], args.delay,
                outdir / "raw_html" / "detail" if save_html else None, "detail")
            audit = {**link, "status": status, "error": error, "fetched_at": now_iso()}
            if not html:
                audit["detail_match_status"] = status; detail_audit.append(audit)
                if status in ("http_403", "http_429", "access_challenge"):
                    logging.warning("详情页出现访问限制，停止后续详情页抓取。"); break
                continue
            parsed = parse_detail_page(html, link["href"], link); audit.update(parsed); detail_audit.append(audit)
            if parsed.get("detail_match_status") in ("position_code_verified_table_row", "position_code_verified_local_context"):
                for row in all_positions:
                    if row["year"] != link["year"] or row["position_code"] != link["position_code"]: continue
                    if row["applicant_count_list"] == "" and parsed.get("detail_applicant_count", "") != "":
                        row["detail_applicant_count"] = parsed["detail_applicant_count"]; row["detail_applicant_evidence"] = parsed.get("detail_applicant_evidence", "")
                    if row["approved_count_list"] == "" and parsed.get("detail_approved_count", "") != "":
                        row["detail_approved_count"] = parsed["detail_approved_count"]; row["detail_approved_evidence"] = parsed.get("detail_approved_evidence", "")
                    row["detail_source_url"] = link["href"]; row["detail_match_status"] = parsed["detail_match_status"]
            if index % 50 == 0: logging.info("详情页进度 %d/%d", index, len(unique))
    for row in all_positions:
        if row.get("approved_count_list", "") != "":
            row["approved_count_final"] = row["approved_count_list"]; row["approved_count_source"] = "列表页明确过审/合格人数列"
        elif row.get("detail_approved_count", "") != "":
            row["approved_count_final"] = row["detail_approved_count"]; row["approved_count_source"] = "职位代码核验后的详情页局部字段"
        else:
            row["approved_count_final"] = ""; row["approved_count_source"] = "未取得可核验的逐岗位过审人数；不得用年度汇总或报考人数替代"
        if row.get("applicant_count_list", "") != "":
            row["applicant_count_final"] = row["applicant_count_list"]; row["applicant_count_source"] = "列表页明确报名/报考人数列"
        elif row.get("detail_applicant_count", "") != "":
            row["applicant_count_final"] = row["detail_applicant_count"]; row["applicant_count_source"] = "职位代码核验后的详情页局部字段"
        else:
            row["applicant_count_final"] = ""; row["applicant_count_source"] = "未取得可核验的逐岗位报名人数"
    write_csv(outdir / "offcn_hubei_tax_positions_2021_2025.csv", all_positions)
    write_csv(outdir / "offcn_hubei_tax_pages_audit.csv", all_page_audits)
    write_csv(outdir / "offcn_hubei_tax_details_audit.csv", detail_audit)
    write_csv(outdir / "offcn_hubei_tax_annual_summary.csv", annual_reports)
    write_csv(outdir / "offcn_hubei_tax_row_conflicts.csv", all_conflicts)
    qa = {
        "scraped_at": now_iso(), "source": "https://zhiwei.offcn.com/",
        "method": "低频分页列表抓取；按网页表头映射字段；详情页只从职位代码对应行/局部节点提取，保留可审计证据",
        "important": "过审总数是年度汇总口径，不是逐岗位人数；报考人数不得替代过审人数。",
        "years": annual_reports, "total_position_rows": len(all_positions),
        "total_detail_links_seen": len(all_detail_links), "total_detail_pages_audited": len(detail_audit),
        "list_pages_blocked": [r for r in all_page_audits if r["status"] in ("http_403", "http_429", "access_challenge")],
        "limitations": [
            "中公是第三方职位库，不是招录机关官方原始数据库。",
            "各年度报名统计时点可能不同；历史报名/过审人数不代表2027实际竞争比。",
            "网页无逐岗位过审字段且详情页无法在职位代码局部核验时，最终过审人数保持空值。",
            "未抓到数据不代表0人；不要用年度总数填补逐岗位空值。",
        ],
    }
    (outdir / "qa_report.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n抓取完成。请先检查 qa_report.json 和 scrape_log.txt。")
    for report in annual_reports:
        print(f"{report['year']}: rows={report['positions_unique']}/site={report['expected_positions']}; 逐岗位过审={report['rows_with_position_level_approved_count']}; 报名={report['rows_with_position_level_applicant_count']}; 状态={report['status']}")
    print(f"输出目录：{outdir}")

if __name__ == "__main__":
    main()
