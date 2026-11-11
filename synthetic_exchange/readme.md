# 本地协议服务

GET /readings?from=0&until=100。窗口左闭右开，最多三行，truncated 表明尚有省略数据。同秒热点在 100 秒；/capabilities 给出接口精度。返回的是远端读数 revision，不是本地接收次序。

支持 meter 过滤（?meter=<id>，按读数的 meter 字段或 segment 匹配）。
reading_script 用于制造联调条件：

- failures：把 `meter:from:until` 映射到故障响应；repeat 取 once（默认，仅首次）、
  times（前 N 次）或 always。
- revisions：把同一窗口键映射到修订脚本。简单形式直接改 revision；
  passes 形式按请求次数递进，例如
  `{"passes": [{}, {"r1b": {"revision": 2, "value": 63}}]}` 表示首次保持原值，
  之后该读数以 revision 2 和新值返回，用于制造交错修订。

场景是响应脚本，不包含采集客户端。可以复制场景文件并改变响应次序、状态码或样本内容来联调。服务只监听 loopback，数据均为合成。

    python3 -m synthetic_exchange.server --port 8765
    python3 -m synthetic_exchange.smoke
    python3 -m energyflow harvest-drill

drill_scenario.json 配套 harvest-drill：同时制造 503 中断恢复、同秒超限热点与交错修订，
输出同时给出已入账读数和无法证明完整的缺口。
