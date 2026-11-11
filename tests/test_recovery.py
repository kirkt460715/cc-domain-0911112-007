import unittest
import tempfile
import json
import contextlib
import io
from pathlib import Path
from energyflow.service import EnergyService
from energyflow.values import EnergyError, fingerprint, canonical
from energyflow.terminal import main


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.s = EnergyService()
        self.addCleanup(self.s.close)
        self.s.catalog.register("m", "Meter")
        self.s.catalog.install("seg", "m", 0, 0, max_gap=7200)
        self.s.readings.add(dict(id="r1", segment="seg", at=3600, value=10))
        self.s.readings.add(dict(id="r2", segment="seg", at=7200, value=20))
        self.s.production.put("shift", "m", 0, 7200, {"a": 1})
        self.s.tariffs.put("rate", "m", 0, 7200, "1")

    def freeze(self):
        preview = self.s.closing.preview(["m"], 0, 7200)
        return self.s.closing.freeze(
            "period", ["m"], 0, 7200, preview["digest"], "controller"
        )

    def test_two_approved_corrections_form_continuous_chain(self):
        original = self.freeze()
        self.s.readings.correct("r2", 1, 22, "normal", "first correction")
        first = self.s.closing.propose_correction("first", "period", "first")
        self.s.closing.approve_correction("first", first["digest"], "operator")
        first_effective = self.s.closing.effective("period")
        self.s.readings.correct("r2", 2, 24, "normal", "second correction")
        second = self.s.closing.propose_correction("second", "period", "second")
        self.assertEqual(second["payload"]["from_digest"], first_effective["digest"])
        self.s.closing.approve_correction("second", second["digest"], "operator")
        current = self.s.closing.effective("period")
        self.assertEqual(current["corrections"], ["first", "second"])
        self.assertEqual(
            sum(row["microkwh"] for row in current["payload"]["energy"]), 24000000
        )
        self.assertEqual(sum(row["minor"] for row in current["payload"]["costs"]), 2400)
        self.assertEqual(self.s.closing.get("period")["payload"], original["payload"])
        self.assertTrue(self.s.closing.verify("period")["verified"])

    def test_schedule_only_correction_redistributes_products(self):
        self.freeze()
        self.s.production.put("shift", "m", 0, 7200, {"b": 1}, expected_revision=1)
        proposal = self.s.closing.propose_correction(
            "product", "period", "production attribution"
        )
        differences = proposal["payload"]["differences"]
        self.assertEqual(sum(row["delta"] for row in differences["energy"]), 0)
        self.assertEqual(sum(row["delta"] for row in differences["costs"]), 0)
        self.assertEqual([row["product"] for row in differences["energy"]], ["a", "b"])
        self.s.closing.approve_correction("product", proposal["digest"], "operator")
        self.assertEqual(
            self.s.closing.effective("period")["payload"]["energy"][0]["product"], "b"
        )
        self.assertEqual(
            self.s.closing.get("period")["payload"]["energy"][0]["product"], "a"
        )
        self.assertFalse(self.s.closing.drift("period")["changed"])

    def test_rate_only_correction_keeps_energy(self):
        self.freeze()
        impact = self.s.tariffs.revision_impact("rate")
        self.assertEqual(impact["closures"], ["period"])
        self.s.tariffs.put("rate", "m", 0, 7200, "1.5", expected_revision=1)
        proposed = self.s.closing.propose_correction(
            "price", "period", "corrected rate"
        )
        self.assertEqual(proposed["payload"]["differences"]["energy"], [])
        self.assertEqual(proposed["payload"]["differences"]["costs"][0]["delta"], 1000)
        self.s.closing.approve_correction("price", proposed["digest"], "operator")
        self.assertEqual(
            self.s.closing.effective("period")["payload"]["costs"][0]["minor"], 3000
        )
        self.assertEqual(
            self.s.closing.get("period")["payload"]["costs"][0]["minor"], 2000
        )
        self.assertTrue(self.s.store.integrity()["verified"])

    def test_coverage_failure_preserves_prior_effective_period(self):
        frozen = self.freeze()
        self.s.readings.correct("r1", 1, 0, "missing", "unavailable source")
        with self.assertRaises(EnergyError):
            self.s.closing.propose_correction("bad", "period", "cannot settle gaps")
        self.assertEqual(self.s.store.rows("SELECT * FROM corrections"), [])
        self.assertEqual(self.s.closing.effective("period")["digest"], frozen["digest"])
        self.assertTrue(self.s.closing.verify("period")["verified"])
        self.s.readings.correct("r1", 2, 11, "normal", "recovered source")
        recovered = self.s.closing.propose_correction(
            "recovered", "period", "repaired measurement"
        )
        self.assertFalse(recovered["approved"])
        self.assertEqual(self.s.closing.get("period")["revision"], 1)

    def test_freeze_retry_and_review_digest_mismatch(self):
        first = self.freeze()
        count = self.s.store.verify_audit()["count"]
        repeated = self.s.closing.freeze(
            "period", ["m"], 0, 7200, first["digest"], "controller"
        )
        self.assertEqual(first, repeated)
        self.assertEqual(self.s.store.verify_audit()["count"], count)
        with self.assertRaises(EnergyError):
            self.s.closing.freeze("period", ["m"], 0, 7200, "wrong", "controller")
        self.s.readings.correct("r2", 1, 21, "normal", "adjustment")
        correction = self.s.closing.propose_correction("c", "period", "adjustment")
        with self.assertRaises(EnergyError):
            self.s.closing.approve_correction("c", "wrong", "controller")
        self.assertFalse(self.s.closing.correction("c")["approved"])
        self.assertEqual(correction["closure"], "period")

    def test_correction_identity_cannot_be_reused_for_other_reason(self):
        self.freeze()
        self.s.readings.correct("r2", 1, 21, "normal", "change")
        first = self.s.closing.propose_correction("c", "period", "original")
        self.assertEqual(
            self.s.closing.propose_correction("c", "period", "original"), first
        )
        with self.assertRaises(EnergyError):
            self.s.closing.propose_correction("c", "period", "different")
        self.assertEqual(len(self.s.store.rows("SELECT * FROM corrections")), 1)
        self.assertEqual(
            self.s.closing.correction("c")["payload"]["reason"], "original"
        )
        self.assertFalse(self.s.closing.correction("c")["approved"])
        self.assertEqual(self.s.closing.effective("period")["corrections"], [])

    def test_emission_requirement_is_explicit(self):
        permissive = self.s.closing.preview(["m"], 0, 7200)
        self.assertFalse(permissive["payload"]["reports"][0]["emissions"]["complete"])
        with self.assertRaises(EnergyError):
            self.s.closing.preview(["m"], 0, 7200, require_factors=True)
        self.s.emissions.put("f", "m", 0, 3600, "500", "local synthetic")
        with self.assertRaises(EnergyError):
            self.s.closing.preview(["m"], 0, 7200, require_factors=True)
        self.s.emissions.put("g", "m", 3600, 7200, "400", "local synthetic")
        strict = self.s.closing.preview(["m"], 0, 7200, require_factors=True)
        self.assertTrue(strict["payload"]["reports"][0]["emissions"]["complete"])
        self.assertEqual(
            sum(row["milligrams"] for row in strict["payload"]["emissions"]), 9000000
        )

    def test_nested_storage_rollback_preserves_outer_mutation(self):
        with self.s.store.atomic():
            self.s.catalog.register("outer", "Outer")
            try:
                with self.s.store.atomic():
                    self.s.catalog.register("inner", "Inner")
                    raise RuntimeError("simulated failure")
            except RuntimeError:
                pass
            self.s.catalog.register("after", "After")
        self.assertEqual(
            [row["id"] for row in self.s.catalog.list()], ["after", "m", "outer"]
        )
        self.assertEqual(self.s.store.depth, 0)
        self.assertTrue(self.s.store.verify_audit()["verified"])
        self.assertTrue(self.s.quality.system()["ok"])

    def test_storage_payload_tamper_blocks_reads(self):
        self.s.store.connection.execute(
            "UPDATE readings SET digest='wrong' WHERE id='r1'"
        )
        with self.assertRaises(EnergyError) as caught:
            self.s.readings.get("r1")
        self.assertEqual(caught.exception.code, "integrity")
        with self.assertRaises(EnergyError):
            self.s.reconstruction.reconstruct("m", 0, 7200)
        with self.assertRaises(EnergyError):
            self.s.store.integrity()
        self.assertEqual(
            self.s.store.one("SELECT value FROM readings WHERE id='r1'")["value"], 10
        )

    def test_audit_pages_have_stable_continuation(self):
        first = self.s.store.audit_page(limit=3)
        self.assertEqual([row["seq"] for row in first["rows"]], [1, 2, 3])
        self.assertEqual(first["next"], 3)
        second = self.s.store.audit_page(after=3, limit=100)
        self.assertEqual(second["rows"][0]["seq"], 4)
        self.assertIsNone(second["next"])
        sequence = [row["seq"] for row in first["rows"] + second["rows"]]
        self.assertEqual(
            sequence, list(range(1, self.s.store.verify_audit()["count"] + 1))
        )
        self.assertEqual(self.s.store.audit_page(after=1000)["rows"], [])
        with self.assertRaises(EnergyError):
            self.s.store.audit_page(limit=0)

    def test_fleet_and_balance_reject_duplicate_or_cross_medium(self):
        with self.assertRaises(EnergyError):
            self.s.reconstruction.fleet(["m", "m"], 0, 7200)
        with self.assertRaises(EnergyError):
            self.s.allocation.fleet(["m", "m"], 0, 7200)
        self.s.catalog.register("heat", "Heat", medium="heat")
        with self.assertRaises(EnergyError):
            self.s.balance.compare("m", ["heat"], 0, 7200)
        with self.assertRaises(EnergyError):
            self.s.balance.compare("m", ["m"], 0, 7200)
        report = self.s.reconstruction.fleet(["m"], 0, 7200, True)
        self.assertEqual(report["totals"][0]["microkwh"], "20000000/1")
        self.assertTrue(report["complete"])

    def test_topology_rejects_shared_child_and_unreachable_nodes(self):
        for name in ["left", "right", "leaf", "unused"]:
            self.s.catalog.register(name, name)
        with self.assertRaises(EnergyError):
            self.s.balance.tree(
                "m",
                {"m": ["left", "right"], "left": ["leaf"], "right": ["leaf"]},
                0,
                7200,
            )
        with self.assertRaises(EnergyError):
            self.s.balance.tree("m", {"m": ["left"], "unused": []}, 0, 7200)
        report = self.s.balance.tree("m", {}, 0, 7200)
        self.assertEqual(report["meters"], ["m"])
        self.assertTrue(report["complete"])
        self.assertEqual(report["reports"], [])

    def test_import_errors_include_line_and_never_write(self):
        with self.assertRaises(EnergyError) as caught:
            self.s.readings.parse('{"id":"x"}\n{bad}\n')
        self.assertEqual(caught.exception.context["line"], 2)
        for content in ["id,id\na,b\n", "id,value\na,-1\n", "id,value\na\n"]:
            with self.assertRaises(EnergyError):
                self.s.readings.parse(content, "csv")
        with self.assertRaises(EnergyError):
            self.s.readings.parse("", "jsonl")
        self.assertEqual(len(self.s.readings.segment("seg")), 2)
        self.assertEqual(self.s.store.rows("SELECT * FROM imports"), [])

    def test_local_cli_returns_structured_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "request.json"
            db = Path(folder) / "cli.db"
            path.write_text('{"action":"unknown"}')
            out = io.StringIO()
            error = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
                status = main(["--db", str(db), "run", str(path)])
            self.assertEqual(status, 2)
            self.assertFalse(json.loads(error.getvalue())["ok"])
            self.assertEqual(json.loads(error.getvalue())["error"]["code"], "action")
            self.assertEqual(out.getvalue(), "")
            self.assertTrue(db.is_file())

    def test_local_cli_can_register_then_query_persistent_meter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "request.json"
            db = Path(folder) / "cli.db"
            path.write_text(
                canonical(
                    {
                        "action": "catalog.register",
                        "arguments": {"meter": "m", "name": "CLI meter"},
                    }
                )
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "run", str(path)]), 0)
            self.assertEqual(
                json.loads(output.getvalue())["result"]["name"], "CLI meter"
            )
            path.write_text(
                canonical({"action": "catalog.get", "arguments": {"meter": "m"}})
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "run", str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())["result"]["unit"], "kWh")

    def test_reopen_keeps_frozen_snapshot_and_pending_correction(self):
        frozen = self.freeze()
        self.s.readings.correct("r2", 1, 22, "normal", "correction")
        correction = self.s.closing.propose_correction(
            "pending", "period", "review pending"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "restart.db"
            self.s.store.backup(path)
            with EnergyService(path) as restarted:
                self.assertEqual(
                    restarted.closing.get("period")["digest"], frozen["digest"]
                )
                self.assertEqual(
                    restarted.closing.correction("pending")["digest"],
                    correction["digest"],
                )
                self.assertFalse(restarted.closing.correction("pending")["approved"])
                restarted.closing.approve_correction(
                    "pending", correction["digest"], "operator"
                )
                self.assertEqual(
                    restarted.closing.effective("period")["corrections"], ["pending"]
                )
                self.assertTrue(restarted.store.integrity()["verified"])

    def test_demand_incomplete_window_blocks_charge(self):
        self.s.readings.correct("r1", 1, 0, "missing", "source gap")
        windows = self.s.demand.windows("m", 0, 7200, 900)
        self.assertFalse(windows["complete"])
        self.assertIsNone(windows["peak"])
        with self.assertRaises(EnergyError):
            self.s.demand.charge(windows, "10")
        rolling = self.s.demand.rolling_peak("m", 0, 7200, 900)
        self.assertIsNone(rolling["peak"])
        self.assertGreater(rolling["incomplete_candidates"], 0)
        self.assertFalse(rolling["complete"])

    def test_daily_cost_and_quality_tie_out(self):
        allocation = self.s.allocation.allocate("m", 0, 7200)
        daily = self.s.tariffs.daily_cost(allocation)
        self.assertEqual(daily["rows"][0]["exact_minor"], "2000/1")
        self.assertEqual(daily["rows"][0]["date"], "1970-01-01")
        cost = self.s.tariffs.integrate(allocation)
        self.assertTrue(self.s.quality.conservation(allocation, cost)["ok"])
        allocation["products"][0]["microkwh"] += 1
        checks = self.s.quality.conservation(allocation, cost)
        self.assertFalse(checks["ok"])
        self.assertIn("integer_energy", checks["findings"])
        self.assertEqual(self.s.allocation.allocate("m", 0, 7200)["microkwh"], 20000000)

    def test_shift_import_has_atomic_overlap_rejection(self):
        shifts = [
            {"id": "one", "start_minute": 0, "end_minute": 60},
            {"id": "two", "start_minute": 60, "end_minute": 120},
        ]
        before = self.s.production.list("m", 0, 7200)
        with self.assertRaises(EnergyError):
            self.s.production.import_shifts(
                "new", "m", 0, 7200, shifts, {"one": {"a": 1}, "two": {"b": 1}}
            )
        self.assertEqual(self.s.production.list("m", 0, 7200), before)
        self.s.catalog.register("other", "Other")
        result = self.s.production.import_shifts(
            "new", "other", 0, 7200, shifts, {"one": {"a": 1}, "two": {"b": 1}}
        )
        self.assertEqual(result["count"], 2)
        self.assertTrue(self.s.production.coverage("other", 0, 7200)["complete"])
        self.assertEqual(
            self.s.production.products("other", 0, 7200)[1]["active_seconds"], 3600
        )
