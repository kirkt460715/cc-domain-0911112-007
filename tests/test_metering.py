import unittest
import tempfile
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from energyflow.service import EnergyService
from energyflow.values import (
    EnergyError,
    instant,
    fixed,
    fraction,
    distribute,
    round_even,
    parse_json,
    fingerprint,
)


class MeteringTests(unittest.TestCase):
    def setUp(self):
        self.s = EnergyService()
        self.addCleanup(self.s.close)
        self.s.catalog.register("m", "Synthetic meter")
        self.s.catalog.install("seg", "m", 0, 0, max_gap=7200)

    def reading(self, ident, at, value, **extra):
        return self.s.readings.add(
            dict(id=ident, segment="seg", at=at, value=value, **extra)
        )

    def test_exact_values_and_utc_normalization(self):
        self.assertEqual(instant("1970-01-01T08:00:00+08:00"), 0)
        self.assertEqual(fixed("1.000001"), 1000001)
        self.assertEqual(fraction("1/3"), Fraction(1, 3))
        self.assertEqual(round_even("1/2"), 0)
        self.assertEqual(round_even("3/2"), 2)
        self.assertEqual(round_even("-3/2"), -2)
        for value in ["1970-01-01", "not-time", True, -1]:
            with self.assertRaises(EnergyError):
                instant(value)
        for value in ["NaN", "Infinity", "0.0000001", -1, 1.2]:
            with self.assertRaises(EnergyError):
                fixed(value)
        with self.assertRaises(EnergyError):
            parse_json('{"id":1,"id":2}')

    def test_largest_remainder_is_deterministic(self):
        self.assertEqual(
            distribute(2, {"c": 1, "a": 1, "b": 1}), {"a": 1, "b": 1, "c": 0}
        )
        self.assertEqual(distribute(7, {"a": "1/3", "b": "2/3"}), {"a": 2, "b": 5})
        self.assertEqual(distribute(0, {"a": 1}), {"a": 0})
        for weights in [{}, {"a": 0}, {"a": -1}, {"a": "NaN"}]:
            with self.assertRaises(EnergyError):
                distribute(2, weights)
        self.assertEqual(
            sum(distribute(10**12, {"a": 7, "b": 11, "c": 13}).values()), 10**12
        )

    def test_registration_is_immutable(self):
        first = self.s.catalog.get("m")
        self.assertEqual(self.s.catalog.register("m", "Synthetic meter"), first)
        with self.assertRaises(EnergyError):
            self.s.catalog.register("m", "Another name")
        with self.assertRaises(EnergyError):
            self.s.catalog.register("x", "Bad unit", unit="litre")
        self.assertEqual(len(self.s.catalog.list()), 1)
        self.assertEqual(self.s.catalog.list(direction="export"), [])
        self.assertEqual(self.s.catalog.get("m")["medium"], "electricity")

    def test_out_of_order_readings_reconstruct_sorted_time(self):
        self.reading("late", 7200, 30)
        self.reading("early", 3600, 10)
        report = self.s.reconstruction.reconstruct("m", 0, 7200, True)
        self.assertEqual(report["microkwh"], "30000000/1")
        self.assertEqual([r["start"] for r in report["intervals"]], [0, 3600])
        self.assertEqual(
            [r["microkwh"] for r in report["intervals"]], ["10000000/1", "20000000/1"]
        )
        self.assertTrue(report["complete"])
        self.assertEqual(report["coverage"]["missing_seconds"], 0)

    def test_reading_replay_after_correction_returns_current(self):
        original = dict(id="r", segment="seg", at=3600, value=10)
        self.s.readings.add(original)
        self.s.readings.correct("r", 1, 11, "normal", "calibration")
        before = self.s.store.verify_audit()
        repeated = self.s.readings.add(original)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["value"], 11)
        self.assertEqual(repeated["revision"], 2)
        self.assertEqual(self.s.store.verify_audit(), before)
        with self.assertRaises(EnergyError):
            self.s.readings.add(dict(original, value=12))

    def test_duplicate_time_and_invalid_readings_are_rejected(self):
        self.reading("r", 3600, 10)
        for ident, at, value, extra in [
            ("other", 3600, 11, {}),
            ("zero", 0, 0, {}),
            ("negative", 100, -1, {}),
            ("missing", 4000, 2, {"kind": "missing"}),
            ("roll", 4000, 2, {"kind": "rollover"}),
        ]:
            with self.assertRaises(EnergyError):
                self.reading(ident, at, value, **extra)
        self.assertEqual(len(self.s.readings.segment("seg")), 1)
        self.assertTrue(self.s.store.integrity()["verified"])

    def test_batch_failure_rolls_back_readings_and_audit(self):
        before = self.s.store.verify_audit()
        rows = [
            dict(id="r1", segment="seg", at=3600, value=10),
            dict(id="r2", segment="missing", at=7200, value=20),
        ]
        with self.assertRaises(EnergyError):
            self.s.readings.import_batch("batch", rows)
        self.assertEqual(self.s.readings.segment("seg"), [])
        self.assertEqual(self.s.store.verify_audit(), before)
        self.assertEqual(self.s.store.rows("SELECT * FROM imports"), [])
        rows[1]["segment"] = "seg"
        result = self.s.readings.import_batch("batch", rows)
        self.assertEqual(result["inserted"], 2)
        self.assertTrue(self.s.readings.import_batch("batch", rows)["reused"])

    def test_correction_changes_adjacent_intervals_not_total(self):
        self.reading("a", 3600, 10)
        self.reading("b", 7200, 30)
        old = self.s.reconstruction.reconstruct("m", 0, 7200)
        self.s.readings.correct("a", 1, 12, "normal", "calibration")
        new = self.s.reconstruction.reconstruct("m", 0, 7200)
        self.assertEqual(old["microkwh"], new["microkwh"])
        self.assertEqual(new["intervals"][0]["microkwh"], "12000000/1")
        self.assertEqual(new["intervals"][1]["microkwh"], "18000000/1")
        self.assertNotEqual(old["digest"], new["digest"])
        self.assertEqual(self.s.readings.get("a", 1)["value"], 10)
        self.assertEqual(len(self.s.readings.history("a")), 2)

    def test_correction_requires_current_revision_and_change(self):
        self.reading("r", 3600, 10)
        with self.assertRaises(EnergyError):
            self.s.readings.correct("r", 2, 11, "normal", "stale")
        with self.assertRaises(EnergyError):
            self.s.readings.correct("r", 1, 10, "normal", "no change")
        self.assertEqual(self.s.readings.get("r")["revision"], 1)
        self.s.readings.correct("r", 1, 11, "normal", "valid")
        with self.assertRaises(EnergyError):
            self.s.readings.correct("r", 1, 12, "normal", "stale")
        self.assertEqual(self.s.readings.get("r")["value"], 11)

    def test_missing_sample_invalidates_both_adjacent_intervals(self):
        self.reading("a", 3600, 0, kind="missing")
        self.reading("b", 7200, 30)
        report = self.s.reconstruction.reconstruct("m", 0, 7200)
        self.assertFalse(report["complete"])
        self.assertEqual(report["microkwh"], "0/1")
        self.assertEqual(report["quality"]["invalid_intervals"], 2)
        self.assertEqual(report["coverage"]["missing_seconds"], 7200)
        with self.assertRaises(EnergyError):
            self.s.reconstruction.reconstruct("m", 0, 7200, True)
        self.s.readings.correct("a", 1, 10, "normal", "recovered source")
        self.assertTrue(self.s.reconstruction.reconstruct("m", 0, 7200)["complete"])

    def test_counter_decrease_is_not_silently_treated_as_reset(self):
        self.reading("a", 3600, 20)
        self.reading("b", 7200, 10)
        report = self.s.reconstruction.reconstruct("m", 0, 7200)
        self.assertEqual(report["intervals"][1]["flags"], ["counter_decrease"])
        self.assertEqual(report["microkwh"], "20000000/1")
        self.assertFalse(report["complete"])
        self.assertEqual(
            self.s.reconstruction.gaps("m", 0, 7200)["missing_seconds"], 3600
        )
        self.assertTrue(self.s.catalog.inspect()["ok"])

    def test_explicit_rollover_and_modulus(self):
        self.s.catalog.register("r", "Rollover meter")
        self.s.catalog.install("roll", "r", 0, 90, modulus=100)
        self.s.readings.add(
            dict(id="wrapped", segment="roll", at=3600, value=5, kind="rollover")
        )
        report = self.s.reconstruction.reconstruct("r", 0, 3600, True)
        self.assertEqual(report["microkwh"], "15000000/1")
        self.assertEqual(report["intervals"][0]["ticks"], 15)
        with self.assertRaises(EnergyError):
            self.s.readings.add(dict(id="bad", segment="roll", at=7200, value=100))
        self.assertEqual(len(self.s.readings.segment("roll")), 1)

    def test_power_limit_and_gap_are_independent_quality_flags(self):
        self.s.catalog.register("limited", "Limited meter")
        self.s.catalog.install("limit", "limited", 0, 0, max_gap=100, max_kw="1")
        self.s.readings.add(dict(id="fast", segment="limit", at=200, value=2))
        report = self.s.reconstruction.reconstruct("limited", 0, 200)
        self.assertIn("gap_too_long", report["intervals"][0]["flags"])
        self.assertIn("power_exceeded", report["intervals"][0]["flags"])
        self.assertEqual(report["microkwh"], "0/1")
        self.assertFalse(report["complete"])
        self.assertIsNone(report["intervals"][0]["microkwh"])

    def test_replacement_uses_new_baseline_without_bridge(self):
        self.reading("end", 3600, 10)
        self.s.catalog.replacement("seg", "new", 3600, 100, microkwh_per_tick=2000000)
        self.s.readings.add(dict(id="next", segment="new", at=7200, value=105))
        report = self.s.reconstruction.reconstruct("m", 0, 7200, True)
        self.assertEqual(report["microkwh"], "20000000/1")
        self.assertEqual([r["segment"] for r in report["intervals"]], ["seg", "new"])
        self.assertTrue(self.s.catalog.coverage("m", 0, 7200)["complete"])
        with self.assertRaises(EnergyError):
            self.reading("too_late", 4000, 11)
        self.assertEqual(self.s.catalog.segment("seg")["end"], 3600)

    def test_failed_replacement_rolls_back_retirement(self):
        self.reading("end", 3600, 10)
        with self.assertRaises(EnergyError):
            self.s.catalog.replacement("seg", "bad", 3600, 100, modulus=50)
        self.assertIsNone(self.s.catalog.segment("seg")["end"])
        self.assertEqual(len(self.s.catalog.segments("m")), 1)
        with self.assertRaises(EnergyError):
            self.s.catalog.retire("seg", 3500)
        self.assertIsNone(self.s.catalog.segment("seg")["end"])
        self.assertTrue(self.s.quality.system()["ok"])

    def test_clipped_interval_keeps_exact_fraction(self):
        self.reading("r", 3, 1)
        report = self.s.reconstruction.reconstruct("m", 1, 2, True)
        self.assertEqual(report["microkwh"], "1000000/3")
        self.assertEqual(report["intervals"][0]["source_start"], 0)
        self.assertEqual(report["intervals"][0]["source_end"], 3)
        self.assertEqual(report["coverage"]["covered_seconds"], 1)
        self.assertTrue(report["complete"])

    def test_no_extrapolation_past_latest_reading(self):
        self.reading("r", 3600, 10)
        report = self.s.reconstruction.reconstruct("m", 0, 7200)
        self.assertEqual(report["microkwh"], "10000000/1")
        self.assertFalse(report["complete"])
        self.assertEqual(report["coverage"]["missing_seconds"], 3600)
        self.assertEqual(report["intervals"][-1]["flags"], ["no_reading_coverage"])
        self.assertEqual(self.s.quality.staleness("m", 7200, 100)["status"], "stale")
        self.assertEqual(self.s.quality.staleness("m", 3700, 100)["status"], "current")

    def test_import_protocol_and_preview_errors(self):
        content = "id,segment,at,value,kind\nr,seg,1970-01-01T01:00:00Z,10,\n"
        rows = self.s.readings.parse(content, "csv")
        self.assertEqual(rows[0]["value"], 10)
        self.assertEqual(self.s.readings.preview(rows)["errors"], [])
        preview = self.s.readings.preview(
            [rows[0], rows[0], dict(id="bad", segment="no", at=20, value=1)]
        )
        self.assertFalse(preview["valid"])
        self.assertEqual(len(preview["errors"]), 2)
        self.assertEqual(self.s.readings.segment("seg"), [])
        self.assertEqual(self.s.readings.import_batch("b", rows)["inserted"], 1)

    def test_storage_backup_and_audit_corruption(self):
        self.reading("r", 3600, 10)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "backup.db"
            self.s.store.backup(path)
            with EnergyService(path) as other:
                self.assertEqual(other.readings.get("r")["value"], 10)
                self.assertTrue(other.store.integrity()["verified"])
            with self.assertRaises(EnergyError):
                self.s.store.backup(path)
        self.s.store.connection.execute("UPDATE audit SET digest='bad' WHERE seq=1")
        with self.assertRaises(EnergyError):
            self.s.store.verify_audit()

    def test_concurrent_corrections_have_one_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shared.db"
            with EnergyService(path) as setup:
                setup.catalog.register("m", "Meter")
                setup.catalog.install("s", "m", 0, 0)
                setup.readings.add(dict(id="r", segment="s", at=3600, value=10))

            def change(value):
                with EnergyService(path) as worker:
                    try:
                        worker.readings.correct("r", 1, value, "normal", "correction")
                        return "ok"
                    except EnergyError as exc:
                        return exc.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(change, [11, 12]))
            self.assertEqual(sorted(results), ["conflict", "ok"])
            with EnergyService(path) as final:
                self.assertEqual(final.readings.get("r")["revision"], 2)
                self.assertEqual(len(final.readings.history("r")), 2)
                self.assertTrue(final.store.integrity()["verified"])
