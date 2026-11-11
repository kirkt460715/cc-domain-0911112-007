from tempfile import TemporaryDirectory
from pathlib import Path
from .service import EnergyService


def run():
    with TemporaryDirectory() as folder:
        with EnergyService(Path(folder) / "energy.db") as service:
            service.catalog.register("line", "Synthetic production line")
            service.catalog.install("counter", "line", 0, 0, max_gap=7200)
            service.readings.import_batch(
                "shift",
                [
                    {"id": "r1", "segment": "counter", "at": 3600, "value": 10},
                    {"id": "r2", "segment": "counter", "at": 7200, "value": 30},
                ],
            )
            service.production.put(
                "first", "line", 0, 3600, {"productA": 1, "productB": 1}
            )
            service.production.put("second", "line", 3600, 7200, {"productB": 1})
            service.tariffs.put("offpeak", "line", 0, 3600, "0.5")
            service.tariffs.put("peak", "line", 3600, 7200, "1.0")
            service.emissions.put("local", "line", 0, 7200, "500", "synthetic factor")
            preview = service.closing.preview(["line"], 0, 7200, require_factors=True)
            frozen = service.closing.freeze(
                "shift",
                ["line"],
                0,
                7200,
                preview["digest"],
                "controller",
                require_factors=True,
            )
            assert (
                sum(row["microkwh"] for row in frozen["payload"]["energy"]) == 30000000
            )
            assert sum(row["minor"] for row in frozen["payload"]["costs"]) == 2500
            service.readings.correct(
                "r1", 1, 12, "normal", "synthetic calibration correction"
            )
            correction = service.closing.propose_correction(
                "adjust", "shift", "corrected intermediate counter"
            )
            service.closing.approve_correction(
                "adjust", correction["digest"], "controller"
            )
            effective = service.closing.effective("shift")
            assert (
                sum(row["microkwh"] for row in effective["payload"]["energy"])
                == 30000000
            )
            assert sum(row["minor"] for row in effective["payload"]["costs"]) == 2400
            assert service.closing.verify("shift")["verified"]
            assert not service.closing.drift("shift")["changed"]
            target = Path(folder) / "settlement.json"
            service.bundles.export("shift", target, effective=True)
            assert service.bundles.verify(target)["verified"]
            assert service.quality.system()["ok"]
            return {
                "status": "ok",
                "energy_microkwh": 30000000,
                "original_cost_minor": 2500,
                "corrected_cost_minor": 2400,
                "frozen_preserved": True,
                "offline": True,
            }
