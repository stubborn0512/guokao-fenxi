# 中公湖北税务岗位报名/过审人数抓取

该脚本抓取中公国考职位库中湖北省税务局 2021—2025 年列表页及可公开访问的职位详情页。

## Windows 本机运行

在仓库根目录打开 PowerShell 或 CMD：

~~~powershell
py -m pip install -r requirements-offcn.txt
py scripts/offcn_hubei_tax_scraper.py --out offcn_hubei_tax_data
~~~

只抓列表页（不访问详情链接）：

~~~powershell
py scripts/offcn_hubei_tax_scraper.py --out offcn_hubei_tax_data --skip-details
~~~

先测试两个年度：

~~~powershell
py scripts/offcn_hubei_tax_scraper.py --years 2021 2022 --out test_2021_2022
~~~

## GitHub Actions 运行

打开仓库 Actions → 中公湖北税务历史数据抓取 → Run workflow。结束后从该次运行的 Artifacts 下载 offcn-hubei-tax-data 压缩包。

## 输出文件

基础抓取输出（与中公网页字段保持一致）：

- `offcn_hubei_tax_positions_2021_2025.csv`：中公逐岗位数据；2021—2024 年包含列表页的过审人数，2025 年保留原始报考人数与来源证据。
- `offcn_hubei_tax_annual_summary.csv`：中公年度汇总，含职位数、招录数、报名总数、过审总数。
- `offcn_hubei_tax_pages_audit.csv`：分页 URL、HTTP 状态和页面解析状态。
- `qa_report.json` 与 `scrape_log.txt`：基础抓取覆盖情况、访问限制和失败原因。

补充数据和最终合并文件：

- `offcn_hubei_tax_positions_2021_2025_enriched.csv`：五年合并结果，补充 2025 逐岗位“合格人数”。
- `offcn_hubei_tax_huatu_2025.csv`：华图公开列表中提取的 537 条 2025 年湖北税务岗位记录。
- `offcn_hubei_tax_huatu_2025_match_audit.csv`：跨来源逐岗位匹配审计和报名人数差异。
- `offcn_hubei_tax_huatu_2025_enrichment_report.json`：补数前后各年度字段覆盖率、匹配数和限制。

## 过审人数口径（重要）

中公 2021—2024 年列表的逐岗位表格列明确包含“过审人数/合格人数”，共提取 1,802 条职位年度记录。中公 2025 列表只显示逐岗位“报考人数”，虽然年度汇总显示总过审 87,202，但每个岗位的“报名大数据”链接是 JavaScript 生成的详情入口，页面逻辑在未登录时会阻止跳转。因此脚本不绕过登录限制，也不把年度总人数分摊到岗位。

为补齐 2025 年逐岗位数据，使用华图公开职位表 `https://ah.huatu.com/zw/hubei/2025_3.html` 的显式“合格人数”列，并按“职位代码 + 用人司局 + 职位名称”逐岗匹配。537/537 条均精确匹配，未匹配或歧义记录为 0。合并文件的 2025 “approved_count_final”因此标注为华图补充来源，不能说成是中公逐岗位过审字段。

需要注意：中公与华图的报名人数有 384/537 条不一致。平台统计时点或数据口径可能不同，因此保留两边原始报名人数及来源，不能将其视为同一时点的直接对比。合格/过审人数用于历史热度参照，不代表 2027 年实际竞争情况，资格和报名情况以官方信息为准。

来源入口：
- https://zhiwei.offcn.com/gj/2021/bmall7905.html
- https://zhiwei.offcn.com/gj/2022/bmall9423_1.html
- https://zhiwei.offcn.com/gj/2023/bmall9217.html
- https://zhiwei.offcn.com/gj/2024/bmall10893.html
- https://zhiwei.offcn.com/gj/2025/bmall11865_2.html
