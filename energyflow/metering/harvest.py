"""设备读数采集引擎：递归拆分窗口直到可以证明覆盖完整。

核心命题：网关满额时只返回 truncated=true、没有 next 链接，因此单次
“本次无新行”并不足以证明区间完整。引擎把每个待补窗口当作可恢复的
持久化状态，按 capabilities 声明的最小精度递归二分：

- 响应未截断：该窗口 [start,end) 的读数集合已被完整观察到。
- 截断且窗口仍可分：按分辨率对齐的中点拆成两个左闭右开子窗口。
- 截断且窗口宽度已等于最小精度：无法再拆，窗口标记 blocked，
  连同该精度点上的同秒读数（热点）一起作为可解释缺口保留。

覆盖区间以“已完成窗口”的并集证明；pending/failed/blocked 窗口永远
不会被计作已覆盖。重启后只从仍有缺口的窗口继续；请求预算按单次
运行计数，而不是按作业累计。
"""
import time
from ..values import (
    key,
    integer,
    period,
    canonical,
    fingerprint,
    reject,
)
from ..calendar import coverage_summary, subtract
from ..values import EnergyError
from .gateway import GatewayError

WINDOW_STATES = ("queued", "fetching", "complete", "failed", "blocked")
OBSERVATION_STATES = (
    "observed", "added", "corrected", "reused", "stale", "conflict", "blocked", "disputed"
)
INGEST_OUTCOMES = OBSERVATION_STATES


def _now(clock):
    value = clock() if clock is not None else int(time.time())
    return integer(value, 0, 4133980800)


def _reason(code, message, **extra):
    payload = {"code": code, "message": message}
    payload.update(extra)
    return canonical(payload)


def _window_id(job, start, end):
    return key(job + ":w:" + str(start) + ":" + str(end))


class Harvest:
    def __init__(self, store, catalog, readings, gateway, clock=None):
        self.store = store
        self.catalog = catalog
        self.readings = readings
        self.gateway = gateway
        self.clock = clock

    # ------------------------------------------------------------------ 作业

    def create_job(
        self,
        job,
        meter,
        start,
        end,
        lookback_seconds=86400,
        request_budget=1000,
        resolution=None,
    ):
        key(job)
        self.catalog.get(meter)
        start, end = period(start, end)
        lookback = integer(lookback_seconds, 1, 366 * 86400)
        budget = integer(request_budget, 1, 10**7)
        if self.store.one("SELECT id FROM harvest_jobs WHERE id=?", (job,)):
            reject("conflict", "harvest job already exists", job=job)
        capabilities = self.gateway.capabilities()
        if resolution is None:
            resolution = capabilities["resolution_seconds"]
        resolution = integer(resolution, 1, 86400)
        if resolution % capabilities["resolution_seconds"] != 0:
            reject(
                "input",
                "resolution must be a multiple of gateway precision",
                gateway_precision=capabilities["resolution_seconds"],
            )
        limit = capabilities["limit"]
        now = _now(self.clock)
        payload = {
            "id": job,
            "meter": meter,
            "start": start,
            "end": end,
            "lookback_seconds": lookback,
            "request_budget": budget,
            "resolution_seconds": resolution,
            "gateway_limit": limit,
            "capabilities": capabilities,
        }
        with self.store.atomic() as con:
            con.execute(
                "INSERT INTO harvest_jobs VALUES(?,?,?,?,?,?,?,?,'created',?,?,?,?)",
                (
                    job, meter, start, end, resolution, limit, lookback, budget,
                    now, now, canonical(payload), fingerprint(payload),
                ),
            )
            con.execute(
                "INSERT INTO harvest_windows "
                "(id,job,meter,start,end,state,attempts,reason,truncated,row_count,"
                "request_count,updated_at,payload,digest) "
                "VALUES(?,?,?,?,?,'queued',0,'',0,0,0,?,?,?)",
                (
                    _window_id(job, start, end), job, meter, start, end, now,
                    canonical({"start": start, "end": end}),
                    fingerprint({"job": job, "start": start, "end": end}),
                ),
            )
            self.store.event("harvest_job_created", job, {"meter": meter, "start": start, "end": end})
        return self.get_job(job)

    def get_job(self, job):
        row = self.store.one("SELECT * FROM harvest_jobs WHERE id=?", (key(job),))
        if row is None:
            reject("missing", "harvest job not found", job=job)
        payload = self._json(row["payload"])
        if fingerprint(payload) != row["digest"]:
            reject("integrity", "harvest job payload changed", job=job)
        return {
            "id": job,
            "meter": row["meter"],
            "start": row["start"],
            "end": row["end"],
            "state": row["state"],
            "resolution_seconds": row["resolution"],
            "gateway_limit": row["limit_rows"],
            "lookback_seconds": row["lookback"],
            "request_budget": row["budget"],
            **payload,
        }

    def list_jobs(self, meter=None):
        if meter is None:
            rows = self.store.rows("SELECT id FROM harvest_jobs ORDER BY created_at,id")
        else:
            rows = self.store.rows(
                "SELECT id FROM harvest_jobs WHERE meter=? ORDER BY created_at,id", (meter,)
            )
        return [self.get_job(row["id"]) for row in rows]

    # ------------------------------------------------------------- 单次运行

    def run(self, job, request_budget=None):
        job = key(job)
        record = self.get_job(job)
        if record["state"] == "running":
            reject("state", "harvest job is already running", job=job)
        budget = record["request_budget"] if request_budget is None else integer(request_budget, 1, 10**7)
        self._reset_interrupted(job)
        run_seq = self._next_run_seq(job)
        now = _now(self.clock)
        run_payload = {
            "job": job,
            "run_seq": run_seq,
            "requests_used": 0,
            "budget": budget,
        }
        with self.store.atomic() as con:
            con.execute(
                "INSERT INTO harvest_runs VALUES(?,?,?,?,NULL,?,?)",
                (job, run_seq, 0, now, canonical(run_payload), fingerprint(run_payload)),
            )
            con.execute(
                "UPDATE harvest_jobs SET state='running',updated_at=? WHERE id=?", (now, job)
            )
        requests_used = 0
        try:
            while requests_used < budget:
                window = self.store.one(
                    "SELECT * FROM harvest_windows WHERE job=? AND state='queued' "
                    "ORDER BY start,id LIMIT 1",
                    (job,),
                )
                if window is None:
                    break
                self._process_window(record, window)
                requests_used += 1
                self._bump_run(job, run_seq, requests_used)
            # 预算耗尽但仍有排队窗口：本次运行 partial，缺口保留待下次续跑。
            budget_exceeded = requests_used >= budget and self._open_windows(job) > 0
            state = "complete" if not self._open_windows(job) else "partial"
        except Exception:
            self._mark_running_failed(job, run_seq, requests_used)
            raise
        finished = _now(self.clock)
        with self.store.atomic() as con:
            con.execute(
                "UPDATE harvest_jobs SET state=?,updated_at=? WHERE id=?", (state, finished, job)
            )
            run_payload.update(requests_used=requests_used, finished_at=finished,
                               final_state=state, budget_exceeded=budget_exceeded)
            con.execute(
                "UPDATE harvest_runs SET requests_used=?,finished_at=?,payload=?,digest=? "
                "WHERE job=? AND run_seq=?",
                (requests_used, finished, canonical(run_payload), fingerprint(run_payload),
                 job, run_seq),
            )
            self.store.event(
                "harvest_run_finished", job,
                {"run_seq": run_seq, "requests_used": requests_used, "state": state},
            )
        return self.report(job)

    def _next_run_seq(self, job):
        row = self.store.one(
            "SELECT COALESCE(MAX(run_seq),0)+1 AS seq FROM harvest_runs WHERE job=?", (job,)
        )
        return row["seq"]

    def _bump_run(self, job, run_seq, used):
        payload = self._json(
            self.store.one(
                "SELECT payload FROM harvest_runs WHERE job=? AND run_seq=?", (job, run_seq)
            )["payload"]
        )
        payload["requests_used"] = used
        self.store.connection.execute(
            "UPDATE harvest_runs SET requests_used=?,payload=?,digest=? WHERE job=?",
            (used, canonical(payload), fingerprint(payload), job),
        )

    def _reset_interrupted(self, job):
        """上次运行崩溃时遗留的 fetching 窗口回到排队，保证重启可续跑。"""
        now = _now(self.clock)
        with self.store.atomic():
            rows = self.store.rows(
                "SELECT id FROM harvest_windows WHERE job=? AND state='fetching'", (job,)
            )
            for row in rows:
                self.store.connection.execute(
                    "UPDATE harvest_windows SET state='queued',reason=?,updated_at=? WHERE id=?",
                    (_reason("interrupted", "response was never recorded"), now, row["id"]),
                )
            if rows:
                self.store.connection.execute(
                    "UPDATE harvest_jobs SET state='interrupted',updated_at=? WHERE id=?", (now, job)
                )

    def _open_windows(self, job):
        return self.store.one(
            "SELECT COUNT(*) AS n FROM harvest_windows WHERE job=? AND state IN ('queued','fetching','failed')",
            (job,),
        )["n"]

    def _mark_running_failed(self, job, run_seq, used):
        now = _now(self.clock)
        with self.store.atomic():
            self.store.connection.execute(
                "UPDATE harvest_jobs SET state='failed',updated_at=? WHERE id=?", (now, job)
            )
            row = self.store.one(
                "SELECT payload FROM harvest_runs WHERE job=? AND run_seq=?", (job, run_seq)
            )
            if row:
                payload = self._json(row["payload"])
                payload.update(requests_used=used, finished_at=now, final_state="failed")
                self.store.connection.execute(
                    "UPDATE harvest_runs SET requests_used=?,finished_at=?,payload=?,digest=? "
                    "WHERE job=? AND run_seq=?",
                    (used, now, canonical(payload), fingerprint(payload), job, run_seq),
                )

    # ------------------------------------------------------------- 窗口状态机

    def _process_window(self, record, window):
        job = record["id"]
        meter = record["meter"]
        start, end = window["start"], window["end"]
        now = _now(self.clock)
        with self.store.atomic():
            self.store.connection.execute(
                "UPDATE harvest_windows SET state='fetching',attempts=attempts+1,"
                "request_count=request_count+1,updated_at=? WHERE id=?",
                (now, window["id"]),
            )
        try:
            response = self.gateway.fetch(meter, start, end)
        except GatewayError as exc:
            with self.store.atomic():
                self.store.connection.execute(
                    "UPDATE harvest_windows SET state='failed',reason=?,updated_at=? WHERE id=?",
                    (canonical(exc.document()), now, window["id"]),
                )
                self.store.event(
                    "harvest_window_failed", window["id"],
                    {"job": job, "start": start, "end": end, "error": exc.document()},
                )
            return "failed"
        return self._record_response(record, window, response, now)

    def _record_response(self, record, window, response, now):
        job = record["id"]
        meter = record["meter"]
        start, end = window["start"], window["end"]
        resolution = record["resolution_seconds"]
        rows = response["items"]
        truncated = response["truncated"]
        out_of_window = [row["id"] for row in rows if not (start <= row["at"] < end)]
        if out_of_window:
            reject("protocol", "gateway returned rows outside requested window",
                   window=[start, end], ids=out_of_window)
        with self.store.atomic() as con:
            for row in rows:
                self._upsert_observation(record, window, row, now)
            if not truncated:
                con.execute(
                    "UPDATE harvest_windows SET state='complete',reason='',truncated=0,"
                    "row_count=?,updated_at=? WHERE id=?",
                    (len(rows), now, window["id"]),
                )
                # 窗口曾因最小精度超限 blocked、扩容后重试成功：解除观测的 blocked 标记，
                # 清理对应热点，使其可以进入核算链路。
                con.execute(
                    "DELETE FROM harvest_hotspots WHERE job=? AND window_id=?",
                    (job, window["id"]),
                )
                if rows:
                    placeholders = ",".join("?" for _ in rows)
                    con.execute(
                        "UPDATE harvest_observations SET state='observed',detail='' "
                        "WHERE job=? AND state='blocked' AND id IN (" + placeholders + ")",
                        (job, *(row["id"] for row in rows)),
                    )
                self.store.event(
                    "harvest_window_complete", window["id"],
                    {"job": job, "start": start, "end": end, "rows": len(rows)},
                )
                return "complete"
            midpoint = start + ((end - start) // 2 // resolution) * resolution
            if midpoint <= start or midpoint >= end or (end - start) <= resolution:
                # 最小精度内仍超限：无法再拆，保留为可解释缺口。truncated=true
                # 说明至少还有一行从未被观察到，其身份与内容未知。
                blocked_reason = {
                    "code": "over_limit_at_minimum_precision",
                    "resolution_seconds": resolution,
                    "returned_rows": len(rows),
                    "gateway_limit": record["gateway_limit"],
                    "truncated": True,
                    "minimum_unobserved_rows": 1,
                    "observed_ids": sorted(row["id"] for row in rows),
                    "explanation": "窗口宽度已等于最小时间精度，无法继续二分；"
                    "truncated 表明至少还有一行读数无法观察，完整集合不可证明",
                }
                con.execute(
                    "UPDATE harvest_windows SET state='blocked',reason=?,"
                    "truncated=1,row_count=?,updated_at=? WHERE id=?",
                    (canonical(blocked_reason), len(rows), now, window["id"]),
                )
                self._record_hotspot(record, window, rows, now, con, blocked_reason)
                self.store.event(
                    "harvest_window_blocked", window["id"],
                    {"job": job, "start": start, "end": end, "at": start},
                )
                return "blocked"
            left_id = _window_id(job, start, midpoint)
            right_id = _window_id(job, midpoint, end)
            con.execute("DELETE FROM harvest_windows WHERE id=?", (window["id"],))
            for child_id, child_start, child_end in (
                (left_id, start, midpoint),
                (right_id, midpoint, end),
            ):
                payload = {"start": child_start, "end": child_end, "parent": window["id"]}
                con.execute(
                    "INSERT INTO harvest_windows "
                    "(id,job,meter,start,end,state,attempts,reason,truncated,row_count,"
                    "request_count,updated_at,payload,digest) "
                    "VALUES(?,?,?,?,?,'queued',0,'',0,0,0,?,?,?)",
                    (
                        child_id, job, meter, child_start, child_end, now,
                        canonical(payload), fingerprint({"job": job, **payload}),
                    ),
                )
            self.store.event(
                "harvest_window_split", window["id"],
                {"job": job, "start": start, "end": end, "midpoint": midpoint,
                 "rows": len(rows)},
            )
            return "split"

    def _record_hotspot(self, record, window, rows, now, con, blocked_reason):
        job = record["id"]
        by_second = {}
        for row in rows:
            by_second.setdefault(row["at"], []).append(row["id"])
        blocked_ids = set()
        for at, ident_list in sorted(by_second.items()):
            if len(ident_list) > 1:
                con.execute(
                    "INSERT OR REPLACE INTO harvest_hotspots VALUES(?,?,?,?)",
                    (
                        job, window["id"], at,
                        canonical({
                            "observed_ids": sorted(ident_list),
                            "minimum_unobserved_rows": blocked_reason[
                                "minimum_unobserved_rows"
                            ],
                            "note": "该秒至少还有一条读数被网关截断，身份未知",
                        }),
                    ),
                )
                blocked_ids.update(ident_list)
        for ident in sorted(blocked_ids):
            con.execute(
                "UPDATE harvest_observations SET state='blocked',detail=? WHERE id=? AND job=?",
                (canonical({
                    "code": "over_limit_at_minimum_precision",
                    "message": "同秒读数超过网关上限，窗口无法证明完整",
                    "at": at,
                    "minimum_unobserved_rows": blocked_reason["minimum_unobserved_rows"],
                }), ident, job),
            )

    def _upsert_observation(self, record, window, row, now):
        job = record["id"]
        payload = {
            "id": row["id"],
            "segment": row["segment"],
            "meter": row.get("meter", record["meter"]),
            "at": row["at"],
            "value": row["value"],
            "revision": row["revision"],
            "kind": row["kind"],
            "note": row["note"],
        }
        checksum = fingerprint(payload)
        existing = self.store.one(
            "SELECT revision,digest,state FROM harvest_observations WHERE id=? AND job=?",
            (row["id"], job),
        )
        if existing is None:
            self.store.connection.execute(
                "INSERT INTO harvest_observations VALUES(?,?,?,?,?,?,?,?,'observed','',?,?,?)",
                (
                    row["id"], job, record["meter"], row["segment"], row["at"],
                    row["revision"], canonical(payload), checksum,
                    window["id"], now, now,
                ),
            )
            return
        # 同一读数在不同窗口的重复边界出现：记录最后一次见到，并核对内容。
        if existing["digest"] != checksum and existing["revision"] == row["revision"]:
            # 同版内容不同：协议异常。保留首次观察到的 payload，不静默覆盖，
            # 状态置为 disputed，ingest 阶段据此拒绝入账并要求人工解释。
            self.store.connection.execute(
                "UPDATE harvest_observations SET state='disputed',detail=?,last_seen=? "
                "WHERE id=? AND job=?",
                (canonical({
                    "code": "same_revision_content_differs",
                    "window": [window["start"], window["end"]],
                    "first_digest": existing["digest"],
                    "conflicting_digest": checksum,
                    "conflicting_value": row["value"],
                }), now, row["id"], job),
            )
            return
        self.store.connection.execute(
            "UPDATE harvest_observations SET revision=?,payload=?,digest=?,last_seen=? "
            "WHERE id=? AND job=?",
            (row["revision"], canonical(payload), checksum, now, row["id"], job),
        )

    # ------------------------------------------------------------- 重试失败窗口

    def retry_failures(self, job):
        """把失败（或上次中断）窗口重新排队；blocked 窗口不会被自动重试。"""
        job = key(job)
        self.get_job(job)
        now = _now(self.clock)
        with self.store.atomic() as con:
            rows = self.store.rows(
                "SELECT id FROM harvest_windows WHERE job=? AND state IN ('failed','fetching')",
                (job,),
            )
            for row in rows:
                con.execute(
                    "UPDATE harvest_windows SET state='queued',reason=?,updated_at=? WHERE id=?",
                    (_reason("operator_retry", "requeued by operator"), now, row["id"]),
                )
            if rows:
                con.execute(
                    "UPDATE harvest_jobs SET state='created',updated_at=? WHERE id=?", (now, job)
                )
                self.store.event("harvest_retry", job, {"windows": len(rows)})
        return {"job": job, "requeued": len(rows)}

    def retry_window(self, job, window_id):
        job = key(job)
        window_id = key(window_id)
        row = self.store.one(
            "SELECT state FROM harvest_windows WHERE job=? AND id=?", (job, window_id)
        )
        if row is None:
            reject("missing", "harvest window not found", window=window_id)
        if row["state"] not in {"failed", "blocked", "fetching"}:
            reject("state", "only failed or blocked windows can be retried", state=row["state"])
        now = _now(self.clock)
        with self.store.atomic() as con:
            con.execute(
                "UPDATE harvest_windows SET state='queued',reason='',updated_at=? WHERE id=?",
                (now, window_id),
            )
            con.execute(
                "UPDATE harvest_jobs SET state='created',updated_at=? WHERE id=?", (now, job)
            )
            self.store.event("harvest_window_retry", window_id, {"job": job})
        return {"job": job, "window": window_id, "state": "queued"}

    def reobserve(self, job, start=None, end=None):
        """回看：把与 [start,end) 相交的已完成窗口重新排队，用于发现交错修订。

        已入账读数不会被删除；新一轮观察只更新观测台账，修订在 ingest 阶段
        按 revision 对账（高版更正、低版拒绝）。
        """
        job = key(job)
        record = self.get_job(job)
        if start is None:
            start, end = record["start"], record["end"]
        else:
            start, end = period(start, end)
            if start < record["start"] or end > record["end"]:
                reject("time", "reobserve range must stay inside the job window")
        now = _now(self.clock)
        with self.store.atomic() as con:
            rows = self.store.rows(
                "SELECT id FROM harvest_windows WHERE job=? AND state='complete' "
                "AND start<? AND end>? ORDER BY start",
                (job, end, start),
            )
            for row in rows:
                con.execute(
                    "UPDATE harvest_windows SET state='queued',reason=?,"
                    "truncated=0,row_count=0,updated_at=? WHERE id=?",
                    (_reason("reobserve", "operator lookback for revisions"), now, row["id"]),
                )
            if rows:
                con.execute(
                    "UPDATE harvest_jobs SET state='created',updated_at=? WHERE id=?", (now, job)
                )
                self.store.event("harvest_reobserve", job, {"windows": len(rows)})
        return {"job": job, "requeued": len(rows), "range": [start, end]}

    def lookback(self, job, span_seconds=None):
        """按作业配置的回看跨度（或显式 span）回看最近的已完成窗口。"""
        job = key(job)
        record = self.get_job(job)
        span = record["lookback_seconds"] if span_seconds is None else integer(span_seconds, 1)
        end = record["end"]
        start = max(record["start"], end - span)
        return self.reobserve(job, start, end)

    # ------------------------------------------------------------- 观测 → 核算链路

    def ingest(self, job):
        """把已证明完整的观测送入现有 readings 链路。

        修订语义：
        - 高 revision 走 readings.correct 更正；
        - 低 revision 不能覆盖新值，记 stale；
        - 同版同内容重复出现记 reused；
        - 同版内容不同记 conflict（观测保留首次 payload，不覆盖）；
        - 同 segment+at 已被其他读数占用记 conflict，不写入。
        blocked 窗口中的观测保持 blocked，绝不进入核算链路。
        """
        job = key(job)
        record = self.get_job(job)
        run_seq = self._next_run_seq(job) - 1
        summary = {outcome: 0 for outcome in INGEST_OUTCOMES}
        details = []
        rows = self.store.rows(
            "SELECT * FROM harvest_observations WHERE job=? "
            "AND state NOT IN ('blocked') ORDER BY at,id",
            (job,),
        )
        with self.store.atomic():
            for row in rows:
                if row["state"] == "disputed":
                    outcome, detail = "conflict", self._json(row["detail"])
                else:
                    outcome, detail = self._ingest_one(record, row)
                summary[outcome] += 1
                details.append({"id": row["id"], "outcome": outcome, "detail": detail})
                # 追加式台账：同一读数的每次对账（含更正链）都保留，用自增序号区分。
                sequence_row = self.store.one(
                    "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM harvest_ingest WHERE job=?",
                    (job,),
                )
                self.store.connection.execute(
                    "INSERT INTO harvest_ingest VALUES(?,?,?,?,?,?,?,?)",
                    (
                        job, sequence_row["seq"], row["id"], row["revision"],
                        detail.get("local_revision"), outcome, canonical(detail),
                        _now(self.clock),
                    ),
                )
            self.store.event("harvest_ingested", job, {**summary, "run_seq": run_seq})
        return {"job": job, "run_seq": run_seq, "summary": summary, "rows": details}

    def _ingest_one(self, record, row):
        payload = self._json(row["payload"])
        if fingerprint(payload) != row["digest"]:
            return "conflict", {"code": "observation_payload_tampered"}
        reading = {
            "id": payload["id"],
            "segment": payload["segment"],
            "at": payload["at"],
            "value": payload["value"],
            "kind": payload["kind"],
            "note": payload["note"],
        }
        # 先做全部可解释的预检，避免依赖异常做控制流。
        existing = self.store.one(
            "SELECT revision,digest,payload FROM readings WHERE id=?", (reading["id"],)
        )
        if existing is not None:
            local = self._json(existing["payload"])
            current_revision = existing["revision"]
            if row["revision"] < current_revision:
                return "stale", {
                    "code": "remote_revision_behind_local",
                    "remote_revision": row["revision"],
                    "local_revision": current_revision,
                }
            if row["revision"] == current_revision:
                if fingerprint(local) == existing["digest"] and self._same_reading(local, reading):
                    return "reused", {"code": "already_current", "local_revision": current_revision}
                return "conflict", {
                    "code": "same_revision_content_differs",
                    "remote_revision": row["revision"],
                    "local_revision": current_revision,
                }
            # 远端更高 revision：走更正链路；身份字段必须保持一致。
            if (local["segment"], local["at"]) != (reading["segment"], reading["at"]):
                return "conflict", {
                    "code": "revision_changes_identity",
                    "remote_revision": row["revision"],
                    "local_revision": current_revision,
                }
            try:
                updated = self.readings.correct(
                    reading["id"], current_revision, reading["value"],
                    reading["kind"],
                    "gateway revision " + str(row["revision"]),
                    reading["note"],
                )
            except EnergyError as exc:
                return "conflict", {"code": "correction_rejected", "error": exc.document()}
            self.store.connection.execute(
                "UPDATE harvest_observations SET state='corrected' WHERE id=? AND job=?",
                (reading["id"], record["id"]),
            )
            return "corrected", {
                "local_revision": updated["revision"],
                "remote_revision": row["revision"],
            }
        occupied = self.store.one(
            "SELECT id FROM readings WHERE segment=? AND at=?",
            (reading["segment"], reading["at"]),
        )
        if occupied is not None:
            return "conflict", {
                "code": "segment_instant_occupied",
                "existing": occupied["id"],
            }
        try:
            # 保留远端初始 revision：远端首次见到的版本即作为本地基线，
            # 后续更高 revision 才能走更正链路。
            added = self.readings.add(reading, initial_revision=row["revision"])
        except EnergyError as exc:
            return "conflict", {"code": "reading_rejected", "error": exc.document()}
        self.store.connection.execute(
            "UPDATE harvest_observations SET state='added' WHERE id=? AND job=?",
            (reading["id"], record["id"]),
        )
        return "added", {"local_revision": added["revision"], "remote_revision": row["revision"]}

    @staticmethod
    def _same_reading(local, remote):
        return all(local[name] == remote[name] for name in ("segment", "at", "value", "kind", "note"))

    # ------------------------------------------------------------- 覆盖与报告

    def coverage(self, job):
        """已覆盖区间只由 complete 窗口的并集证明。"""
        job = key(job)
        record = self.get_job(job)
        start, end = record["start"], record["end"]
        completed = [
            (row["start"], row["end"])
            for row in self.store.rows(
                "SELECT start,end FROM harvest_windows WHERE job=? AND state='complete' "
                "ORDER BY start",
                (job,),
            )
        ]
        summary = coverage_summary(start, end, completed)
        gaps = subtract(start, end, completed)
        gap_windows = []
        for left, right in gaps:
            owners = self.store.rows(
                "SELECT id,state,start,end,reason FROM harvest_windows "
                "WHERE job=? AND start<? AND end>? AND state!='complete' ORDER BY start,id",
                (job, right, left),
            )
            gap_windows.append({"start": left, "end": right, "windows": owners})
        return {
            "job": job,
            "meter": record["meter"],
            "start": start,
            "end": end,
            **summary,
            "gap_detail": gap_windows,
            "proven_complete": summary["complete"],
        }

    def report(self, job):
        job = key(job)
        record = self.get_job(job)
        window_rows = self.store.rows(
            "SELECT state,COUNT(*) AS n,COALESCE(SUM(row_count),0) AS rows,"
            "COALESCE(SUM(request_count),0) AS requests FROM harvest_windows "
            "WHERE job=? GROUP BY state",
            (job,),
        )
        windows = {state: 0 for state in WINDOW_STATES}
        window_rows_count = 0
        request_count = 0
        for row in window_rows:
            windows[row["state"]] = row["n"]
            window_rows_count += row["n"]
            request_count += row["requests"]
        observation_rows = self.store.rows(
            "SELECT state,COUNT(*) AS n FROM harvest_observations WHERE job=? GROUP BY state",
            (job,),
        )
        observations = {state: 0 for state in OBSERVATION_STATES}
        for row in observation_rows:
            observations[row["state"]] = row["n"]
        hotspots = [
            {
                "window_id": row["window_id"],
                "at": row["at"],
                "detail": self._json(row["observation_ids"]),
            }
            for row in self.store.rows(
                "SELECT * FROM harvest_hotspots WHERE job=? ORDER BY at,window_id", (job,)
            )
        ]
        blocked_windows = [
            {
                "id": row["id"],
                "start": row["start"],
                "end": row["end"],
                "reason": self._json(row["reason"]) if row["reason"] else None,
            }
            for row in self.store.rows(
                "SELECT * FROM harvest_windows WHERE job=? AND state='blocked' ORDER BY start",
                (job,),
            )
        ]
        failed_windows = [
            {
                "id": row["id"],
                "start": row["start"],
                "end": row["end"],
                "attempts": row["attempts"],
                "reason": self._json(row["reason"]) if row["reason"] else None,
            }
            for row in self.store.rows(
                "SELECT * FROM harvest_windows WHERE job=? AND state='failed' ORDER BY start",
                (job,),
            )
        ]
        runs = [
            {
                "run_seq": row["run_seq"],
                "requests_used": row["requests_used"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
            for row in self.store.rows(
                "SELECT run_seq,requests_used,started_at,finished_at FROM harvest_runs "
                "WHERE job=? ORDER BY run_seq",
                (job,),
            )
        ]
        coverage = self.coverage(job)
        return {
            "job": record,
            "state": record["state"],
            "windows": windows,
            "window_count": window_rows_count,
            "gateway_requests": request_count,
            "observations": observations,
            "hotspots": hotspots,
            "blocked_windows": blocked_windows,
            "failed_windows": failed_windows,
            "runs": runs,
            "coverage": coverage,
            "proven_complete": coverage["proven_complete"],
        }

    def observations(self, job, state=None):
        job = key(job)
        self.get_job(job)
        if state is None:
            rows = self.store.rows(
                "SELECT * FROM harvest_observations WHERE job=? ORDER BY at,id", (job,)
            )
        else:
            if state not in OBSERVATION_STATES:
                reject("input", "unknown observation state", state=state)
            rows = self.store.rows(
                "SELECT * FROM harvest_observations WHERE job=? AND state=? ORDER BY at,id",
                (job, state),
            )
        result = []
        for row in rows:
            result.append({
                "id": row["id"],
                "segment": row["segment"],
                "at": row["at"],
                "revision": row["revision"],
                "state": row["state"],
                "detail": self._json(row["detail"]) if row["detail"] else None,
                "payload": self._json(row["payload"]),
                "first_window": row["first_window"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            })
        return result

    def windows(self, job, state=None):
        job = key(job)
        self.get_job(job)
        query = "SELECT * FROM harvest_windows WHERE job=?"
        arguments = [job]
        if state is not None:
            if state not in WINDOW_STATES:
                reject("input", "unknown window state", state=state)
            query += " AND state=?"
            arguments.append(state)
        query += " ORDER BY start,id"
        result = []
        for row in self.store.rows(query, arguments):
            result.append({
                "id": row["id"],
                "start": row["start"],
                "end": row["end"],
                "state": row["state"],
                "attempts": row["attempts"],
                "truncated": bool(row["truncated"]),
                "row_count": row["row_count"],
                "request_count": row["request_count"],
                "reason": self._json(row["reason"]) if row["reason"] else None,
            })
        return result

    def ingest_log(self, job):
        job = key(job)
        self.get_job(job)
        return [
            {
                "seq": row["seq"],
                "observation_id": row["observation_id"],
                "remote_revision": row["remote_revision"],
                "local_revision": row["local_revision"],
                "outcome": row["outcome"],
                "detail": self._json(row["detail"]),
                "at": row["at"],
            }
            for row in self.store.rows(
                "SELECT * FROM harvest_ingest WHERE job=? ORDER BY seq,observation_id", (job,)
            )
        ]

    def verify_integrity(self, job):
        """采集台账专用完整性校验。

        - 每条观测的 payload 与其 digest 一致；
        - 每个窗口都落在作业区间内且保持左闭右开；
        - 已完成窗口互不重叠（同一点的同秒热点由 blocked 窗口承载，不计入完成并集）；
        - blocked 窗口必须能在热点表中找到对应记录。
        """
        job = key(job)
        record = self.get_job(job)
        findings = []
        for row in self.store.rows(
            "SELECT * FROM harvest_observations WHERE job=?", (job,)
        ):
            payload = self._json(row["payload"])
            if fingerprint(payload) != row["digest"]:
                findings.append({"code": "observation_digest_mismatch", "id": row["id"]})
        windows = self.store.rows(
            "SELECT * FROM harvest_windows WHERE job=? ORDER BY start,end", (job,)
        )
        completed = []
        for row in windows:
            if row["start"] < record["start"] or row["end"] > record["end"]:
                findings.append({"code": "window_outside_job", "id": row["id"]})
            if row["end"] <= row["start"]:
                findings.append({"code": "window_not_half_open", "id": row["id"]})
            if row["state"] == "complete":
                completed.append((row["start"], row["end"]))
            if row["state"] == "blocked":
                hotspot = self.store.one(
                    "SELECT 1 FROM harvest_hotspots WHERE job=? AND window_id=?",
                    (job, row["id"]),
                )
                if hotspot is None:
                    findings.append({"code": "blocked_window_without_hotspot", "id": row["id"]})
        cursor = record["start"]
        for left, right in sorted(completed):
            if left < cursor:
                findings.append({"code": "completed_windows_overlap", "at": left})
            cursor = max(cursor, right)
        return {
            "job": job,
            "verified": not findings,
            "findings": findings,
            "window_count": len(windows),
            "observation_count": self.store.one(
                "SELECT COUNT(*) AS n FROM harvest_observations WHERE job=?", (job,)
            )["n"],
        }

    @staticmethod
    def _json(content):
        from ..values import parse_json

        return parse_json(content)
