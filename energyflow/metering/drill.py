"""本地联调演练：对合成网关跑完整采集 → 核算链路。

演练同时演示：
1. 同秒超限热点（最小精度内仍超限，窗口 blocked，缺口可解释）；
2. 交错修订（同一次采集中出现 revision 1/2，高版走更正）；
3. 中断恢复（首个窗口 503 失败，重试后续跑）；
4. 结果同时给出已入账读数与“无法证明完整”的部分。
"""
import json
import threading
from pathlib import Path

from ..service import EnergyService
from .gateway import GatewayClient
from synthetic_exchange.server import make_server


def _scenario(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise FileNotFoundError("scenario must be a regular file: " + str(target))
    return json.loads(target.read_text("utf-8"))


def run_drill(scenario_path="synthetic_exchange/drill_scenario.json", port=0):
    scenario = _scenario(scenario_path)
    server = make_server(scenario, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = EnergyService(":memory:")
    try:
        base = "http://127.0.0.1:" + str(server.server_port)
        gateway = GatewayClient(base, timeout=3, fetch_retries=0)
        service.attach_gateway(gateway)
        service.catalog.register("main", "Main production line")
        service.catalog.install("counter", "main", 0, 0, max_gap=7200)
        service.catalog.register("hot", "High frequency line")
        service.catalog.install("counter2", "hot", 0, 0, max_gap=7200)
        service.production.put("shift-main", "main", 0, 200, {"productA": 1})
        service.tariffs.put("rate-main", "main", 0, 200, "1")

        harvest = service.harvest

        # 作业一：常规表计，含首次 503 中断与交错修订。采集窗口取 0..201，
        # 演示左闭右开：at=200 的读数会被采到，而核算窗口仍是 [0,200)。
        harvest.create_job("drill-main", "main", 0, 201, request_budget=80)
        first = harvest.run("drill-main")
        first_failed = [w["id"] for w in harvest.windows("drill-main", "failed")]
        retry = harvest.retry_failures("drill-main")
        second = harvest.run("drill-main")
        ingest_first = harvest.ingest("drill-main")

        # 第三轮采集：修订脚本让 r1b 以 revision 2 返回，演示交错修订 → 更正；
        # 已完成窗口通过回看重新排队，观测台账记录版本推进。
        reobserve = harvest.reobserve("drill-main", 100, 201)
        third = harvest.run("drill-main")
        ingest_main = harvest.ingest("drill-main")
        report_main = harvest.report("drill-main")
        coverage_main = harvest.coverage("drill-main")

        # 作业二：高频表计，同秒 5 条 > 上限 3，最小精度处 blocked。
        harvest.create_job("drill-hot", "hot", 0, 200, request_budget=80)
        harvest.run("drill-hot")
        ingest_hot = harvest.ingest("drill-hot")
        report_hot = harvest.report("drill-hot")
        coverage_hot = harvest.coverage("drill-hot")

        reconstruction = service.reconstruction.reconstruct("main", 0, 200)

        # 常规表计覆盖完整，可冻结；冻结后再推一个更高 revision 验证漂移可查、
        # 冻结快照保持原样。
        frozen_digest = None
        drift = None
        if report_main["proven_complete"]:
            preview = service.closing.preview(["main"], 0, 200)
            frozen = service.closing.freeze(
                "drill-period", ["main"], 0, 200, preview["digest"], "drill-operator"
            )
            frozen_digest = frozen["digest"]
            current_revision = service.readings.get("r1b")["revision"]
            service.readings.correct(
                "r1b", current_revision, 65, "normal", "post-freeze recalibration"
            )
            # 再更正末条读数，改变区间总量，使漂移在能量/金额层面也可见。
            r3_revision = service.readings.get("r3")["revision"]
            service.readings.correct(
                "r3", r3_revision, 95, "normal", "post-freeze terminal adjustment"
            )
            drift = service.closing.drift("drill-period")
            # 冻结快照本身不被改写。
            assert service.closing.get("drill-period")["digest"] == frozen_digest

        result = {
            "gateway": base,
            "capabilities": gateway.capabilities(),
            "main_job": {
                "first_run": {
                    "state": first["state"],
                    "failed_windows": first_failed,
                    "requests": first["runs"][0]["requests_used"],
                },
                "retry": retry,
                "second_run_state": second["state"],
                "first_ingest": ingest_first["summary"],
                "reobserve": reobserve,
                "third_run_state": third["state"],
                "ingest_summary": ingest_main["summary"],
                "ingest_log": harvest.ingest_log("drill-main"),
                "proven_complete": report_main["proven_complete"],
                "coverage": {
                    "covered_seconds": coverage_main["covered_seconds"],
                    "missing_seconds": coverage_main["missing_seconds"],
                    "gaps": coverage_main["gaps"],
                },
                "runs": report_main["runs"],
                "gateway_requests": report_main["gateway_requests"],
            },
            "hot_job": {
                "proven_complete": report_hot["proven_complete"],
                "coverage": {
                    "covered_seconds": coverage_hot["covered_seconds"],
                    "missing_seconds": coverage_hot["missing_seconds"],
                    "gaps": coverage_hot["gaps"],
                },
                "unprovable": {
                    "blocked_windows": report_hot["blocked_windows"],
                    "hotspots": report_hot["hotspots"],
                    "blocked_observations": [
                        {"id": row["id"], "at": row["at"], "detail": row["detail"]}
                        for row in harvest.observations("drill-hot", "blocked")
                    ],
                },
                "ingest_summary": ingest_hot["summary"],
            },
            "accounting": {
                "reconstruction_complete": reconstruction["complete"],
                "microkwh": reconstruction["microkwh"],
            },
            "frozen_closure_digest": frozen_digest,
            "post_freeze_drift": drift,
        }
        return result
    finally:
        service.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


if __name__ == "__main__":
    from ..values import canonical

    print(canonical(run_drill()))
