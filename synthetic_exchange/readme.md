# 本地协议服务

GET /readings?from=0&until=100。窗口左闭右开，最多三行，truncated 表明尚有省略数据。同秒热点在 100 秒；/capabilities 给出接口精度。返回的是远端读数 revision，不是本地接收次序。

场景是响应脚本，不包含采集客户端。可以复制场景文件并改变响应次序、状态码或样本内容来联调。服务只监听 loopback，数据均为合成。

    python3 -m synthetic_exchange.server --port 8765
    python3 -m synthetic_exchange.smoke
