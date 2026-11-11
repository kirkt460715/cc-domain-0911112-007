# energyflow 数据接入工作台

保留现有领域校验、持久化与操作入口。Python 3.11 标准库和 SQLite；运行时无需下载依赖。测试覆盖已有领域行为、错误路径和恢复。本地联调材料不会自动采集到业务库。

## 本地运行

    python3 -m compileall -q energyflow
    python3 -m unittest discover -s tests -v
    python3 -m energyflow demo
    python3 -m synthetic_exchange.smoke

## 容器运行

    docker build -t local/energyflow .
    docker run --rm --network none local/energyflow

构建时准备基础镜像，正式运行不下载。run_checks.sh 是完整的测试、编译检查及实际产品演示入口，正常在 90 秒内结束。每次演示使用临时数据或内存库，不改写源样本。

## 采集联调

GET /readings?from=0&until=100。窗口左闭右开，最多三行，truncated 表明尚有省略数据。同秒热点在 100 秒；/capabilities 给出接口精度。返回的是远端读数 revision，不是本地接收次序。

    python3 -m synthetic_exchange.server --port 8765

网关满额只给 truncated=true、没有 next 链接，单次“无新行”不能证明区间完整。
采集引擎（energyflow/metering/harvest.py）按表计和时间范围递归二分窗口，
直到每个窗口都能证明覆盖完整，再把观测送入现有 readings 核算链路：

- 未截断窗口计入已覆盖；截断窗口按 capabilities 最小精度继续拆分。
- 最小精度内仍超限：窗口标记 blocked，记录同秒热点与“至少还有 N 行未观察”，
  该秒读数绝不进入核算链路，缺口可解释且可精确定位。
- 已覆盖区间与仍有缺口（queued/failed/blocked）分别保留；重启只从待补窗口续跑。
- 请求预算按单次运行计数；预算耗尽作业保持 partial，下一次运行接着跑。
- 高 revision 走更正，低版不能覆盖新值（stale），同版内容不同记 conflict/disputed。
- 冻结结算保持原样；后续修订通过 closing.drift 查询漂移，不回写冻结快照。

运维操作（动作前缀 harvest.*，可通过 `python3 -m energyflow harvest <request> --gateway <url>` 调用）：

- create_job / run：建立并运行采集作业，可设回看跨度 lookback_seconds 与 request_budget。
- coverage / report：查看已覆盖、缺口、blocked 窗口、热点与每次运行的请求预算用量。
- retry_failures / retry_window：重试失败或被阻塞的窗口。
- reobserve：按时间范围回看已完成窗口，发现交错修订（不删除已入账读数）。
- ingest / ingest_log / observations / windows：对账入账并查询每条读数的处置轨迹。

一键端到端演练（同秒超限、交错修订、503 中断恢复、冻结漂移）：

    python3 -m energyflow harvest-drill

原有操作参数可通过 python3 -m energyflow --help 查询。
