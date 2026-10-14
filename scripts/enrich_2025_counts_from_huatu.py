#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Supplement 2025 Hubei tax approval counts from Huatu's public job table.

This does not access Offcn's login-gated lnblb pages. It reads the public Huatu
list page where each 2025 Hubei tax position has an explicit "合格人数" value,
then matches by job code + unit + position name to the existing Offcn extraction.
Original Offcn fields remain preserved; every substituted/fallback count is tagged
with its source URL and an audit record.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HUATU_URL = "https://ah.huatu.com/zw/hubei/2025_3.html"
INPUT_CSV = Path("data/offcn_hubei_tax_2021_2025/offcn_hubei_tax_positions_2021_2025.csv")
OUT_DIR = Path("data/offcn_hubei_tax_2021_2025")
OUTPUT_CSV = OUT_DIR / "offcn_hubei_tax_positions_2021_2025_enriched.csv"
HUATU_CSV = OUT_DIR / "offcn_hubei_tax_huatu_2025.csv"
AUDIT_CSV = OUT_DIR / "offcn_hubei_tax_huatu_2025_match_audit.csv"
QA_JSON = OUT_DIR / "offcn_hubei_tax_huatu_2025_enrichment_report.json"

HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def norm(value: str) -> str:
    value = str(value or "").replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()

def norm_key(value: str) -> str:
    value = norm(value)
    value = value.replace("（", "(").replace("）", ")")
    value = re.sub(r"\s+", "", value)
    return value

def to_int(value):
    s = norm(value).replace(",", "").replace("，", "")
    match = re.search(r"(?<!\d)(\d+)(?!\d)", s)
    return int(match.group(1)) if match else ""

def col_index(headers, aliases):
    compact = [norm(h).replace(" ", "") for h in headers]
    for alias in aliases:
        alias = alias.replace(" ", "")
        for i, h in enumerate(compact):
            if h == alias:
                return i
    for alias in aliases:
        alias = alias.replace(" ", "")
        for i, h in enumerate(compact):
            if alias and alias in h:
                return i
    return None

def csv_read(path: Path):
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def csv_write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    headers = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

def fetch_huatu():
    response = requests.get(HUATU_URL, headers=HEADERS_HTTP, timeout=(15, 45))
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    expected_positions = ""
    whole_text = norm(soup.get_text(" ", strip=True))
    total_match = re.search(r"(?:共|总计)\s*(\d+)\s*条", whole_text)
    if total_match:
        expected_positions = int(total_match.group(1))
    records = []
    header_used = None
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        for header_row_no, tr in enumerate(trs[:8]):
            headers = [norm(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"], recursive=False)]
            joined = "|".join(headers)
            if not all(k in joined for k in ("岗位代码", "用人司局", "报名人数", "合格人数")):
                continue
            idx = {
                "position_code": col_index(headers, ["岗位代码", "职位代码"]),
                "unit": col_index(headers, ["用人司局", "招录机关"]),
                "position_name": col_index(headers, ["职位名称", "招考职位"]),
                "recruit_count": col_index(headers, ["招考人数", "招录人数"]),
                "applicant_count": col_index(headers, ["报名人数", "报考人数"]),
                "approved_count": col_index(headers, ["合格人数", "过审人数", "资格审查通过人数"]),
            }
            if any(idx[k] is None for k in ("position_code", "unit", "position_name", "approved_count")):
                continue
            header_used = headers
            for data_tr in trs[header_row_no + 1:]:
                cells = data_tr.find_all(["th", "td"], recursive=False)
                values = [norm(cell.get_text(" ", strip=True)) for cell in cells]
                row_text = norm(data_tr.get_text(" ", strip=True))
                def cell(field):
                    pos = idx[field]
                    return values[pos] if pos is not None and pos < len(values) else ""
                code_text = cell("position_code")
                match = re.search(r"(?<!\d)(\d{12})(?!\d)", code_text or row_text)
                if not match:
                    continue
                code = match.group(1)
                detail_url = ""
                for a in data_tr.find_all("a", href=True):
                    href = a.get("href", "")
                    label = norm(a.get_text(" ", strip=True))
                    if label in ("详细", "职位详细", "详情"):
                        detail_url = urljoin(HUATU_URL, href)
                        break
                record = {
                    "year": 2025,
                    "position_code": code,
                    "unit": cell("unit"),
                    "position_name": cell("position_name"),
                    "recruit_count": to_int(cell("recruit_count")),
                    "applicant_count_huatu": to_int(cell("applicant_count")),
                    "approved_count_huatu": to_int(cell("approved_count")),
                    "approval_field_label": headers[idx["approved_count"]],
                    "huatu_list_source_url": HUATU_URL,
                    "huatu_detail_source_url": detail_url,
                    "huatu_fetched_at": now_iso(),
                    "raw_row_text_huatu": row_text,
                }
                # The list has 537 rows; only retain row-level job-code records.
                if record["position_code"] and record["unit"] and record["position_name"]:
                    records.append(record)
            break
        if records:
            break
    if not records:
        raise RuntimeError("未从华图公开列表中识别到含岗位代码、用人司局、报名人数和合格人数的职位表。")
    # Avoid silently resolving conflicting duplicates.
    index = {}
    for row in records:
        key = (row["position_code"], norm_key(row["unit"]), norm_key(row["position_name"]))
        if key in index and index[key] != row:
            raise RuntimeError(f"华图列表出现重复冲突键：{key}")
        index[key] = row
    deduped = list(index.values())
    return deduped, expected_positions, header_used

def main():
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"找不到已有中公抓取数据：{INPUT_CSV}")
    offcn_rows = csv_read(INPUT_CSV)
    huatu_rows, expected_positions, headers = fetch_huatu()
    csv_write(HUATU_CSV, huatu_rows)

    huatu_index = {}
    for h in huatu_rows:
        exact = (h["position_code"], norm_key(h["unit"]), norm_key(h["position_name"]))
        huatu_index.setdefault(exact, []).append(h)
    by_code_unit = {}
    for h in huatu_rows:
        key = (h["position_code"], norm_key(h["unit"]))
        by_code_unit.setdefault(key, []).append(h)

    audit = []
    exact_matches = 0
    fallback_matches = 0
    no_match = 0
    approved_filled = 0
    applicant_mismatch = 0
    conflict_matches = 0
    for row in offcn_rows:
        if str(row.get("year", "")) != "2025":
            continue
        key = (str(row.get("position_code", "")), norm_key(row.get("unit", "")), norm_key(row.get("position_name", "")))
        candidates = huatu_index.get(key, [])
        match_method = "exact_code_unit_position_name"
        if not candidates:
            candidates = by_code_unit.get((str(row.get("position_code", "")), norm_key(row.get("unit", ""))), [])
            match_method = "unique_code_and_unit_fallback"
        if len(candidates) != 1:
            no_match += 1
            audit.append({
                "year": 2025, "position_code": row.get("position_code", ""), "offcn_unit": row.get("unit", ""),
                "offcn_position_name": row.get("position_name", ""), "match_status": "not_matched" if not candidates else "ambiguous_match",
                "match_method": match_method, "match_candidates": len(candidates), "approved_count_huatu": "",
                "applicant_count_offcn": row.get("applicant_count_final", row.get("applicant_count_list", "")),
                "applicant_count_huatu": "", "applicant_counts_equal": "", "source_url": HUATU_URL,
            })
            row["approved_count_huatu"] = ""
            row["applicant_count_huatu"] = ""
            row["huatu_match_status"] = "not_matched" if not candidates else "ambiguous_match"
            continue
        h = candidates[0]
        if match_method == "exact_code_unit_position_name":
            exact_matches += 1
        else:
            fallback_matches += 1
        current_approved = row.get("approved_count_final", "")
        # Never overwrite an existing first-source count. Use Huatu only as a fallback.
        if current_approved in ("", None) and h["approved_count_huatu"] not in ("", None):
            row["approved_count_final"] = h["approved_count_huatu"]
            row["approved_count_source"] = "华图公开职位表“合格人数”字段（补充来源；中公2025列表仅提供报考人数）"
            row["approved_count_source_url"] = h["huatu_list_source_url"]
            row["approved_count_source_detail_url"] = h["huatu_detail_source_url"]
            row["approved_count_source_field"] = h["approval_field_label"]
            row["approved_count_final_status"] = "filled_from_alternative_public_source"
            approved_filled += 1
        row["approved_count_huatu"] = h["approved_count_huatu"]
        row["applicant_count_huatu"] = h["applicant_count_huatu"]
        row["huatu_source_url"] = h["huatu_list_source_url"]
        row["huatu_detail_source_url"] = h["huatu_detail_source_url"]
        row["huatu_fetched_at"] = h["huatu_fetched_at"]
        row["huatu_match_status"] = "matched_exact" if match_method == "exact_code_unit_position_name" else "matched_fallback"
        offcn_applicant = row.get("applicant_count_final", row.get("applicant_count_list", ""))
        huatu_applicant = h["applicant_count_huatu"]
        if offcn_applicant not in ("", None) and huatu_applicant not in ("", None):
            equal = str(offcn_applicant).replace(",", "") == str(huatu_applicant).replace(",", "")
            if not equal:
                applicant_mismatch += 1
        else:
            equal = ""
        audit.append({
            "year": 2025, "position_code": row.get("position_code", ""), "offcn_unit": row.get("unit", ""),
            "offcn_position_name": row.get("position_name", ""), "match_status": "matched",
            "match_method": match_method, "match_candidates": 1, "approved_count_huatu": h["approved_count_huatu"],
            "applicant_count_offcn": offcn_applicant, "applicant_count_huatu": huatu_applicant,
            "applicant_counts_equal": equal, "recruit_count_huatu": h["recruit_count"],
            "huatu_detail_source_url": h["huatu_detail_source_url"], "source_url": HUATU_URL,
        })
    csv_write(OUTPUT_CSV, offcn_rows)
    csv_write(AUDIT_CSV, audit)
    report = {
        "generated_at": now_iso(),
        "offcn_original_csv": str(INPUT_CSV),
        "huatu_public_source_url": HUATU_URL,
        "huatu_table_header": headers,
        "huatu_expected_position_count_if_detected": expected_positions,
        "huatu_unique_position_records": len(huatu_rows),
        "offcn_2025_position_records": sum(str(r.get("year", "")) == "2025" for r in offcn_rows),
        "matches_exact_code_unit_name": exact_matches,
        "matches_fallback_code_unit_only": fallback_matches,
        "unmatched_or_ambiguous_offcn_2025_rows": no_match,
        "approval_count_fallbacks_added_from_huatu": approved_filled,
        "applicant_count_mismatches_between_offcn_and_huatu": applicant_mismatch,
        "source_policy": "2021-2024 Offcn approval counts remain unchanged. 2025 approval counts are filled from Huatu's explicit public '合格人数' column only after matching position code, unit and position name; no inferred values are used.",
        "caveat": "Offcn and Huatu applicant counts differ for some positions, indicating different capture snapshots or platform data. Therefore Huatu supplemental counts are clearly labeled and kept alongside Offcn original fields, not represented as Offcn-provided values.",
        "outputs": [str(OUTPUT_CSV), str(HUATU_CSV), str(AUDIT_CSV)],
    }
    QA_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Enriched data: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
