from fractions import Fraction
from ..values import (
    period,
    fraction,
    rational,
    round_even,
    distribute,
    fingerprint,
    reject,
    integer,
)
from ..calendar import split


class Allocator:
    def __init__(self, reconstructor, calendar):
        self.reconstructor = reconstructor
        self.calendar = calendar

    def allocate(self, meter, start, end, strict=True, unassigned="unassigned"):
        from ..values import key

        key(unassigned)
        start, end = period(start, end)
        reconstruction = self.reconstructor.reconstruct(meter, start, end, strict)
        schedules = self.calendar.list(meter, start, end)
        cuts = {start, end}
        for schedule in schedules:
            cuts.update([schedule["start"], schedule["end"]])
        slices = []
        unassigned_seconds = 0
        for interval in reconstruction["intervals"]:
            if not interval["valid"]:
                continue
            for left, right in split(interval["start"], interval["end"], cuts):
                schedule = next(
                    (
                        row
                        for row in schedules
                        if row["start"] <= left and row["end"] >= right
                    ),
                    None,
                )
                quantity = fraction(interval["microkwh"]) * Fraction(
                    right - left, interval["seconds"]
                )
                if schedule is None:
                    weights = {unassigned: Fraction(1)}
                    unassigned_seconds += right - left
                else:
                    weights = {
                        name: fraction(value)
                        for name, value in schedule["weights"].items()
                    }
                denominator = sum(weights.values())
                parts = {
                    name: quantity * value / denominator
                    for name, value in weights.items()
                }
                slices.append(
                    {
                        "start": left,
                        "end": right,
                        "interval": interval["id"],
                        "schedule": schedule["id"] if schedule else None,
                        "schedule_revision": schedule["revision"] if schedule else None,
                        "microkwh": rational(quantity),
                        "products": {
                            name: rational(value)
                            for name, value in sorted(parts.items())
                        },
                    }
                )
        exact = {}
        for row in slices:
            for product, amount in row["products"].items():
                exact[product] = exact.get(product, Fraction(0)) + fraction(amount)
        total = fraction(reconstruction["microkwh"])
        if sum(exact.values()) != total:
            reject("integrity", "product allocation does not conserve interval energy")
        rounded_total = round_even(total)
        if exact and total:
            integers = distribute(rounded_total, exact)
        else:
            integers = {product: 0 for product in exact}
        rows = [
            {
                "product": product,
                "exact_microkwh": rational(exact[product]),
                "microkwh": integers[product],
                "rounding_microkwh": rational(
                    Fraction(integers[product]) - exact[product]
                ),
            }
            for product in sorted(exact)
        ]
        report = {
            "meter": meter,
            "start": start,
            "end": end,
            "products": rows,
            "slices": slices,
            "exact_microkwh": rational(total),
            "microkwh": rounded_total,
            "unassigned_seconds": unassigned_seconds,
            "complete": reconstruction["complete"],
            "reading_digest": reconstruction["digest"],
            "schedule_digest": fingerprint(schedules),
        }
        report["digest"] = fingerprint(report)
        return report

    def require_assigned(self, meter, start, end):
        report = self.allocate(meter, start, end)
        if report["unassigned_seconds"]:
            reject(
                "coverage",
                "production schedule does not cover every valid interval",
                seconds=report["unassigned_seconds"],
            )
        return report

    def product_trace(self, meter, start, end, product):
        from ..values import key

        key(product)
        report = self.allocate(meter, start, end, strict=False)
        slices = []
        total = Fraction(0)
        for row in report["slices"]:
            value = fraction(row["products"].get(product, "0"))
            if value == 0:
                continue
            total += value
            slices.append(
                {
                    "start": row["start"],
                    "end": row["end"],
                    "interval": row["interval"],
                    "schedule": row["schedule"],
                    "schedule_revision": row["schedule_revision"],
                    "exact_microkwh": rational(value),
                }
            )
        return {
            "meter": meter,
            "product": product,
            "slices": slices,
            "exact_microkwh": rational(total),
            "complete": report["complete"],
        }

    def compare(self, before, after):
        if (before["meter"], before["start"], before["end"]) != (
            after["meter"],
            after["start"],
            after["end"],
        ):
            reject(
                "input", "allocation comparisons require the same meter and interval"
            )
        old = {row["product"]: row for row in before["products"]}
        new = {row["product"]: row for row in after["products"]}
        rows = []
        for product in sorted(old.keys() | new.keys()):
            left = old.get(product, {}).get("microkwh", 0)
            right = new.get(product, {}).get("microkwh", 0)
            exact_left = fraction(old.get(product, {}).get("exact_microkwh", "0"))
            exact_right = fraction(new.get(product, {}).get("exact_microkwh", "0"))
            if exact_left != exact_right or left != right:
                rows.append(
                    {
                        "product": product,
                        "before_microkwh": left,
                        "after_microkwh": right,
                        "delta_microkwh": right - left,
                        "exact_delta_microkwh": rational(exact_right - exact_left),
                    }
                )
        return {
            "rows": rows,
            "delta_microkwh": after["microkwh"] - before["microkwh"],
            "reading_changed": before["reading_digest"] != after["reading_digest"],
            "schedule_changed": before["schedule_digest"] != after["schedule_digest"],
        }

    def fleet(self, meters, start, end, strict=True):
        from ..values import items

        items(meters, 1, 1000)
        if len(set(meters)) != len(meters):
            reject("duplicate", "duplicate meter in allocation fleet")
        reports = [self.allocate(meter, start, end, strict) for meter in sorted(meters)]
        grouped = {}
        for report in reports:
            meter = self.reconstructor.catalog.get(report["meter"])
            for product in report["products"]:
                identity = (meter["medium"], meter["direction"], product["product"])
                row = grouped.setdefault(
                    identity, {"microkwh": 0, "exact": Fraction(0), "meters": []}
                )
                row["microkwh"] += product["microkwh"]
                row["exact"] += fraction(product["exact_microkwh"])
                row["meters"].append(meter["id"])
        totals = []
        for (medium, direction, product), row in sorted(grouped.items()):
            totals.append(
                {
                    "medium": medium,
                    "direction": direction,
                    "product": product,
                    "microkwh": row["microkwh"],
                    "exact_microkwh": rational(row["exact"]),
                    "meters": row["meters"],
                }
            )
        return {
            "reports": reports,
            "products": totals,
            "complete": all(row["complete"] for row in reports),
        }

    def utilization(self, meter, start, end):
        report = self.allocate(meter, start, end, strict=False)
        spans = {}
        for row in report["slices"]:
            for product, amount in row["products"].items():
                if fraction(amount) <= 0:
                    continue
                spans.setdefault(product, []).append((row["start"], row["end"]))
        from ..calendar import coverage_summary

        result = []
        for product, intervals in sorted(spans.items()):
            coverage = coverage_summary(report["start"], report["end"], intervals)
            result.append(
                {
                    "product": product,
                    "active_seconds": coverage["covered_seconds"],
                    "fraction": rational(
                        Fraction(
                            coverage["covered_seconds"], report["end"] - report["start"]
                        )
                    ),
                }
            )
        return {"meter": meter, "rows": result, "complete": report["complete"]}
