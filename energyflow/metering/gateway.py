"""设备网关 HTTP 客户端。

约定：
- GET /readings?from=<epoch>&until=<epoch>[&meter=<id>]，窗口左闭右开。
- 每次响应最多 limit 行；被截断时仅返回 truncated=true，没有 next 链接。
- GET /capabilities 声明 resolution_seconds、limit 与区间语义。
- 5xx 与网络瞬时错误按有限次数重试；4xx 属于不可重试的协议错误。

客户端只做传输与形状校验，不做任何完整性推断；完整性由 harvest 引擎负责。
"""
import json
import urllib.parse
import urllib.request
import urllib.error
from ..values import integer, reject, object_fields, instant


class GatewayError(Exception):
    def __init__(self, code, message, **context):
        super().__init__(message)
        self.code = code
        self.context = context

    def document(self):
        return {"code": self.code, "message": str(self), "context": self.context}


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def normalize_row(row):
    if not isinstance(row, dict):
        raise GatewayError("protocol", "reading item must be an object")
    object_fields(
        row,
        {"id", "segment", "meter", "at", "value", "revision", "kind", "note"},
        {"id", "segment", "at", "value", "revision"},
    )
    if not isinstance(row["id"], str) or not row["id"]:
        raise GatewayError("protocol", "reading id missing")
    item = {
        "id": row["id"],
        "segment": row["segment"],
        "meter": row.get("meter"),
        "at": instant(row["at"]),
        "value": integer(row["value"], 0, 10**12),
        "revision": integer(row["revision"], 1, 10**9),
        "kind": row.get("kind", "normal"),
        "note": row.get("note", ""),
    }
    if item["kind"] not in {"normal", "rollover", "missing"}:
        raise GatewayError("protocol", "unknown reading kind")
    return item


class GatewayClient:
    def __init__(
        self,
        base_url,
        timeout=5,
        fetch_retries=2,
        opener=None,
        clock=None,
    ):
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            reject("input", "gateway base url must be http(s)")
        integer(timeout, 1, 300)
        integer(fetch_retries, 0, 5)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.fetch_retries = fetch_retries
        self.opener = opener or urllib.request.build_opener()
        self.clock = clock
        self._capabilities = None

    def _open(self, url):
        last = None
        for attempt in range(self.fetch_retries + 1):
            try:
                request = urllib.request.Request(url, headers={"Accept": "application/json"})
                with self.opener.open(request, timeout=self.timeout) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in RETRYABLE_STATUS or attempt == self.fetch_retries:
                    raise GatewayError(
                        "upstream",
                        "gateway rejected request",
                        status=exc.code,
                        attempts=attempt + 1,
                    ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt == self.fetch_retries:
                    raise GatewayError(
                        "unreachable",
                        "gateway request failed",
                        reason=str(exc),
                        attempts=attempt + 1,
                    ) from exc
            if self.clock is not None:
                self.clock.sleep(min(2 ** attempt, 8))
        raise GatewayError("unreachable", "gateway request failed", reason=str(last))

    def _json(self, path, query):
        url = self.base_url + path + "?" + urllib.parse.urlencode(query)
        status, raw = self._open(url)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GatewayError("protocol", "gateway returned malformed json", reason=str(exc))
        return url, status, data

    def capabilities(self):
        if self._capabilities is None:
            _, _, data = self._json("/capabilities", {})
            if not isinstance(data, dict):
                raise GatewayError("protocol", "capabilities must be an object")
            resolution = integer(data.get("resolution_seconds", 1), 1, 86400)
            limit = integer(data.get("limit", 3), 1, 10000)
            interval = data.get("interval", "[from,until)")
            if interval != "[from,until)":
                raise GatewayError("protocol", "gateway interval semantics differ", interval=interval)
            self._capabilities = {
                "resolution_seconds": resolution,
                "limit": limit,
                "interval": interval,
            }
        return dict(self._capabilities)

    def fetch(self, meter, start, end):
        """单次窗口请求。返回标准化响应，不做任何拆分。"""
        integer(start, 0)
        integer(end, 0)
        if end <= start:
            reject("time", "gateway window must be a non-empty half-open interval")
        query = {"from": start, "until": end, "meter": meter}
        url, status, data = self._json("/readings", query)
        if not isinstance(data, dict) or "items" not in data or "truncated" not in data:
            raise GatewayError("protocol", "gateway response shape differs")
        if not isinstance(data["items"], list) or not isinstance(data["truncated"], bool):
            raise GatewayError("protocol", "gateway response shape differs")
        rows = []
        for row in data["items"]:
            rows.append(normalize_row(row))
        return {
            "url": url,
            "status": status,
            "items": rows,
            "truncated": data["truncated"],
            "count": len(rows),
        }


class ScriptedGateway:
    """进程内测试替身：按 (meter, 窗口) 脚本响应，模拟截断、故障与交错修订。

    每个 meter 对应一个 dataset（带 at/revision 的行列表）；故障脚本把
    (from,until) 映射到 GatewayError 或 (items, truncated) 覆盖响应。
    """

    def __init__(self, datasets, capabilities=None, failures=None, revisions=None):
        self.datasets = {meter: list(rows) for meter, rows in datasets.items()}
        self._capabilities = capabilities or {
            "resolution_seconds": 1,
            "limit": 3,
            "interval": "[from,until)",
        }
        self.failures = failures or {}
        self.revisions = revisions or {}
        self.request_log = []
        self.requests = 0
        self._pass_counts = {}

    def capabilities(self):
        return dict(self._capabilities)

    def _apply_revision_rule(self, key, raw):
        rule = self.revisions.get(key)
        if rule is None:
            return raw
        if isinstance(rule, dict) and "passes" in rule:
            seen = self._pass_counts.get(key, 0)
            self._pass_counts[key] = seen + 1
            override = {}
            for index, pass_map in enumerate(rule["passes"]):
                if seen >= index:
                    override.update(pass_map)
            patched = []
            for row in raw:
                copy = dict(row)
                spec = override.get(row["id"])
                if spec is not None:
                    if isinstance(spec, dict):
                        copy.update(spec)
                    else:
                        copy["revision"] = spec
                patched.append(copy)
            return patched
        return [dict(row, revision=rule.get(row["id"], row["revision"])) for row in raw]

    def fetch(self, meter, start, end):
        integer(start, 0)
        integer(end, 0)
        key = (meter, start, end)
        self.request_log.append(key)
        self.requests += 1
        if key in self.failures:
            failure = self.failures[key]
            if isinstance(failure, GatewayError):
                raise failure
            if isinstance(failure, Exception):
                raise GatewayError("unreachable", "scripted failure", reason=str(failure))
        raw = [row for row in self.datasets.get(meter, []) if start <= row["at"] < end]
        raw = self._apply_revision_rule(key, raw)
        items = [normalize_row(dict(row, meter=row.get("meter", meter))) for row in raw]
        limit = self._capabilities["limit"]
        truncated = len(items) > limit
        return {
            "url": f"script://{meter}?from={start}&until={end}",
            "status": 200,
            "items": items[:limit],
            "truncated": truncated,
            "count": min(len(items), limit),
        }
