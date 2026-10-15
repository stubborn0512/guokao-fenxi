#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect Hubei 2021–2025 national-civil-service job-level approval counts and match them to the 114 Hubei 2027 candidate jobs.

Only public pages are requested. No login-gated endpoints, CAPTCHA bypass, or guessed private API is used.
The target metric is the position-level number of applicants marked “合格人数/过审人数/资格审查通过人数”.
“报名人数” is captured separately and is never substituted for approval counts.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

YEARS = range(2021, 2026)
OVERVIEW_TEMPLATE = "https://ah.huatu.com/zw/hubei/{year}.html"
TARGET_CSV_DEFAULT = Path("data/hubei_candidates_2027.csv")
OFFCN_CSV_DEFAULT = Path("data/offcn_hubei_tax_2021_2025/offcn_hubei_tax_positions_2021_2025_enriched.csv")
OUT_DEFAULT = Path("data/approval_counts_2021_2025")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
CODE_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
COMP_MAJOR_RE = re.compile(r"计算机|软件工程|网络空间安全|信息与通信工程|电子信息类|数据科学与大数据技术|计算机技术|计算机应用|计算机软件")
OPEN_MAJOR_RE = re.compile(r"不限|专业不限|不限制专业|无专业要求|无专业限制|专业不限")

ALIASES = {
    "position_code": ["岗位代码", "职位代码"],
    "unit": ["用人司局", "招录机关", "招录单位"],
    "position_name": ["职位名称", "招考职位"],
    "education": ["学历要求", "学历"],
    "major_requirement": ["专业要求", "专业"],
    "recruit_count": ["招考人数", "招录人数", "计划招录人数"],
    "applicant_count": ["报名人数", "报考人数", "报名总人数"],
    "approved_count": ["合格人数", "过审人数", "资格审查通过人数", "审核通过人数"],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def norm(value: object) -> str:
    text = str(value or "").replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def norm_key(value: object) -> str:
    text = norm(value).replace("（", "(").replace("）", ")").replace("【", "[").replace("】", "]")
    text = re.sub(r"\s+", "", text)
    return text


def to_int(value: object):
    text = norm(value).replace(",", "").replace("，", "")
    match = re.fullmatch(r"\d+", text)
    return int(text) if match else None


def csv_read(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_write(path: Path, rows, fieldnames=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    })
    retries = Retry(total=2, connect=2, read=1, status=1, backoff_factor=1.0,
                    status_forcelist=(500, 502, 503, 504), allowed_methods=frozenset(["GET"]),
                    raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_public_html(session, url, delay):
    time.sleep(max(0.0, delay))
    try:
        response = session.get(url, timeout=(12, 35))
    except requests.RequestException as exc:
        return None, "request_error", str(exc)
    if response.status_code in (403, 429):
        # Do not try to evade blocking or access controls. Record and stop this page.
        return None, f"http_{response.status_code}_stopped", f"HTTP {response.status_code}; stopped without bypass"
    if response.status_code >= 400:
        return None, f"http_{response.status_code}", f"HTTP {response.status_code}"
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    content = response.text
    lowered = content.lower()
    if any(token in lowered for token in ("captcha", "验证码", "访问过于频繁", "安全验证")) and len(content) < 12000:
        return None, "verification_page_stopped", "Possible verification/challenge page; no bypass attempted"
    return content, "ok", ""


def find_col(headers, aliases):
    compact = [re.sub(r"\s+", "", norm(h)) for h in headers]
    for alias in aliases:
        a = re.sub(r"\s+", "", alias)
        for i, header in enumerate(compact):
            if header == a:
                return i
    for alias in aliases:
        a = re.sub(r"\s+", "", alias)
        for i, header in enumerate(compact):
            if a and a in header:
                return i
    return None


def discover_department_pages(html, year, overview_url):
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(rf"/zw/hubei/{year}_\d+\.html$")
    found = []
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(overview_url, anchor["href"])
        path = urlparse(absolute).path
        if pattern.search(path):
            found.append(absolute.split("#", 1)[0])
    return list(dict.fromkeys(found))


def classify_system(text: object) -> str:
    value = norm(text)
    if "国家税务总局" in value or "税务局" in value:
        return "税务"
    if any(k in value for k in ("国家金融监督管理总局", "中国银行保险监督管理委员会", "银行保险监督管理局", "银保监会")):
        return "金融监管"
    if "铁路公安局" in value or "铁路公安处" in value:
        return "铁路公安"
    if "中国人民银行" in value or "人民银行" in value:
        return "人民银行"
    if "审计署驻武汉特派员办事处" in value:
        return "审计"
    return "其他系统"


def parse_huatu_page(html, url, year):
    soup = BeautifulSoup(html, "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))
    expected_match = re.search(r"共\s*(\d+)\s*条信息", page_text)
    expected_rows = int(expected_match.group(1)) if expected_match else None
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        for header_row_idx, tr in enumerate(trs[:10]):
            cells = tr.find_all(["th", "td"], recursive=False)
            headers = [norm(cell.get_text(" ", strip=True)) for cell in cells]
            joined = "|".join(headers)
            if not (("岗位代码" in joined or "职位代码" in joined) and
                    ("用人司局" in joined or "招录机关" in joined) and "合格人数" in joined):
                continue
            indexes = {key: find_col(headers, aliases) for key, aliases in ALIASES.items()}
            required = ("position_code", "unit", "position_name", "approved_count")
            if any(indexes[key] is None for key in required):
                continue
            records = []
            for data_tr in trs[header_row_idx + 1:]:
                data_cells = data_tr.find_all(["th", "td"], recursive=False)
                values = [norm(cell.get_text(" ", strip=True)) for cell in data_cells]
                row_text = norm(data_tr.get_text(" ", strip=True))
                def cell(field):
                    idx = indexes.get(field)
                    return values[idx] if idx is not None and idx < len(values) else ""
                code_match = CODE_RE.search(cell("position_code") or row_text)
                if not code_match:
                    continue
                detail_url = ""
                for anchor in data_tr.find_all("a", href=True):
                    label = norm(anchor.get_text(" ", strip=True))
                    if label in ("详细", "职位详细", "详情"):
                        detail_url = urljoin(url, anchor.get("href", ""))
                        break
                unit = cell("unit")
                title = cell("position_name")
                records.append({
                    "year": year,
                    "position_code": code_match.group(1),
                    "system": classify_system(unit + " " + title),
                    "unit": unit,
                    "position_name": title,
                    "education": cell("education"),
                    "major_requirement": cell("major_requirement"),
                    "recruit_count": to_int(cell("recruit_count")),
                    "applicant_count": to_int(cell("applicant_count")),
                    "approved_count": to_int(cell("approved_count")),
                    "source_platform": "华图",
                    "source_url": url,
                    "detail_source_url": detail_url,
                    "fetched_at": now_iso(),
                    "raw_row_text": row_text,
                    "approval_field_label": headers[indexes["approved_count"]],
                })
            if records:
                return records, expected_rows, "ok"
    return [], expected_rows, "no_required_position_table"


def load_offcn_rows(path: Path):
    if not path.exists():
        logging.warning("未找到中公历史补充文件：%s；本次仍将使用华图公开职位表。", path)
        return []
    result = []
    for row in csv_read(path):
        try:
            year = int(row.get("year", ""))
        except (TypeError, ValueError):
            continue
        if year not in range(2021, 2026) or year == 2025:
            # 2025 Offcn rows do not independently expose approval counts; Huatu is the public fallback.
            continue
        approved = row.get("approved_count_final", row.get("approved_count_list", ""))
        if approved in (None, ""):
            approved = row.get("approved_count_list", "")
        result.append({
            "year": year,
            "position_code": norm(row.get("position_code", "")),
            "system": "税务",
            "unit": norm(row.get("unit", "")),
            "position_name": norm(row.get("position_name", "")),
            "education": norm(row.get("education", "")),
            "major_requirement": norm(row.get("major_requirement", "")),
            "recruit_count": to_int(row.get("recruit_count", "")),
            "applicant_count": to_int(row.get("applicant_count_final", row.get("applicant_count_list", ""))),
            "approved_count": to_int(approved),
            "source_platform": "中公",
            "source_url": norm(row.get("source_url", "")),
            "detail_source_url": norm(row.get("detail_source_url", "")),
            "fetched_at": norm(row.get("fetched_at", "")),
            "raw_row_text": norm(row.get("raw_row_text", "")),
            "approval_field_label": norm(row.get("approved_count_source", "中公公开列表逐岗位合格/过审人数")) or "中公公开列表逐岗位合格/过审人数",
        })
    return result


def compatible_major(major: object) -> str:
    text = norm(major)
    if not text:
        return "unknown"
    if OPEN_MAJOR_RE.search(text):
        return "compatible"
    if COMP_MAJOR_RE.search(text):
        return "compatible"
    return "incompatible"


def bachelor_eligible(education: object) -> str:
    text = norm(education)
    if not text:
        return "unknown"
    if "本科" in text or "大学本科" in text:
        return "yes"
    return "no"


def title_family(system: str, title: object) -> str:
    text = norm(title)
    if not text:
        return ""
    if system == "税务":
        # Remove the serial numeral suffix (e.g. （一） / （十二）) while retaining the job family.
        base = re.sub(r"[（(][一二三四五六七八九十百\d]+[）)]", "", text)
        return norm_key(base)
    if system == "金融监管":
        for key in ("统计信息部门", "科技部门", "信息科技部门", "综合部门", "监管部门", "财会部门", "法律部门"):
            if key in text:
                return key
        return norm_key(text)
    if system == "铁路公安":
        if "基层所队民警" in text:
            return "基层所队民警"
        if "公安" in text:
            return "铁路公安" + re.sub(r"^.*?铁路公安处", "", text)
    if system == "人民银行":
        if "综合业务部门" in text:
            return "综合业务部门"
        return norm_key(text)
    if system == "审计":
        if "审计业务处" in text:
            return "审计业务处"
        return norm_key(text)
    return norm_key(text)


def same_system(target_system: str, historical: dict) -> bool:
    hist_system = historical.get("system", "")
    if hist_system == target_system:
        return True
    # Historical regulatory-system restructuring: CBIRC and NFRA belong to the same comparable system.
    if target_system == "金融监管" and any(k in norm(historical.get("unit", "")) for k in (
        "中国银行保险监督管理委员会", "银行保险监督管理局", "银保监会")):
        return True
    return False


def median_value(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None


def canonicalize_sources(huatu_rows, offcn_rows):
    """Deduplicate same year/code/unit/title. For 2021–24 Hubei tax use Offcn first; Huatu remains a cross-check."""
    combined = defaultdict(list)
    for row in huatu_rows + offcn_rows:
        key = (int(row["year"]), norm_key(row.get("position_code")), norm_key(row.get("unit")), norm_key(row.get("position_name")))
        combined[key].append(row)
    canonical, conflicts, source_versions = [], [], []
    for key, versions in combined.items():
        # Retain every source version as evidence, including disagreements.
        for v in versions:
            source_versions.append(dict(v))
        huatu = next((v for v in versions if v.get("source_platform") == "华图"), None)
        offcn = next((v for v in versions if v.get("source_platform") == "中公"), None)
        year = int(key[0])
        system = (offcn or huatu or versions[0]).get("system", "")
        if system == "税务" and year <= 2024 and offcn and offcn.get("approved_count") is not None:
            chosen = offcn
            source_used = "中公（优先）"
        elif huatu and huatu.get("approved_count") is not None:
            chosen = huatu
            source_used = "华图（公开职位表）"
        elif offcn and offcn.get("approved_count") is not None:
            chosen = offcn
            source_used = "中公（兜底）"
        else:
            chosen = huatu or offcn or versions[0]
            source_used = "暂无可核验过审人数"
        row = dict(chosen)
        row["approved_count_used"] = chosen.get("approved_count")
        row["approval_source_used"] = source_used
        row["approved_count_huatu"] = huatu.get("approved_count") if huatu else None
        row["approved_count_offcn"] = offcn.get("approved_count") if offcn else None
        row["applicant_count_huatu"] = huatu.get("applicant_count") if huatu else None
        row["applicant_count_offcn"] = offcn.get("applicant_count") if offcn else None
        row["source_urls_all"] = " ; ".join(dict.fromkeys(v.get("source_url", "") for v in versions if v.get("source_url")))
        row["source_platforms_all"] = ";".join(dict.fromkeys(v.get("source_platform", "") for v in versions if v.get("source_platform")))
        if huatu and offcn and huatu.get("approved_count") is not None and offcn.get("approved_count") is not None:
            if huatu["approved_count"] != offcn["approved_count"]:
                conflicts.append({
                    "year": year, "position_code": row.get("position_code", ""), "unit": row.get("unit", ""),
                    "position_name": row.get("position_name", ""),
                    "approved_count_huatu": huatu.get("approved_count"), "approved_count_offcn": offcn.get("approved_count"),
                    "chosen_count": row.get("approved_count_used"), "chosen_source": source_used,
                    "huatu_url": huatu.get("source_url", ""), "offcn_url": offcn.get("source_url", ""),
                    "resolution_rule": "2021—2024税务优先中公逐岗位过审数；华图值保留作对照；差异不覆盖原值。",
                })
        canonical.append(row)
    canonical.sort(key=lambda r: (int(r.get("year", 0)), r.get("system", ""), r.get("position_code", "")))
    return canonical, conflicts, source_versions


def match_tier(target: dict, hist: dict):
    tsys = norm(target.get("系统类别", ""))
    hsys = norm(hist.get("system", ""))
    if not same_system(tsys, hist):
        return None
    major_status = compatible_major(hist.get("major_requirement", ""))
    edu_status = bachelor_eligible(hist.get("education", ""))
    if major_status == "incompatible" or edu_status == "no":
        return None
    t_unit, h_unit = norm_key(target.get("用人司局", "")), norm_key(hist.get("unit", ""))
    t_title, h_title = norm_key(target.get("招考职位", "")), norm_key(hist.get("position_name", ""))
    t_family = title_family(tsys, target.get("招考职位", ""))
    h_family = title_family(hsys if hsys != "其他系统" else tsys, hist.get("position_name", ""))
    strong = major_status == "compatible" and edu_status == "yes"
    if strong and t_unit == h_unit and t_title == h_title:
        return (1, "T1_同司局同职位_专业明确兼容且本科可报", "高")
    if strong and t_unit == h_unit and t_family and t_family == h_family:
        return (2, "T2_同司局同职能_专业明确兼容且本科可报", "较高")
    if strong and t_title == h_title:
        return (3, "T3_同系统同职位名称_专业明确兼容且本科可报", "中高")
    if strong and t_family and t_family == h_family:
        return (4, "T4_同系统同职能类别_专业明确兼容且本科可报", "中")
    if strong:
        return (5, "T5_同系统专业兼容且本科可报_宽口径背景", "较低")
    # Explicitly labeled last-resort context only: education/major is unknown in the source record.
    if major_status == "unknown" or edu_status == "unknown":
        if t_unit == h_unit and t_title == h_title:
            return (6, "T6_同司局同职位但专业或学历信息不完整_背景参考", "低")
        if t_family and t_family == h_family:
            return (7, "T7_同系统同职能但专业或学历信息不完整_背景参考", "低")
    return None


def match_targets(targets, historical):
    summaries, details = [], []
    for target in targets:
        target_code = norm(target.get("职位代码", ""))
        target_system = norm(target.get("系统类别", ""))
        for year in YEARS:
            possible = []
            for hist in historical:
                if int(hist.get("year", 0) or 0) != year:
                    continue
                tier = match_tier(target, hist)
                if tier:
                    possible.append((tier, hist))
            if not possible:
                summaries.append({
                    "目标职位代码_2027": target_code, "目标用人司局": target.get("用人司局", ""),
                    "目标职位名称": target.get("招考职位", ""), "目标系统": target_system, "历史年份": year,
                    "历史合格人数参考中位数": None, "所选匹配层级": "未找到符合专业/本科条件且可核验的历史可比岗位",
                    "样本数": 0, "样本最小值": None, "样本最大值": None,
                    "样本招录人数合计": 0, "样本历史职位代码": "", "样本来源平台": "",
                    "来源链接": "", "匹配可信度": "无", "结果解释": "空白/缺数不等于0；需要继续人工检索第三方公开档案。",
                })
                continue
            best_tier = min(t[0][0] for t in possible)
            chosen = [(tier, hist) for tier, hist in possible if tier[0] == best_tier]
            values = [hist.get("approved_count_used") for _, hist in chosen if hist.get("approved_count_used") is not None]
            med = median_value(values)
            first_meta = chosen[0][0]
            urls = list(dict.fromkeys(url for _, hist in chosen for url in (hist.get("source_urls_all", "").split(" ; ") if hist.get("source_urls_all") else [hist.get("source_url", "")]) if url))
            platforms = list(dict.fromkeys(p for _, hist in chosen for p in (hist.get("source_platforms_all", "").split(";") if hist.get("source_platforms_all") else [hist.get("source_platform", "")]) if p))
            codes = list(dict.fromkeys(norm(hist.get("position_code", "")) for _, hist in chosen if hist.get("position_code")))
            recruit_sum = sum(hist.get("recruit_count") or 0 for _, hist in chosen)
            applicant_values = [hist.get("applicant_count") for _, hist in chosen]
            applicant_median = median_value(applicant_values)
            row = {
                "目标职位代码_2027": target_code,
                "目标用人司局": target.get("用人司局", ""),
                "目标职位名称": target.get("招考职位", ""),
                "目标地区": target.get("地区/辖区说明", target.get("公告工作地点", "")),
                "目标系统": target_system,
                "历史年份": year,
                "历史合格人数参考中位数": med,
                "历史报名人数参考中位数_辅助": applicant_median,
                "所选匹配层级": first_meta[1],
                "样本数": len(chosen),
                "有合格人数的样本数": len(values),
                "样本最小值": min(values) if values else None,
                "样本最大值": max(values) if values else None,
                "样本招录人数合计": recruit_sum,
                "样本历史职位代码": ";".join(codes),
                "样本来源平台": ";".join(platforms),
                "来源链接": " ; ".join(urls[:20]),
                "匹配可信度": first_meta[2] if values else "无合格人数值",
                "结果解释": "该数是所选历史可比岗位的合格人数中位数，不等于2027岗位实际人数。若样本数>1，范围见最小/最大值。" if values else "找到职位字段相似的样本，但这些样本缺少可核验合格人数。",
            }
            summaries.append(row)
            for tier, hist in chosen:
                details.append({
                    "目标职位代码_2027": target_code, "目标用人司局": target.get("用人司局", ""),
                    "目标职位名称": target.get("招考职位", ""), "目标系统": target_system,
                    "历史年份": year, "匹配层级": tier[1], "历史职位代码": hist.get("position_code", ""),
                    "历史用人司局": hist.get("unit", ""), "历史职位名称": hist.get("position_name", ""),
                    "历史学历要求": hist.get("education", ""), "历史专业要求": hist.get("major_requirement", ""),
                    "历史招录人数": hist.get("recruit_count"), "历史报名人数": hist.get("applicant_count"),
                    "历史合格人数_选用值": hist.get("approved_count_used"),
                    "华图合格人数": hist.get("approved_count_huatu"), "中公合格人数": hist.get("approved_count_offcn"),
                    "采用来源": hist.get("approval_source_used", ""), "来源平台": hist.get("source_platforms_all", hist.get("source_platform", "")),
                    "来源链接": hist.get("source_urls_all", hist.get("source_url", "")),
                    "明细链接": hist.get("detail_source_url", ""),
                    "所选统计样本": "是", "源字段标签": hist.get("approval_field_label", ""),
                })
    return summaries, details


def scrape_years(session, years, delay):
    all_rows, page_audit = [], []
    blocked_hosts = set()
    for year in years:
        overview_url = OVERVIEW_TEMPLATE.format(year=year)
        overview_html, status, error = fetch_public_html(session, overview_url, delay)
        if not overview_html:
            page_audit.append({"year": year, "page_type": "overview", "url": overview_url, "status": status,
                               "expected_rows": None, "parsed_rows": 0, "error": error})
            continue
        urls = discover_department_pages(overview_html, year, overview_url)
        page_audit.append({"year": year, "page_type": "overview", "url": overview_url, "status": "ok",
                           "expected_rows": None, "parsed_rows": 0, "department_pages_discovered": len(urls), "error": ""})
        if not urls:
            page_audit.append({"year": year, "page_type": "overview", "url": overview_url, "status": "no_department_links",
                               "expected_rows": None, "parsed_rows": 0, "error": "未找到形如 /zw/hubei/{year}_数字.html 的公开系统职位页链接"})
            continue
        stopped = False
        for url in urls:
            host = urlparse(url).netloc
            if host in blocked_hosts:
                page_audit.append({"year": year, "page_type": "department", "url": url, "status": "skipped_after_block",
                                   "expected_rows": None, "parsed_rows": 0, "error": "之前的同站页面触发403/429或验证页，按策略停止请求"})
                continue
            html, pstatus, perror = fetch_public_html(session, url, delay)
            if not html:
                page_audit.append({"year": year, "page_type": "department", "url": url, "status": pstatus,
                                   "expected_rows": None, "parsed_rows": 0, "error": perror})
                if pstatus in ("http_403_stopped", "http_429_stopped", "verification_page_stopped"):
                    blocked_hosts.add(host)
                    stopped = True
                continue
            rows, expected_rows, parse_status = parse_huatu_page(html, url, year)
            page_audit.append({"year": year, "page_type": "department", "url": url,
                               "status": parse_status, "expected_rows": expected_rows,
                               "parsed_rows": len(rows), "error": "" if rows else "页面未识别到明确包含岗位代码、用人司局、职位名称和合格人数的表格"})
            all_rows.extend(rows)
    return all_rows, page_audit


def summarize_qa(targets, raw_rows, canonical, summaries, conflicts, page_audit, output_dir):
    by_year = {}
    for year in YEARS:
        huatu = [r for r in raw_rows if int(r.get("year", 0) or 0) == year]
        can = [r for r in canonical if int(r.get("year", 0) or 0) == year]
        by_system = Counter(r.get("system", "") for r in can)
        matched = [r for r in summaries if int(r.get("历史年份", 0) or 0) == year]
        by_tier = Counter(r.get("所选匹配层级", "") for r in matched)
        by_year[str(year)] = {
            "huatu_rows_captured": len(huatu),
            "canonical_historical_jobs": len(can),
            "canonical_jobs_with_approved_count": sum(r.get("approved_count_used") is not None for r in can),
            "jobs_by_system": dict(by_system),
            "target_job_year_pairs": len(matched),
            "target_pairs_with_numeric_reference": sum(r.get("历史合格人数参考中位数") is not None for r in matched),
            "target_pairs_without_match": sum(str(r.get("所选匹配层级", "")).startswith("未找到") for r in matched),
            "selected_tier_distribution": dict(by_tier),
        }
    qa = {
        "generated_at": now_iso(),
        "definition": {
            "target_set": "data/hubei_candidates_2027.csv 中筛出的114个湖北目标岗位",
            "time_range": "2021—2025国考报名/资格审查历史数据，不收2026作为已完成年度，不将2027计划招录人数视为报名人数",
            "primary_metric": "岗位级‘合格人数/过审人数/资格审查通过人数’",
            "separate_metrics": ["报名人数", "招录人数"],
            "not_collected_as_primary_metric": ["进面人数", "面试入围人数", "年度省级过审总人数分摊值"],
        },
        "target_count": len(targets),
        "target_job_year_pairs_expected": len(targets) * len(list(YEARS)),
        "target_job_year_pairs_output": len(summaries),
        "raw_huatu_rows": len(raw_rows),
        "canonical_historical_job_rows": len(canonical),
        "source_conflict_rows": len(conflicts),
        "all_years": by_year,
        "pages": {
            "page_audit_rows": len(page_audit),
            "department_pages_ok": sum(1 for r in page_audit if r.get("page_type") == "department" and r.get("status") == "ok"),
            "department_pages_failed_or_unparsed": sum(1 for r in page_audit if r.get("page_type") == "department" and r.get("status") != "ok"),
        },
        "matching_rules": [
            "T1：同用人司局+同职位名，历史专业要求明确兼容计算机专业或不限，且历史岗位本科可报。",
            "T2：同用人司局+同职能类别，专业明确兼容且本科可报。",
            "T3：同系统+同职位名，专业明确兼容且本科可报。",
            "T4：同系统+同职能类别，专业明确兼容且本科可报。",
            "T5：同系统+专业明确兼容且本科可报的较宽口径背景样本。",
            "T6/T7只在专业或学历字段缺失时作为低可信背景，不冒充严格匹配。",
            "一个目标岗位-年份单元格只采用最高优先级（T数最小）的那一层；多个样本用合格人数中位数，并保存样本数、最小/最大值与逐条来源。",
        ],
        "source_precedence": "2021—2024税务同岗位：中公已有逐岗位合格/过审人数优先，华图同岗位值并列保留且差异进冲突审计；其他系统及2025税务：华图公开职位表优先；中公只作为补充兜底。",
        "important_caveat": "除T1且样本数为1外，输出是历史可比岗位参考，不是目标2027岗位的真实报名过审人数。空白/无匹配不等于0。第三方公开页面可能是特定时间截面。",
        "by_year": by_year,
        "outputs": [
            str(output_dir / "hubei_historical_positions_source_versions.csv"),
            str(output_dir / "hubei_historical_positions_canonical.csv"),
            str(output_dir / "target_114_approval_counts_2021_2025.csv"),
            str(output_dir / "target_114_approval_match_details.csv"),
            str(output_dir / "scrape_pages_audit.csv"),
            str(output_dir / "source_conflicts_audit.csv"),
        ],
    }
    (output_dir / "qa_report.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    return qa


def main():
    parser = argparse.ArgumentParser(description="采集并匹配湖北国考2021—2025岗位级过审/合格人数")
    parser.add_argument("--targets", type=Path, default=TARGET_CSV_DEFAULT)
    parser.add_argument("--offcn", type=Path, default=OFFCN_CSV_DEFAULT)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--delay", type=float, default=0.45, help="公开页面请求间隔秒数，默认0.45秒")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    if not args.targets.exists():
        raise FileNotFoundError(f"目标岗位文件不存在：{args.targets}")
    targets = csv_read(args.targets)
    if not targets:
        raise RuntimeError("目标岗位CSV为空")
    missing_headers = [h for h in ("职位代码", "系统类别", "用人司局", "招考职位") if h not in targets[0]]
    if missing_headers:
        raise RuntimeError(f"目标岗位文件缺少必需字段：{missing_headers}")
    years = range(args.start_year, args.end_year + 1)
    args.out.mkdir(parents=True, exist_ok=True)
    session = make_session()
    logging.info("目标岗位=%s，目标岗位×年份=%s，年份=%s—%s", len(targets), len(targets) * len(list(years)), args.start_year, args.end_year)
    huatu_rows, page_audit = scrape_years(session, years, args.delay)
    offcn_rows = load_offcn_rows(args.offcn)
    canonical, conflicts, versions = canonicalize_sources(huatu_rows, offcn_rows)
    summaries, details = match_targets(targets, canonical)
    csv_write(args.out / "hubei_historical_positions_source_versions.csv", versions)
    csv_write(args.out / "hubei_historical_positions_canonical.csv", canonical)
    csv_write(args.out / "target_114_approval_counts_2021_2025.csv", summaries)
    csv_write(args.out / "target_114_approval_match_details.csv", details)
    csv_write(args.out / "scrape_pages_audit.csv", page_audit)
    csv_write(args.out / "source_conflicts_audit.csv", conflicts)
    qa = summarize_qa(targets, huatu_rows, canonical, summaries, conflicts, page_audit, args.out)
    print(json.dumps(qa, ensure_ascii=False, indent=2))
    if len(summaries) != len(targets) * len(list(years)):
        raise RuntimeError(f"审计失败：目标-年份组合应为{len(targets)*len(list(years))}，实际{len(summaries)}")
    if not huatu_rows:
        logging.warning("本轮没有从华图获取职位行；请先查看 scrape_pages_audit.csv 和 qa_report.json，不要把此结果视为数据已补齐。")

if __name__ == "__main__":
    main()