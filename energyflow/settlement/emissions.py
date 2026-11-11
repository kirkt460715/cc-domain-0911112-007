from fractions import Fraction
from ..values import (
    key,
    period,
    fixed,
    integer,
    canonical,
    fingerprint,
    fraction,
    rational,
    round_even,
    reject,
    text,
)
from ..calendar import split


class EmissionFactors:
    def __init__(self, store, catalog):
        self.store = store
        self.catalog = catalog

    def put(
        self, factor, meter, start, end, grams_per_kwh, source, expected_revision=0
    ):
        key(factor)
        self.catalog.get(meter)
        start, end = period(start, end)
        integer(expected_revision)
        source = text(source, 1000)
        amount = fixed(grams_per_kwh, 3)
        payload = {
            "id": factor,
            "meter": meter,
            "start": start,
            "end": end,
            "milligrams_per_kwh": amount,
            "source": source,
        }
        with self.store.atomic() as con:
            old = self.store.one("SELECT revision FROM factors WHERE id=?", (factor,))
            revision = old["revision"] if old else 0
            if revision != expected_revision:
                reject("conflict", "emission factor revision changed")
            if self.store.one(
                "SELECT id FROM factors WHERE meter=? AND start<? AND end>? AND id!=?",
                (meter, end, start, factor),
            ):
                reject("overlap", "emission factors overlap")
            if old and self.get(factor)["meter"] != meter:
                reject("conflict", "factor cannot change meter")
            con.execute(
                "INSERT INTO factors VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET start=excluded.start,end=excluded.end,revision=excluded.revision,payload=excluded.payload,digest=excluded.digest",
                (
                    factor,
                    meter,
                    start,
                    end,
                    revision + 1,
                    canonical(payload),
                    fingerprint(payload),
                ),
            )
            self.store.event(
                "factor_saved", factor, {"revision": revision + 1, "source": source}
            )
        return self.get(factor)

    def get(self, factor):
        row, payload = self.store.payload("factors", factor)
        return {**payload, "revision": row["revision"], "digest": row["digest"]}

    def list(self, meter, start, end):
        start, end = period(start, end)
        return [
            self.get(row["id"])
            for row in self.store.rows(
                "SELECT id FROM factors WHERE meter=? AND start<? AND end>? ORDER BY start,id",
                (meter, end, start),
            )
        ]

    def integrate(self, allocation, strict=False):
        factors = self.list(allocation["meter"], allocation["start"], allocation["end"])
        cuts = {point for row in factors for point in [row["start"], row["end"]]}
        totals = {}
        details = []
        gaps = []
        for interval in allocation["slices"]:
            for start, end in split(interval["start"], interval["end"], cuts):
                factor = next(
                    (
                        row
                        for row in factors
                        if row["start"] <= start and row["end"] >= end
                    ),
                    None,
                )
                if factor is None:
                    gaps.append({"start": start, "end": end})
                    continue
                duration = Fraction(end - start, interval["end"] - interval["start"])
                quantities = {}
                for product, energy in interval["products"].items():
                    value = (
                        fraction(energy)
                        * duration
                        * Fraction(factor["milligrams_per_kwh"], 10**6)
                    )
                    totals[product] = totals.get(product, Fraction(0)) + value
                    quantities[product] = rational(value)
                details.append(
                    {
                        "start": start,
                        "end": end,
                        "factor": factor["id"],
                        "revision": factor["revision"],
                        "source": factor["source"],
                        "product_milligrams": quantities,
                    }
                )
        if gaps and strict:
            reject("coverage", "emission factors do not cover allocation", gaps=gaps)
        total = sum(totals.values(), Fraction(0))
        from ..values import distribute

        rounded = round_even(total)
        integers = (
            distribute(rounded, totals) if total > 0 else {name: 0 for name in totals}
        )
        return {
            "meter": allocation["meter"],
            "exact_milligrams": rational(total),
            "milligrams": rounded,
            "products": [
                {
                    "product": name,
                    "exact_milligrams": rational(value),
                    "milligrams": integers[name],
                }
                for name, value in sorted(totals.items())
            ],
            "details": details,
            "gaps": gaps,
            "complete": allocation["complete"] and not gaps,
            "factor_digest": fingerprint(factors),
            "allocation_digest": allocation["digest"],
        }

    def intensity(self, report, output_units):
        if not isinstance(output_units, dict):
            reject("input", "product output quantities required")
        result = []
        for row in report["products"]:
            product = row["product"]
            if product not in output_units:
                result.append(
                    {
                        "product": product,
                        "status": "missing_output",
                        "milligrams_per_unit": None,
                    }
                )
                continue
            units = fraction(output_units[product])
            if units < 0:
                reject("input", "product output cannot be negative")
            if units == 0:
                result.append(
                    {
                        "product": product,
                        "status": "zero_output",
                        "milligrams_per_unit": None,
                    }
                )
                continue
            value = fraction(row["exact_milligrams"]) / units
            result.append(
                {
                    "product": product,
                    "status": "ok",
                    "milligrams_per_unit": rational(value),
                }
            )
        return {"rows": result, "complete_factors": report["complete"]}

    def compare(self, before, after):
        if before["meter"] != after["meter"]:
            reject("input", "cannot compare emission reports from different meters")
        old = {
            row["product"]: fraction(row["exact_milligrams"])
            for row in before["products"]
        }
        new = {
            row["product"]: fraction(row["exact_milligrams"])
            for row in after["products"]
        }
        return {
            "meter": before["meter"],
            "factor_changed": before["factor_digest"] != after["factor_digest"],
            "rows": [
                {
                    "product": product,
                    "delta_milligrams": rational(
                        new.get(product, 0) - old.get(product, 0)
                    ),
                }
                for product in sorted(old.keys() | new.keys())
            ],
            "delta_milligrams": after["milligrams"] - before["milligrams"],
        }
