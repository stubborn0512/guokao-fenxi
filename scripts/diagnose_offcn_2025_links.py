#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnose interactive links used by the 2025 Offcn Hubei tax job table.

Fetches only one public listing page. It records link/button attributes and script
references for a few sample positions; it does not click or bypass access controls.
"""
import json, re
from pathlib import Path
import requests
from bs4 import BeautifulSoup

URL = "https://zhiwei.offcn.com/gj/2025/bmall11865_2.html"
CODES = {"300110008013", "300110008014", "300110008015"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}
def clean(s):
    return re.sub(r"\\s+", " ", str(s or "")).strip()
def main():
    response = requests.get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    soup = BeautifulSoup(response.text, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        text = clean(tr.get_text(" ", strip=True))
        codes = re.findall(r"(?<!\\d)\\d{12}(?!\\d)", text)
        if not set(codes) & CODES:
            continue
        interactions = []
        for node in tr.find_all(["a", "button", "input", "span", "i", "div"]):
            node_text = clean(node.get_text(" ", strip=True))
            attrs = {k: v for k, v in node.attrs.items() if k in {
                "href", "onclick", "data-href", "data-url", "data-id", "data-code",
                "data-toggle", "data-target", "id", "class", "title", "type", "value",
                "role", "target", "rel", "url", "data"
            }}
            if node.name in ("a", "button", "input") or node_text in ("报名大数据", "历年分数线", "职位详情"):
                if attrs or node_text in ("报名大数据", "历年分数线", "职位详情"):
                    interactions.append({"tag": node.name, "text": node_text[:100], "attrs": attrs})
        rows.append({"position_codes": codes, "row_text": text[:900], "interactions": interactions})
    scripts = []
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if src:
            scripts.append({"src": requests.compat.urljoin(URL, src), "type": tag.get("type", "")})
        elif tag.string and any(k in tag.string.lower() for k in ("baokao", "报名", "detail", "ajax")):
            scripts.append({"inline_script_excerpt": clean(tag.string)[:1000]})
    result = {"source_url": URL, "http_status": response.status_code, "page_title": clean(soup.title.get_text(" ", strip=True)) if soup.title else "",
              "matched_rows": rows, "script_refs": scripts}
    out = Path("data/offcn_diagnostics_2025_links.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
if __name__ == "__main__":
    main()
