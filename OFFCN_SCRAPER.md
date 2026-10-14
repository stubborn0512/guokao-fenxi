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

- offcn_hubei_tax_positions_2021_2025.csv：逐岗位数据；approved_count_final 是最终过审字段。
- offcn_hubei_tax_annual_summary.csv：年度汇总，含网站展示的职位数、招录数、报名总数、过审总数。
- offcn_hubei_tax_pages_audit.csv：分页 URL、HTTP 状态和页面解析状态。
- offcn_hubei_tax_details_audit.csv：详情页核验结果和字段证据。
- offcn_hubei_tax_row_conflicts.csv：同年度同职位代码出现冲突时的审计记录。
- qa_report.json 与 scrape_log.txt：覆盖情况、访问限制和失败原因。
- raw_html/：默认保存已抓取页面的 HTML 原文。

## 过审人数口径（重要）

中公年度页面会同时展示“过审人数”汇总，但每年的逐岗位表格列头可能不同。2025 列表列头是“报考人数”，这只能作为报名人数，不能当成逐岗位过审人数。脚本只会在网页列头明确标注“过审人数/合格人数”，或职位代码对应的详情页局部区域明确出现该字段时补值。否则留空并记录原因，不会把年度总人数分摊到各岗位，也不会把报名人数误标成过审人数。

脚本只访问公开页面、低频串行请求；遇到 403、429 或访问验证即停止相应抓取，不会尝试绕过。网站结构可能变化，运行后请检查 qa_report.json。中公是第三方数据源，人数统计时点可能与其它平台不同；这些人数适合做历史热度参照，不代表 2027 年实际竞争情况。

来源入口：
- https://zhiwei.offcn.com/gj/2021/bmall7905.html
- https://zhiwei.offcn.com/gj/2022/bmall9423_1.html
- https://zhiwei.offcn.com/gj/2023/bmall9217.html
- https://zhiwei.offcn.com/gj/2024/bmall10893.html
- https://zhiwei.offcn.com/gj/2025/bmall11865_2.html
