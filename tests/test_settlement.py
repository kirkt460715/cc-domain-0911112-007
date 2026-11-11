import unittest
import tempfile
import json
from pathlib import Path
from energyflow.service import EnergyService
from energyflow.values import EnergyError, canonical, fingerprint
from energyflow.demo import run


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.s = EnergyService()
        self.addCleanup(self.s.close)
        self.s.catalog.register("m", "Meter")
        self.s.catalog.install("s", "m", 0, 0, max_gap=7200)
        self.s.readings.add(dict(id="a", segment="s", at=3600, value=10))
        self.s.readings.add(dict(id="b", segment="s", at=7200, value=30))
        self.s.production.put("shift", "m", 0, 7200, {"x": 1, "y": 1})
        self.s.tariffs.put("low", "m", 0, 3600, "0.5")
        self.s.tariffs.put("high", "m", 3600, 7200, "1")
        self.s.emissions.put("factor", "m", 0, 7200, "500", "synthetic factor")

    def freeze(self, ident="period"):
        preview = self.s.closing.preview(["m"], 0, 7200, require_factors=True)
        return self.s.closing.freeze(
            ident, ["m"], 0, 7200, preview["digest"], "operator", require_factors=True
        )

    def test_time_of_use_integrates_at_rate_boundaries(self):
        allocation = self.s.allocation.allocate("m", 0, 7200)
        report = self.s.tariffs.integrate(allocation)
        self.assertEqual(
            report["totals"],
            [{"currency": "CNY", "exact_minor": "2500/1", "minor": 2500}],
        )
        self.assertEqual([r["minor"] for r in report["products"]], [1250, 1250])
        self.assertEqual(len(report["details"]), 2)
        self.assertEqual(report["gaps"], [])
        self.assertTrue(report["complete"])
        self.assertTrue(self.s.quality.conservation(allocation, report)["ok"])

    def test_price_zero_and_currency_switch_do_not_net(self):
        self.s.tariffs.put("low", "m", 0, 3600, "0", expected_revision=1)
        self.s.tariffs.put(
            "high", "m", 3600, 7200, "2", currency="USD", expected_revision=1
        )
        result = self.s.tariffs.integrate(self.s.allocation.allocate("m", 0, 7200))
        self.assertEqual(
            {r["currency"]: r["minor"] for r in result["totals"]},
            {"CNY": 0, "USD": 4000},
        )
        self.assertEqual(
            self.s.tariffs.coverage("m", 0, 7200)["currencies"], ["CNY", "USD"]
        )
        with self.assertRaises(EnergyError):
            self.s.tariffs.put("bad", "m", 0, 10, "-1")
        self.assertEqual(self.s.tariffs.get("high")["revision"], 2)

    def test_tariff_gap_blocks_settlement(self):
        self.s.tariffs.put("low", "m", 0, 1800, "0.5", expected_revision=1)
        allocation = self.s.allocation.allocate("m", 0, 7200)
        with self.assertRaises(EnergyError):
            self.s.tariffs.integrate(allocation)
        partial = self.s.tariffs.integrate(allocation, False)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["gaps"][0]["start"], 1800)
        self.assertEqual(partial["gaps"][0]["end"], 3600)
        with self.assertRaises(EnergyError):
            self.freeze()
        self.assertEqual(self.s.closing.list(), [])

    def test_rate_overlap_and_revision_conflict(self):
        with self.assertRaises(EnergyError):
            self.s.tariffs.put("bad", "m", 3000, 4000, "1")
        with self.assertRaises(EnergyError):
            self.s.tariffs.put("low", "m", 0, 3600, "0.6")
        self.assertEqual(self.s.tariffs.get("low")["revision"], 1)
        self.assertEqual(len(self.s.tariffs.list("m", 0, 7200)), 2)
        quote = self.s.tariffs.quote("m", 0, 7200, "20000000")
        self.assertEqual(quote["totals"][0]["minor"], 1500)

    def test_emissions_and_output_intensity(self):
        allocation = self.s.allocation.allocate("m", 0, 7200)
        report = self.s.emissions.integrate(allocation, True)
        self.assertEqual(report["milligrams"], 15000000)
        self.assertEqual(
            [r["milligrams"] for r in report["products"]], [7500000, 7500000]
        )
        intensity = self.s.emissions.intensity(report, {"x": 10, "y": 0})
        self.assertEqual(intensity["rows"][0]["milligrams_per_unit"], "750000/1")
        self.assertEqual(intensity["rows"][1]["status"], "zero_output")
        missing = self.s.emissions.intensity(report, {})
        self.assertTrue(all(r["status"] == "missing_output" for r in missing["rows"]))
        self.assertTrue(report["complete"])

    def test_factor_revision_propagates_to_new_report(self):
        allocation = self.s.allocation.allocate("m", 0, 7200)
        before = self.s.emissions.integrate(allocation)
        self.s.emissions.put(
            "factor", "m", 0, 7200, "400", "revised synthetic factor", 1
        )
        after = self.s.emissions.integrate(allocation)
        diff = self.s.emissions.compare(before, after)
        self.assertTrue(diff["factor_changed"])
        self.assertEqual(diff["delta_milligrams"], -3000000)
        self.assertEqual(after["milligrams"], 12000000)
        self.assertEqual(self.s.emissions.get("factor")["revision"], 2)
        self.assertEqual(before["milligrams"], 15000000)

    def test_frozen_period_does_not_mutate_after_reading_correction(self):
        original = self.freeze()
        self.s.readings.correct("a", 1, 12, "normal", "calibration")
        self.assertEqual(self.s.closing.get("period"), original)
        self.assertTrue(self.s.closing.verify("period")["verified"])
        drift = self.s.closing.drift("period")
        self.assertTrue(drift["changed"])
        self.assertEqual(sum(r["delta"] for r in drift["differences"]["costs"]), -100)
        self.assertEqual(drift["differences"]["energy"], [])
        self.assertEqual(
            self.s.reconstruction.revision_impact("a", 2)["closures"][0]["id"], "period"
        )

    def test_correction_chain_updates_effective_only(self):
        frozen = self.freeze()
        self.s.readings.correct("a", 1, 12, "normal", "calibration")
        proposal = self.s.closing.propose_correction(
            "c1", "period", "counter correction"
        )
        self.assertFalse(proposal["approved"])
        self.assertEqual(self.s.closing.effective("period")["digest"], frozen["digest"])
        self.s.closing.approve_correction("c1", proposal["digest"], "reviewer")
        self.assertEqual(self.s.closing.effective("period")["corrections"], ["c1"])
        self.assertFalse(self.s.closing.drift("period")["changed"])
        self.assertEqual(self.s.closing.get("period")["payload"], frozen["payload"])
        self.assertEqual(self.s.closing.get("period")["revision"], 2)
        self.assertTrue(
            self.s.closing.approve_correction("c1", proposal["digest"], "reviewer")[
                "approved"
            ]
        )

    def test_stale_correction_cannot_be_approved(self):
        self.freeze()
        self.s.readings.correct("a", 1, 12, "normal", "first")
        proposal = self.s.closing.propose_correction("c1", "period", "first change")
        self.s.readings.correct("a", 2, 13, "normal", "second")
        with self.assertRaises(EnergyError):
            self.s.closing.approve_correction("c1", proposal["digest"], "reviewer")
        self.assertFalse(self.s.closing.correction("c1")["approved"])
        self.assertEqual(self.s.closing.effective("period")["corrections"], [])
        fresh = self.s.closing.propose_correction("c2", "period", "updated review")
        self.s.closing.approve_correction("c2", fresh["digest"], "reviewer")
        self.assertFalse(self.s.closing.drift("period")["changed"])

    def test_competing_corrections_cannot_fork_chain(self):
        self.freeze()
        self.s.readings.correct("a", 1, 12, "normal", "corrected")
        first = self.s.closing.propose_correction(
            "first", "period", "same change first review"
        )
        second = self.s.closing.propose_correction(
            "second", "period", "same change second review"
        )
        self.s.closing.approve_correction("first", first["digest"], "a")
        with self.assertRaises(EnergyError):
            self.s.closing.approve_correction("second", second["digest"], "b")
        self.assertEqual(self.s.closing.effective("period")["corrections"], ["first"])
        self.assertTrue(self.s.closing.verify("period")["verified"])
        with self.assertRaises(EnergyError):
            self.s.closing.propose_correction("none", "period", "no change")

    def test_stale_freeze_preview_and_overlap(self):
        preview = self.s.closing.preview(["m"], 0, 7200)
        self.s.production.put("shift", "m", 0, 7200, {"z": 1}, expected_revision=1)
        with self.assertRaises(EnergyError):
            self.s.closing.freeze("p", ["m"], 0, 7200, preview["digest"], "operator")
        self.assertEqual(self.s.closing.list(), [])
        self.freeze()
        other = self.s.closing.preview(["m"], 0, 3600)
        with self.assertRaises(EnergyError):
            self.s.closing.freeze(
                "overlap", ["m"], 0, 3600, other["digest"], "operator"
            )
        self.assertEqual(len(self.s.closing.list()), 1)

    def test_bundle_export_and_tamper(self):
        self.freeze()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bundle.json"
            result = self.s.bundles.export("period", path)
            self.assertGreater(result["bytes"], 0)
            self.assertTrue(self.s.bundles.verify(path)["verified"])
            with self.assertRaises(EnergyError):
                self.s.bundles.export("period", path)
            envelope = json.loads(path.read_text())
            envelope["payload"]["costs"][0]["minor"] += 1
            with self.assertRaises(EnergyError):
                self.s.bundles.verify_content(json.dumps(envelope))
            self.assertEqual(list(Path(folder).glob(".energyflow-*")), [])

    def test_bundle_comparison_shows_approved_delta(self):
        self.freeze()
        before = self.s.bundles.envelope("period")
        self.s.readings.correct("a", 1, 12, "normal", "correction")
        correction = self.s.closing.propose_correction("c", "period", "adjust")
        self.s.closing.approve_correction("c", correction["digest"], "reviewer")
        after = self.s.bundles.envelope("period", True)
        result = self.s.bundles.compare(before, after)
        self.assertTrue(result["changed"])
        self.assertEqual(sum(r["delta"] for r in result["differences"]["costs"]), -100)
        self.assertEqual(after["corrections"], ["c"])
        self.assertIn("energy,electricity,import,x,", self.s.bundles.csv("period"))

    def test_demand_windows_and_rolling_peak(self):
        windows = self.s.demand.windows("m", 0, 7200, 900)
        self.assertEqual(len(windows["windows"]), 8)
        self.assertEqual(windows["peak"]["microkw"], "20000000/1")
        self.assertEqual(windows["peak"]["start"], 3600)
        rolling = self.s.demand.rolling_peak("m", 0, 7200, 1800)
        self.assertEqual(rolling["peak"]["microkw"], "20000000/1")
        self.assertEqual(self.s.demand.load_factor(windows)["load_factor"], "3/4")
        charge = self.s.demand.charge(windows, "10", "15", True)
        self.assertEqual(charge["minor"], 5000)
        events = self.s.demand.exceedances(windows, "15")
        self.assertEqual(events["seconds"], 3600)
        self.assertEqual(events["events"][0]["windows"], 4)

    def test_partial_demand_windows_cannot_define_peak(self):
        windows = self.s.demand.windows("m", 100, 800, 900, include_partial=True)
        self.assertEqual(len(windows["windows"]), 1)
        self.assertTrue(windows["windows"][0]["partial"])
        self.assertIsNone(windows["peak"])
        with self.assertRaises(EnergyError):
            self.s.demand.charge(windows, "10")
        self.assertEqual(
            self.s.demand.load_factor(windows)["status"], "no_complete_windows"
        )
        with self.assertRaises(EnergyError):
            self.s.demand.rolling_peak("m", 100, 800, 900)

    def test_quality_readiness_and_revision_summary(self):
        ready = self.s.quality.readiness(["m"], 0, 7200, require_factors=True)
        self.assertTrue(ready["ready"])
        self.s.readings.correct("a", 1, 0, "missing", "source lost")
        self.assertFalse(self.s.quality.readiness(["m"], 0, 7200)["ready"])
        summary = self.s.quality.revision_summary("m")
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["corrected_readings"][0]["reasons"], ["source lost"])
        self.assertGreater(self.s.quality.inspect("m", 0, 7200)["errors"], 0)
        self.assertTrue(self.s.quality.system()["ok"])

    def test_dispatch_and_business_demo(self):
        result = self.s.dispatch(
            {
                "action": "reconstruction.reconstruct",
                "arguments": {"meter": "m", "start": 0, "end": 7200},
            }
        )
        self.assertEqual(result["microkwh"], "30000000/1")
        self.assertGreater(len(self.s.describe()), 60)
        with self.assertRaises(EnergyError):
            self.s.dispatch({"action": "unknown"})
        with self.assertRaises(EnergyError):
            self.s.dispatch({"action": "catalog.get", "arguments": {"wrong": "m"}})
        demo = run()
        self.assertEqual(demo["original_cost_minor"], 2500)
        self.assertEqual(demo["corrected_cost_minor"], 2400)
        self.assertTrue(demo["frozen_preserved"])
