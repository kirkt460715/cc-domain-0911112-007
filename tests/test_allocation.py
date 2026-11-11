import unittest
from fractions import Fraction
from energyflow.service import EnergyService
from energyflow.values import EnergyError, fingerprint, fraction
from energyflow.calendar import (
    boundaries,
    split,
    subtract,
    utc_days,
    shift_intervals,
    slice_quantity,
    integer_slices,
    coverage_summary,
)


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.s = EnergyService()
        self.addCleanup(self.s.close)
        self.s.catalog.register("m", "Meter")
        self.s.catalog.install("s", "m", 0, 0, max_gap=7200)
        self.s.readings.add(dict(id="r", segment="s", at=7200, value=30))

    def test_calendar_boundaries_clip_and_align(self):
        self.assertEqual(boundaries(100, 250, 60), [100, 120, 180, 240, 250])
        self.assertEqual(split(0, 10, [3, 3, -0, 20]), [(0, 3), (3, 10)])
        self.assertEqual(subtract(0, 10, [(2, 4), (3, 6), (8, 12)]), [(0, 2), (6, 8)])
        self.assertEqual(utc_days(86390, 86410)[0]["seconds"], 10)
        report = coverage_summary(0, 10, [(0, 5), (4, 10)])
        self.assertEqual(report["overlap_seconds"], 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["missing_seconds"], 0)

    def test_fractional_slices_conserve_quantity(self):
        parts = slice_quantity("1/3", 0, 10, [1, 4, 8])
        self.assertEqual(
            sum(fraction(row["quantity"]) for row in parts), Fraction(1, 3)
        )
        self.assertEqual(parts[0]["quantity"], "1/30")
        integers = integer_slices(5, 0, 10, [3, 6])
        self.assertEqual(sum(row["quantity"] for row in integers), 5)
        self.assertEqual([row["quantity"] for row in integers], [2, 1, 2])
        with self.assertRaises(EnergyError):
            slice_quantity("-1", 0, 10, [])

    def test_overnight_fixed_offset_shifts(self):
        shifts = [{"id": "night", "start_minute": 1320, "end_minute": 1800}]
        spans = shift_intervals(0, 86400, shifts)
        self.assertEqual(
            [(r["start"], r["end"]) for r in spans], [(0, 21600), (79200, 86400)]
        )
        self.assertEqual(spans[0]["date"], "1969-12-31")
        with self.assertRaises(EnergyError):
            shift_intervals(
                0,
                86400,
                shifts + [{"id": "other", "start_minute": 1380, "end_minute": 1500}],
            )
        self.assertEqual(
            shift_intervals(
                0, 3600, [{"id": "day", "start_minute": 480, "end_minute": 960}], 480
            )[0]["end"],
            3600,
        )

    def test_product_weights_and_time_splits(self):
        self.s.production.put("one", "m", 0, 3600, {"a": 1, "b": 2})
        self.s.production.put("two", "m", 3600, 7200, {"b": 1})
        report = self.s.allocation.require_assigned("m", 0, 7200)
        self.assertEqual(
            {r["product"]: r["microkwh"] for r in report["products"]},
            {"a": 5000000, "b": 25000000},
        )
        self.assertEqual(len(report["slices"]), 2)
        self.assertEqual(report["microkwh"], 30000000)
        self.assertEqual(report["unassigned_seconds"], 0)
        self.assertEqual(
            self.s.allocation.product_trace("m", 0, 7200, "a")["exact_microkwh"],
            "5000000/1",
        )
        self.assertTrue(report["complete"])

    def test_unassigned_time_is_visible(self):
        self.s.production.put("partial", "m", 0, 3600, {"a": 1})
        report = self.s.allocation.allocate("m", 0, 7200)
        self.assertEqual(report["unassigned_seconds"], 3600)
        self.assertEqual(
            {r["product"]: r["microkwh"] for r in report["products"]},
            {"a": 15000000, "unassigned": 15000000},
        )
        with self.assertRaises(EnergyError):
            self.s.allocation.require_assigned("m", 0, 7200)
        coverage = self.s.production.coverage("m", 0, 7200)
        self.assertEqual(coverage["missing_seconds"], 3600)
        self.assertFalse(coverage["complete"])

    def test_schedule_overlap_revision_and_boundary(self):
        self.s.production.put("a", "m", 0, 3600, {"a": 1})
        with self.assertRaises(EnergyError):
            self.s.production.put("b", "m", 3500, 7200, {"b": 1})
        with self.assertRaises(EnergyError):
            self.s.production.put("a", "m", 0, 3600, {"a": 2})
        self.s.production.put("b", "m", 3600, 7200, {"b": 1})
        self.assertEqual(self.s.production.at("m", 3600)["id"], "b")
        self.s.production.put("a", "m", 0, 3600, {"c": 1}, expected_revision=1)
        self.assertEqual(self.s.production.get("a")["revision"], 2)
        self.assertEqual(self.s.production.at("m", 0)["weights"], {"c": "1/1"})

    def test_replace_span_is_atomic_on_overlap(self):
        self.s.production.put("a", "m", 0, 7200, {"a": 1})
        before = self.s.production.list("m", 0, 7200)
        replacement = [
            {"id": "x", "start": 0, "end": 4000, "weights": {"x": 1}},
            {"id": "y", "start": 3000, "end": 7200, "weights": {"y": 1}},
        ]
        with self.assertRaises(EnergyError):
            self.s.production.replace_span(
                "m", 0, 7200, replacement, fingerprint(before)
            )
        self.assertEqual(self.s.production.list("m", 0, 7200), before)
        replacement[0]["end"] = 3000
        result = self.s.production.replace_span(
            "m", 0, 7200, replacement, fingerprint(before)
        )
        self.assertEqual([r["id"] for r in result], ["x", "y"])
        self.assertTrue(self.s.production.coverage("m", 0, 7200)["complete"])

    def test_schedule_change_moves_energy_without_changing_total(self):
        self.s.production.put("a", "m", 0, 7200, {"a": 1})
        old = self.s.allocation.allocate("m", 0, 7200)
        self.s.production.put("a", "m", 0, 7200, {"b": 1}, expected_revision=1)
        new = self.s.allocation.allocate("m", 0, 7200)
        differences = self.s.allocation.compare(old, new)
        self.assertEqual(differences["delta_microkwh"], 0)
        self.assertTrue(differences["schedule_changed"])
        self.assertFalse(differences["reading_changed"])
        self.assertEqual(
            [r["delta_microkwh"] for r in differences["rows"]], [-30000000, 30000000]
        )
        self.assertEqual(
            self.s.allocation.utilization("m", 0, 7200)["rows"][0]["fraction"], "1/1"
        )

    def test_fractional_energy_rounds_once_at_period(self):
        self.s.production.put("a", "m", 0, 7200, {"a": 1, "b": 1, "c": 1})
        report = self.s.allocation.allocate("m", 0, 1)
        self.assertEqual(report["exact_microkwh"], "12500/3")
        self.assertEqual(report["microkwh"], 4167)
        self.assertEqual(sum(r["microkwh"] for r in report["products"]), 4167)
        self.assertTrue(all(r["microkwh"] == 1389 for r in report["products"]))
        self.assertEqual(
            sum(fraction(r["exact_microkwh"]) for r in report["products"]),
            Fraction(12500, 3),
        )

    def test_zero_energy_does_not_divide_by_zero(self):
        self.s.readings.correct("r", 1, 0, "normal", "idle plant")
        self.s.production.put("a", "m", 0, 7200, {"a": 1, "b": 0})
        report = self.s.allocation.allocate("m", 0, 7200)
        self.assertEqual(report["microkwh"], 0)
        self.assertEqual(report["products"][0]["microkwh"], 0)
        self.assertEqual(report["products"][1]["microkwh"], 0)
        self.assertTrue(report["complete"])
        self.assertEqual(self.s.allocation.utilization("m", 0, 7200)["rows"], [])

    def test_parent_child_balance_aligns_different_sampling(self):
        self.s.catalog.register("child", "Submeter")
        self.s.catalog.install("cs", "child", 0, 0)
        self.s.readings.add(dict(id="c1", segment="cs", at=3600, value=10))
        self.s.readings.add(dict(id="c2", segment="cs", at=7200, value=20))
        report = self.s.balance.compare("m", ["child"], 0, 7200)
        self.assertTrue(report["balanced"])
        self.assertEqual(report["residual_microkwh"], "10000000/1")
        self.assertEqual(report["loss_ratio"], "1/3")
        self.assertEqual(len(report["rows"]), 2)
        allocated = self.s.balance.allocate_residual(report, {"a": 1, "b": 1})
        self.assertEqual(allocated["allocated_microkwh"], 10000000)
        self.assertEqual(allocated["products"][0]["microkwh"], 5000000)

    def test_parent_child_overdraw_and_topology_cycle(self):
        self.s.catalog.register("child", "Submeter")
        self.s.catalog.install("cs", "child", 0, 0, max_gap=7200)
        self.s.readings.add(dict(id="c", segment="cs", at=7200, value=40))
        report = self.s.balance.compare("m", ["child"], 0, 7200)
        self.assertFalse(report["balanced"])
        self.assertEqual(report["rows"][0]["status"], "child_exceeds_parent")
        with self.assertRaises(EnergyError):
            self.s.balance.allocate_residual(report, {"a": 1})
        with self.assertRaises(EnergyError):
            self.s.balance.tree("m", {"m": ["child"], "child": ["m"]}, 0, 7200)
        self.assertTrue(
            self.s.balance.compare("m", ["child"], 0, 7200, 10000000)["balanced"]
        )

    def test_partial_child_coverage_is_not_zero_consumption(self):
        self.s.catalog.register("child", "Submeter")
        self.s.catalog.install("cs", "child", 0, 0)
        self.s.readings.add(dict(id="c", segment="cs", at=3600, value=10))
        report = self.s.balance.compare("m", ["child"], 0, 7200)
        self.assertFalse(report["complete"])
        self.assertEqual(report["rows"][1]["status"], "incomplete")
        self.assertEqual(report["rows"][1]["missing"], ["child"])
        with self.assertRaises(EnergyError):
            self.s.balance.allocate_residual(report, {"a": 1})
        self.assertFalse(report["balanced"])

    def test_meter_fleet_keeps_directions_separate(self):
        self.s.production.put("a", "m", 0, 7200, {"a": 1})
        self.s.catalog.register("export", "Export meter", direction="export")
        self.s.catalog.install("es", "export", 0, 0, max_gap=7200)
        self.s.readings.add(dict(id="e", segment="es", at=7200, value=5))
        self.s.production.put("e", "export", 0, 7200, {"a": 1})
        report = self.s.allocation.fleet(["m", "export"], 0, 7200)
        self.assertEqual(len(report["products"]), 2)
        self.assertEqual(
            {r["direction"]: r["microkwh"] for r in report["products"]},
            {"export": 5000000, "import": 30000000},
        )
        with self.assertRaises(EnergyError):
            self.s.balance.compare("m", ["export"], 0, 7200)
        self.assertTrue(report["complete"])
