import unittest
import tempfile
import threading
import json
from pathlib import Path

from energyflow.service import EnergyService
from energyflow.values import EnergyError, canonical
from energyflow.metering.gateway import GatewayClient, GatewayError, ScriptedGateway
from energyflow.metering.drill import run_drill
from synthetic_exchange.server import make_server


def reading(ident, at, value, revision=1, segment="seg", meter="m", **extra):
    row = {
        "id": ident,
        "segment": segment,
        "meter": meter,
        "at": at,
        "value": value,
        "revision": revision,
    }
    row.update(extra)
    return row


class HarvestEngineTests(unittest.TestCase):
    def setUp(self):
        self.service = EnergyService()
        self.addCleanup(self.service.close)
        self.service.catalog.register("m", "Synthetic meter")
        self.service.catalog.install("seg", "m", 0, 0, max_gap=7200)

    def gateway(self, rows, **kwargs):
        return ScriptedGateway({"m": rows}, **kwargs)

    def attach(self, gateway):
        self.service.attach_gateway(gateway)
        return self.service.harvest

    def test_unsplit_window_is_proven_complete_within_limit(self):
        gateway = self.gateway([
            reading("a", 10, 1), reading("b", 20, 2), reading("c", 30, 3),
        ])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=20)
        report = harvest.run("job")
        self.assertTrue(report["proven_complete"])
        self.assertEqual(report["windows"]["complete"], 1)
        self.assertEqual(report["coverage"]["missing_seconds"], 0)
        self.assertEqual(gateway.requests, 1)

    def test_truncated_window_recursively_splits_until_complete(self):
        # 12 条读数，上限 3，需要多轮二分才能全部证明。
        rows = [reading("r%02d" % i, i * 5, i) for i in range(12)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=200)
        report = harvest.run("job")
        self.assertTrue(report["proven_complete"])
        # 没有任何窗口仍处于打开状态。
        self.assertEqual(report["windows"].get("queued", 0), 0)
        self.assertEqual(report["windows"].get("failed", 0), 0)
        observed = {o["id"] for o in harvest.observations("job")}
        self.assertEqual(observed, {row["id"] for row in rows})
        # 拆分得到的子窗口都遵守左闭右开，互不重叠且并集覆盖原区间。
        windows = harvest.windows("job", "complete")
        spans = sorted((w["start"], w["end"]) for w in windows)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], 100)
        for (_, right), (left2, _) in zip(spans, spans[1:]):
            self.assertEqual(right, left2)

    def test_empty_response_proves_nothing_without_completed_window(self):
        # 空响应且未截断：该窗口确实完整（窗口内无读数）。
        gateway = self.gateway([])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        report = harvest.run("job")
        self.assertTrue(report["proven_complete"])
        self.assertEqual(report["observations"]["observed"], 0)
        # “本次无新行”仍然是一个已完成窗口，覆盖被证明。
        self.assertEqual(report["coverage"]["covered_seconds"], 100)

    def test_over_limit_at_minimum_precision_blocks_with_explanation(self):
        # 同一秒 5 条，精度 1 秒，无法再拆。
        rows = [reading("h%d" % i, 50, i, segment="seg") for i in range(5)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=200)
        report = harvest.run("job")
        self.assertFalse(report["proven_complete"])
        self.assertEqual(len(report["blocked_windows"]), 1)
        blocked = report["blocked_windows"][0]
        self.assertEqual(blocked["start"], 50)
        self.assertEqual(blocked["end"], 51)
        self.assertEqual(blocked["reason"]["code"], "over_limit_at_minimum_precision")
        self.assertGreaterEqual(blocked["reason"]["minimum_unobserved_rows"], 1)
        # 缺口被精确定位到那一秒。
        self.assertEqual(report["coverage"]["gaps"], [{"start": 50, "end": 51}])
        # 被观察到的同秒读数标记 blocked，绝不进入核算链路。
        blocked_states = {o["id"]: o["state"] for o in harvest.observations("job")}
        self.assertTrue(all(state == "blocked" for state in blocked_states.values()))
        ingest = harvest.ingest("job")
        self.assertEqual(sum(ingest["summary"].values()), 0)

    def test_splitting_terminates_and_respects_request_budget(self):
        rows = [reading("r%02d" % i, i, i) for i in range(30)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 60, request_budget=4)
        report = harvest.run("job")
        # 预算耗尽时作业保持 partial，仍有排队窗口，但不会无限拆。
        self.assertEqual(report["state"], "partial")
        self.assertGreater(report["windows"].get("queued", 0), 0)
        total_requests = sum(
            w["request_count"] for w in harvest.windows("job")
        )
        self.assertLessEqual(total_requests, 4)
        self.assertFalse(report["proven_complete"])
        # 预算按单次运行计数：下一次运行从待补窗口接着跑。
        report2 = harvest.run("job", request_budget=200)
        self.assertTrue(report2["proven_complete"])
        self.assertEqual(len(report2["runs"]), 2)

    def test_failed_window_is_retryable_and_does_not_poison_coverage(self):
        rows = [reading("a", 10, 1), reading("b", 70, 2)]
        gateway = self.gateway(rows)
        # 第一个大窗口请求失败。
        gateway.failures[("m", 0, 100)] = GatewayError("upstream", "503", status=503)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=50)
        first = harvest.run("job")
        self.assertEqual(first["state"], "partial")
        failed = harvest.windows("job", "failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"]["code"], "upstream")
        # 失败窗口不能算作覆盖。
        self.assertFalse(first["proven_complete"])
        # 清除故障并重试，作业收敛。
        gateway.failures.clear()
        harvest.retry_failures("job")
        second = harvest.run("job")
        self.assertTrue(second["proven_complete"])

    def test_restart_resumes_from_pending_windows(self):
        rows = [reading("r%02d" % i, i * 3, i) for i in range(10)]
        gateway = self.gateway(rows)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "harvest.db"
            with EnergyService(path) as service:
                service.catalog.register("m", "Synthetic meter")
                service.catalog.install("seg", "m", 0, 0, max_gap=7200)
                service.attach_gateway(gateway)
                service.harvest.create_job("job", "m", 0, 60, request_budget=2)
                partial = service.harvest.run("job")
                self.assertFalse(partial["proven_complete"])
            # 重新打开服务，作业与待补窗口都从持久化状态恢复。
            with EnergyService(path) as restarted:
                restarted.attach_gateway(gateway)
                harvest = restarted.harvest
                self.assertEqual(harvest.get_job("job")["state"], "partial")
                report = harvest.run("job", request_budget=200)
                self.assertTrue(report["proven_complete"])

    def test_interrupted_fetching_window_is_recovered_on_restart(self):
        rows = [reading("a", 10, 1)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        # 手工把窗口置为 fetching，模拟进程在收到响应前崩溃。
        harvest.store.connection.execute(
            "UPDATE harvest_windows SET state='fetching' WHERE job='job'"
        )
        report = harvest.run("job")
        self.assertTrue(report["proven_complete"])
        self.assertEqual(harvest.windows("job", "complete")[0]["attempts"], 1)

    def test_duplicate_boundary_rows_are_deduped_in_observation_ledger(self):
        # 同一读数会在相邻窗口的边界重复出现（左闭右开下 at 属于右侧窗口，
        # 但拆分树中不同层级会重复观察）。台账按 id 只保留一条并记录最后见到。
        rows = [reading("a", 10, 1), reading("b", 20, 2), reading("c", 30, 3),
                reading("d", 40, 4)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=200)
        harvest.run("job")
        ids = [o["id"] for o in harvest.observations("job")]
        self.assertEqual(sorted(ids), sorted(["a", "b", "c", "d"]))

    def test_out_of_order_rows_are_ingested_sorted_by_time(self):
        gateway = self.gateway([
            reading("late", 60, 6), reading("early", 20, 2), reading("mid", 40, 4),
        ])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=100)
        harvest.run("job")
        harvest.ingest("job")
        stored = self.service.readings.segment("seg")
        self.assertEqual([row["at"] for row in stored], [20, 40, 60])

    def test_higher_revision_corrects_lower_revision_is_stale(self):
        gateway = self.gateway([reading("r", 30, 10, revision=1)])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        harvest.run("job")
        first = harvest.ingest("job")
        self.assertEqual(first["summary"]["added"], 1)
        self.assertEqual(self.service.readings.get("r")["revision"], 1)
        # 远端以更高 revision 给出新值。
        gateway.revisions[("m", 0, 100)] = {"r": 2}
        gateway.datasets["m"][0] = reading("r", 30, 12, revision=2)
        harvest.reobserve("job")
        harvest.run("job")
        second = harvest.ingest("job")
        self.assertEqual(second["summary"]["corrected"], 1)
        self.assertEqual(self.service.readings.get("r")["value"], 12)
        self.assertEqual(self.service.readings.get("r")["revision"], 2)
        # 低版本不能覆盖新值：再把远端降回 rev1。
        gateway.revisions[("m", 0, 100)] = {"r": 1}
        gateway.datasets["m"][0] = reading("r", 30, 10, revision=1)
        harvest.reobserve("job")
        harvest.run("job")
        third = harvest.ingest("job")
        self.assertEqual(third["summary"]["stale"], 1)
        self.assertEqual(self.service.readings.get("r")["value"], 12)

    def test_same_revision_with_different_content_is_conflict_not_overwrite(self):
        gateway = self.gateway([reading("r", 30, 10, revision=1)])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        harvest.run("job")
        harvest.ingest("job")
        # 同 revision 但内容变化：标记冲突，本地值保持不变。
        gateway.datasets["m"][0] = reading("r", 30, 99, revision=1)
        harvest.reobserve("job")
        harvest.run("job")
        result = harvest.ingest("job")
        self.assertEqual(result["summary"]["conflict"], 1)
        self.assertEqual(self.service.readings.get("r")["value"], 10)

    def test_ingest_feeds_accounting_chain_and_reports_unprovable_part(self):
        gateway = self.gateway([
            reading("a", 30, 10), reading("b", 60, 30), reading("c", 90, 40),
        ])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=100)
        harvest.run("job")
        harvest.ingest("job")
        report = self.service.reconstruction.reconstruct("m", 0, 100)
        # 读数链覆盖 0..90，最后一段 90..100 无读数，核算明确报告不完整。
        self.assertFalse(report["complete"])
        self.assertEqual(report["coverage"]["missing_seconds"], 10)
        self.assertEqual(report["microkwh"], "40000000/1")

    def test_lookback_reobserve_detects_revision_without_deleting_rows(self):
        gateway = self.gateway([reading("r", 30, 10, revision=1)])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        harvest.run("job")
        harvest.ingest("job")
        gateway.datasets["m"][0] = reading("r", 30, 11, revision=2)
        gateway.revisions[("m", 0, 100)] = {"r": 2}
        result = harvest.reobserve("job", 0, 50)
        self.assertEqual(result["requeued"], 1)
        harvest.run("job")
        # 回看期间本地读数仍在。
        self.assertEqual(self.service.readings.get("r")["value"], 10)
        ingest = harvest.ingest("job")
        self.assertEqual(ingest["summary"]["corrected"], 1)

    def test_reobserve_rejects_range_outside_job(self):
        gateway = self.gateway([reading("r", 30, 10)])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        harvest.run("job")
        with self.assertRaises(EnergyError):
            harvest.reobserve("job", 50, 200)

    def test_lookback_uses_configured_span(self):
        gateway = self.gateway([
            reading("a", 10, 1), reading("b", 80, 2), reading("c", 90, 3),
        ])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, lookback_seconds=30)
        harvest.run("job")
        # 回看跨度 30 秒，只应重新排队与 [70,100) 相交的完成窗口。
        result = harvest.lookback("job")
        self.assertEqual(result["range"], [70, 100])
        self.assertGreaterEqual(result["requeued"], 1)
        for window in harvest.windows("job", "queued"):
            self.assertTrue(window["end"] > 70 and window["start"] < 100)

    def test_resolution_must_align_with_gateway_precision(self):
        gateway = self.gateway([], capabilities={
            "resolution_seconds": 2, "limit": 3, "interval": "[from,until)"
        })
        harvest = self.attach(gateway)
        with self.assertRaises(EnergyError):
            harvest.create_job("job", "m", 0, 100, resolution=3)

    def test_coarse_resolution_blocks_at_resolution_boundary(self):
        # 网关精度 2 秒；同一 2 秒窗口内 4 条读数（at 落在 10..11），
        # 窗口拆到宽度 2 秒即不可再分。
        gateway = self.gateway(
            [reading("h%d" % i, 10 + (i % 2), i) for i in range(4)],
            capabilities={"resolution_seconds": 2, "limit": 3,
                          "interval": "[from,until)"},
        )
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 20, request_budget=200)
        report = harvest.run("job")
        blocked = report["blocked_windows"]
        self.assertTrue(blocked)
        # 所有 blocked 窗口宽度都等于分辨率，不会无限拆。
        for window in blocked:
            self.assertEqual(window["end"] - window["start"], 2)
        self.assertFalse(report["proven_complete"])

    def test_split_midpoint_aligns_to_resolution_grid(self):
        # 奇数宽度窗口在精度 2 秒下，中点必须对齐到偶数秒网格。
        gateway = self.gateway(
            [reading("r%02d" % i, i, i) for i in range(8)],
            capabilities={"resolution_seconds": 2, "limit": 2,
                          "interval": "[from,until)"},
        )
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 21, request_budget=400)
        report = harvest.run("job")
        # 拆分产生的内部边界（既不是作业起点也不是作业终点）必须落在分辨率网格上。
        job_start, job_end = 0, 21
        for window in harvest.windows("job"):
            for boundary in (window["start"], window["end"]):
                if boundary not in (job_start, job_end):
                    self.assertEqual(boundary % 2, 0)
        self.assertTrue(report["proven_complete"])

    def test_retry_blocked_window_allows_manual_recheck(self):
        rows = [reading("h%d" % i, 50, i) for i in range(5)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=200)
        harvest.run("job")
        blocked = harvest.windows("job", "blocked")
        self.assertEqual(len(blocked), 1)
        # 运维显式重试 blocked 窗口（例如网关扩容后）。
        harvest.retry_window("job", blocked[0]["id"])
        self.assertEqual(harvest.windows("job", "queued")[0]["state"], "queued")

    def test_pass_based_interleaved_revision_corrects_through_ledger(self):
        gateway = self.gateway([reading("r", 30, 10, revision=1)])
        # 第一次请求窗口保持 rev1，之后以 rev2 + 新值返回。
        gateway.revisions[("m", 0, 100)] = {
            "passes": [{}, {"r": {"revision": 2, "value": 14}}]
        }
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        harvest.run("job")
        self.assertEqual(harvest.ingest("job")["summary"]["added"], 1)
        harvest.reobserve("job")
        harvest.run("job")
        second = harvest.ingest("job")
        self.assertEqual(second["summary"]["corrected"], 1)
        self.assertEqual(self.service.readings.get("r")["revision"], 2)
        self.assertEqual(self.service.readings.get("r")["value"], 14)
        # 更正链在 reading_history 中可追溯。
        self.assertEqual(
            [row["revision"] for row in self.service.readings.history("r")], [1, 2]
        )

    def test_budget_exhausted_run_is_explicitly_flagged(self):
        rows = [reading("r%02d" % i, i, i) for i in range(30)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 60, request_budget=3)
        harvest.run("job")
        last_run = harvest.report("job")["runs"][-1]
        self.assertEqual(last_run["requests_used"], 3)
        self.assertEqual(harvest.get_job("job")["state"], "partial")

    def test_verify_integrity_detects_tampered_observation(self):
        gateway = self.gateway([
            reading("a", 10, 1), reading("b", 20, 2), reading("c", 30, 3),
        ])
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100)
        harvest.run("job")
        self.assertTrue(harvest.verify_integrity("job")["verified"])
        # 篡改观测摘要而不改 payload，专用完整性校验必须发现。
        self.service.store.connection.execute(
            "UPDATE harvest_observations SET digest='bad' WHERE id='a'"
        )
        result = harvest.verify_integrity("job")
        self.assertFalse(result["verified"])
        self.assertIn("observation_digest_mismatch",
                      [item["code"] for item in result["findings"]])
        # 被篡改的观测入账时拒绝。
        ingest = harvest.ingest("job")
        self.assertEqual(ingest["summary"]["conflict"], 1)

    def test_blocked_window_carries_hotspot_integrity_evidence(self):
        rows = [reading("h%d" % i, 50, i) for i in range(5)]
        gateway = self.gateway(rows)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=200)
        harvest.run("job")
        result = harvest.verify_integrity("job")
        self.assertTrue(result["verified"])
        self.assertEqual(result["window_count"], harvest.report("job")["window_count"])

    def test_blocked_window_recovers_after_gateway_limit_increase(self):
        # 同秒 5 条、上限 3 导致最小精度窗口 blocked；网关扩容到 10 后重试，
        # harvest 层阻塞解除并可证明覆盖。
        hotspot = [reading("k%d" % i, 80, i) for i in range(5)]
        gateway = self.gateway(hotspot)
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=200)
        harvest.run("job")
        self.assertEqual(len(harvest.windows("job", "blocked")), 1)
        gateway._capabilities["limit"] = 10
        blocked = harvest.windows("job", "blocked")[0]
        harvest.retry_window("job", blocked["id"])
        report = harvest.run("job")
        self.assertTrue(report["proven_complete"])
        self.assertEqual(harvest.windows("job", "blocked"), [])
        self.assertEqual(
            {o["state"] for o in harvest.observations("job")}, {"observed"}
        )

    def test_same_second_readings_remain_uningestable_by_domain_constraint(self):
        # 即使网关扩容让同秒 5 条全部可观察，本地 readings 的 UNIQUE(segment,at)
        # 仍只接受一条；其余明确记 conflict，不会被静默丢弃。
        rows = [reading("k%d" % i, 80, i) for i in range(5)]
        gateway = self.gateway(rows, capabilities={
            "resolution_seconds": 1, "limit": 10, "interval": "[from,until)"
        })
        harvest = self.attach(gateway)
        harvest.create_job("job", "m", 0, 100, request_budget=200)
        harvest.run("job")
        self.assertTrue(harvest.report("job")["proven_complete"])
        ingest = harvest.ingest("job")
        self.assertEqual(ingest["summary"]["added"], 1)
        self.assertEqual(ingest["summary"]["conflict"], 4)
        conflicts = [r for r in ingest["rows"] if r["outcome"] == "conflict"]
        self.assertTrue(
            all(r["detail"]["code"] == "segment_instant_occupied" for r in conflicts)
        )


class GatewayClientTests(unittest.TestCase):
    def test_http_client_understands_half_open_window_and_truncation(self):
        scenario = {
            "limit": 3,
            "readings": [
                reading("a", 10, 1, meter="m", segment="seg"),
                reading("b", 20, 2, meter="m", segment="seg"),
                reading("c", 30, 3, meter="m", segment="seg"),
                reading("d", 40, 4, meter="m", segment="seg"),
            ],
            "routes": [
                {"path": "/capabilities", "responses": [
                    {"json": {"resolution_seconds": 1, "limit": 3,
                              "interval": "[from,until)"}}]}
            ],
        }
        server = make_server(scenario, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            client = GatewayClient(base, timeout=3, fetch_retries=0)
            self.assertEqual(client.capabilities()["limit"], 3)
            response = client.fetch("m", 0, 100)
            self.assertTrue(response["truncated"])
            self.assertEqual(response["count"], 3)
            self.assertEqual(response["items"][0]["at"], 10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_http_client_retries_then_surfaces_upstream_error(self):
        scenario = {
            "limit": 3,
            "readings": [],
            "reading_script": {
                "failures": {"m:0:100": {"status": 503, "repeat": "always"}}
            },
            "routes": [
                {"path": "/capabilities", "responses": [
                    {"json": {"resolution_seconds": 1, "limit": 3,
                              "interval": "[from,until)"}}]}
            ],
        }
        server = make_server(scenario, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            client = GatewayClient(base, timeout=3, fetch_retries=1)
            with self.assertRaises(GatewayError) as caught:
                client.fetch("m", 0, 100)
            self.assertEqual(caught.exception.code, "upstream")
            self.assertEqual(caught.exception.context["status"], 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class DrillScenarioTests(unittest.TestCase):
    def test_drill_reports_both_readings_and_unprovable_gaps(self):
        result = run_drill()
        # 主表计完整覆盖并完成更正与冻结漂移演示。
        self.assertTrue(result["main_job"]["proven_complete"])
        self.assertEqual(result["main_job"]["ingest_summary"]["corrected"], 1)
        drift = result["post_freeze_drift"]
        self.assertTrue(drift["changed"])
        # 冻结快照保持原样，当前值漂移在能量与金额层面都可查。
        self.assertEqual(drift["effective_digest"], result["frozen_closure_digest"])
        self.assertTrue(drift["differences"]["energy"])
        self.assertTrue(drift["differences"]["costs"])
        energy_delta = drift["differences"]["energy"][0]["delta"]
        self.assertEqual(energy_delta, 5000000)
        # 高频表计无法证明完整，缺口与热点都可解释。
        self.assertFalse(result["hot_job"]["proven_complete"])
        self.assertEqual(result["hot_job"]["coverage"]["missing_seconds"], 1)
        blocked = result["hot_job"]["unprovable"]
        self.assertTrue(blocked["blocked_windows"])
        self.assertTrue(blocked["hotspots"])
        self.assertGreaterEqual(
            blocked["blocked_windows"][0]["reason"]["minimum_unobserved_rows"], 1
        )
        # blocked 观测没有进入核算链路。
        self.assertEqual(sum(result["hot_job"]["ingest_summary"].values()), 0)
        # 中断恢复确实发生过。
        self.assertTrue(result["main_job"]["first_run"]["failed_windows"])
        self.assertEqual(result["main_job"]["retry"]["requeued"], 1)

    def test_drill_scenario_file_is_valid_json(self):
        path = Path("synthetic_exchange/drill_scenario.json")
        scenario = json.loads(path.read_text())
        self.assertEqual(scenario["limit"], 3)
        self.assertIn("reading_script", scenario)


if __name__ == "__main__":
    unittest.main(verbosity=2)
