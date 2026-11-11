from fractions import Fraction
from .values import period, fraction, rational, integer, reject, fingerprint


class Quality:
    def __init__(
        self, store, catalog, readings, reconstructor, calendar, tariffs, factors
    ):
        self.store = store
        self.catalog = catalog
        self.readings = readings
        self.reconstructor = reconstructor
        self.calendar = calendar
        self.tariffs = tariffs
        self.factors = factors

    def inspect(self, meter, start, end):
        start, end = period(start, end)
        report = self.reconstructor.reconstruct(meter, start, end)
        production = self.calendar.coverage(meter, start, end)
        rates = self.tariffs.coverage(meter, start, end)
        findings = []
        for interval in report["intervals"]:
            for flag in interval["flags"]:
                findings.append(
                    {
                        "category": "measurement",
                        "code": flag,
                        "severity": "error",
                        "start": interval["start"],
                        "end": interval["end"],
                        "evidence": interval["evidence"],
                    }
                )
        for gap in production["gaps"]:
            findings.append(
                {
                    "category": "production",
                    "code": "unassigned_time",
                    "severity": "warning",
                    **gap,
                }
            )
        for gap in rates["gaps"]:
            findings.append(
                {
                    "category": "tariff",
                    "code": "unpriced_time",
                    "severity": "error",
                    **gap,
                }
            )
        for segment in self.catalog.segments(meter):
            readings = self.readings.segment(segment["id"])
            for left, right in zip(readings, readings[1:]):
                if (
                    right["value"] == left["value"]
                    and right["kind"] == "normal"
                    and left["kind"] == "normal"
                ):
                    a = max(start, left["at"])
                    b = min(end, right["at"])
                    if a < b:
                        findings.append(
                            {
                                "category": "measurement",
                                "code": "flat_counter",
                                "severity": "warning",
                                "start": a,
                                "end": b,
                                "readings": [left["id"], right["id"]],
                            }
                        )
        findings.sort(key=lambda row: (row["start"], row["category"], row["code"]))
        return {
            "meter": meter,
            "start": start,
            "end": end,
            "findings": findings,
            "errors": sum(row["severity"] == "error" for row in findings),
            "warnings": sum(row["severity"] == "warning" for row in findings),
            "coverage": report["coverage"],
            "digest": fingerprint(findings),
        }

    def readiness(
        self, meters, start, end, require_assigned=True, require_factors=False
    ):
        start, end = period(start, end)
        reports = []
        for meter in sorted(meters):
            report = self.inspect(meter, start, end)
            blocked = report["errors"] > 0
            if require_assigned and any(
                row["category"] == "production" for row in report["findings"]
            ):
                blocked = True
            factors = self.factors.list(meter, start, end)
            from .calendar import coverage_summary

            carbon = coverage_summary(
                start, end, [(row["start"], row["end"]) for row in factors]
            )
            if require_factors and not carbon["complete"]:
                blocked = True
            reports.append(
                {
                    "meter": meter,
                    "ready": not blocked,
                    "quality": report,
                    "factor_coverage": carbon,
                }
            )
        return {
            "ready": all(row["ready"] for row in reports),
            "meters": reports,
            "require_assigned": require_assigned,
            "require_factors": require_factors,
        }

    def staleness(self, meter, at, maximum_age=3600):
        from .values import instant

        at = instant(at)
        integer(maximum_age, 0, 366 * 86400)
        readings = []
        for segment in self.catalog.segments(meter):
            readings.extend(
                row
                for row in self.readings.segment(segment["id"])
                if row["at"] <= at and row["kind"] != "missing"
            )
        latest = max(readings, key=lambda row: row["at"], default=None)
        if latest is None:
            return {
                "meter": meter,
                "status": "no_readings",
                "latest": None,
                "age_seconds": None,
            }
        age = at - latest["at"]
        return {
            "meter": meter,
            "status": "stale" if age > maximum_age else "current",
            "latest": latest["id"],
            "age_seconds": age,
            "revision": latest["revision"],
        }

    def revision_summary(self, meter):
        result = []
        for segment in self.catalog.segments(meter):
            for reading in self.readings.segment(segment["id"]):
                if reading["revision"] <= 1:
                    continue
                history = self.readings.history(reading["id"])
                result.append(
                    {
                        "reading": reading["id"],
                        "segment": segment["id"],
                        "at": reading["at"],
                        "revisions": len(history),
                        "original_value": history[0]["value"],
                        "current_value": reading["value"],
                        "reasons": [row["reason"] for row in history[1:]],
                    }
                )
        result.sort(key=lambda row: (row["at"], row["reading"]))
        return {"meter": meter, "corrected_readings": result, "count": len(result)}

    def conservation(self, allocation, cost, emissions=None):
        findings = []
        exact = sum(
            (fraction(row["exact_microkwh"]) for row in allocation["products"]),
            Fraction(0),
        )
        if exact != fraction(allocation["exact_microkwh"]):
            findings.append("exact_energy")
        if (
            sum(row["microkwh"] for row in allocation["products"])
            != allocation["microkwh"]
        ):
            findings.append("integer_energy")
        for row in allocation["slices"]:
            if sum(fraction(value) for value in row["products"].values()) != fraction(
                row["microkwh"]
            ):
                findings.append("slice_energy:" + row["interval"])
        for total in cost["totals"]:
            rows = [
                row for row in cost["products"] if row["currency"] == total["currency"]
            ]
            if sum(row["minor"] for row in rows) != total["minor"]:
                findings.append("integer_cost:" + total["currency"])
            if sum(fraction(row["exact_minor"]) for row in rows) != fraction(
                total["exact_minor"]
            ):
                findings.append("exact_cost:" + total["currency"])
        if emissions is not None:
            if (
                sum(row["milligrams"] for row in emissions["products"])
                != emissions["milligrams"]
            ):
                findings.append("integer_emissions")
        return {"ok": not findings, "findings": findings}

    def system(self):
        storage = self.store.integrity()
        catalog = self.catalog.inspect()
        return {
            "ok": storage["verified"] and catalog["ok"],
            "storage": storage,
            "catalog": catalog,
        }
