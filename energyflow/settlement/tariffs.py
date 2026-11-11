from fractions import Fraction
from ..values import (
    key,
    period,
    fixed,
    integer,
    canonical,
    fingerprint,
    reject,
    fraction,
    rational,
    round_even,
    distribute,
    text,
)
from ..calendar import split, coverage_summary


class Tariffs:
    def __init__(self, store, catalog):
        self.store = store
        self.catalog = catalog

    def put(
        self,
        rate,
        meter,
        start,
        end,
        price,
        currency="CNY",
        expected_revision=0,
        label="",
    ):
        key(rate)
        self.catalog.get(meter)
        start, end = period(start, end)
        integer(expected_revision)
        price = fixed(price)
        if currency not in {"CNY", "USD", "EUR"}:
            reject("currency", "unsupported settlement currency")
        payload = {
            "id": rate,
            "meter": meter,
            "start": start,
            "end": end,
            "microcurrency_per_kwh": price,
            "currency": currency,
            "label": text(label, 200, True),
        }
        with self.store.atomic() as con:
            old = self.store.one("SELECT revision FROM rates WHERE id=?", (rate,))
            version = old["revision"] if old else 0
            if expected_revision != version:
                reject("conflict", "tariff revision changed")
            if self.store.one(
                "SELECT id FROM rates WHERE meter=? AND start<? AND end>? AND id!=?",
                (meter, end, start, rate),
            ):
                reject("overlap", "tariff intervals overlap")
            if old and self.get(rate)["meter"] != meter:
                reject("conflict", "tariff cannot change meter")
            con.execute(
                "INSERT INTO rates VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET start=excluded.start,end=excluded.end,revision=excluded.revision,payload=excluded.payload,digest=excluded.digest",
                (
                    rate,
                    meter,
                    start,
                    end,
                    version + 1,
                    canonical(payload),
                    fingerprint(payload),
                ),
            )
            self.store.event(
                "tariff_saved",
                rate,
                {"revision": version + 1, "digest": fingerprint(payload)},
            )
        return self.get(rate)

    def get(self, rate):
        row, payload = self.store.payload("rates", rate)
        return {**payload, "revision": row["revision"], "digest": row["digest"]}

    def list(self, meter, start, end):
        start, end = period(start, end)
        self.catalog.get(meter)
        return [
            self.get(row["id"])
            for row in self.store.rows(
                "SELECT id FROM rates WHERE meter=? AND start<? AND end>? ORDER BY start,id",
                (meter, end, start),
            )
        ]

    def coverage(self, meter, start, end):
        start, end = period(start, end)
        rows = self.list(meter, start, end)
        report = coverage_summary(
            start, end, [(row["start"], row["end"]) for row in rows]
        )
        return {
            **report,
            "currencies": sorted({row["currency"] for row in rows}),
            "digest": fingerprint(rows),
        }

    def integrate(self, allocation, strict=True):
        meter = allocation["meter"]
        rates = self.list(meter, allocation["start"], allocation["end"])
        cuts = {value for row in rates for value in [row["start"], row["end"]]}
        cost = {}
        details = []
        uncovered = []
        for interval in allocation["slices"]:
            for left, right in split(interval["start"], interval["end"], cuts):
                rate = next(
                    (
                        row
                        for row in rates
                        if row["start"] <= left and row["end"] >= right
                    ),
                    None,
                )
                duration = Fraction(right - left, interval["end"] - interval["start"])
                if rate is None:
                    uncovered.append(
                        {"start": left, "end": right, "interval": interval["interval"]}
                    )
                    continue
                products = {}
                for product, energy in interval["products"].items():
                    amount = (
                        fraction(energy)
                        * duration
                        * Fraction(rate["microcurrency_per_kwh"], 10**10)
                    )
                    products[product] = rational(amount)
                    identity = (rate["currency"], product)
                    cost[identity] = cost.get(identity, Fraction(0)) + amount
                details.append(
                    {
                        "start": left,
                        "end": right,
                        "rate": rate["id"],
                        "rate_revision": rate["revision"],
                        "currency": rate["currency"],
                        "product_exact_minor": products,
                        "interval": interval["interval"],
                    }
                )
        if uncovered and strict:
            reject("coverage", "tariff does not cover allocated energy", gaps=uncovered)
        currencies = sorted({currency for currency, product in cost})
        totals = []
        products = []
        for currency in currencies:
            exact = {
                product: value
                for (code, product), value in cost.items()
                if code == currency
            }
            subtotal = sum(exact.values())
            rounded = round_even(subtotal)
            if subtotal > 0:
                allocated = distribute(rounded, exact)
            else:
                allocated = {product: 0 for product in exact}
            totals.append(
                {
                    "currency": currency,
                    "exact_minor": rational(subtotal),
                    "minor": rounded,
                }
            )
            for product in sorted(exact):
                products.append(
                    {
                        "currency": currency,
                        "product": product,
                        "minor": allocated[product],
                        "exact_minor": rational(exact[product]),
                    }
                )
        result = {
            "meter": meter,
            "totals": totals,
            "products": products,
            "details": details,
            "gaps": uncovered,
            "complete": allocation["complete"] and not uncovered,
            "allocation_digest": allocation["digest"],
            "tariff_digest": fingerprint(rates),
        }
        result["digest"] = fingerprint(result)
        return result

    def quote(self, meter, start, end, quantity):
        start, end = period(start, end)
        quantity = fraction(quantity)
        if quantity < 0:
            reject("input", "quoted energy must be nonnegative")
        allocation = {
            "meter": meter,
            "start": start,
            "end": end,
            "slices": [
                {
                    "start": start,
                    "end": end,
                    "interval": "quote",
                    "products": {"quoted": rational(quantity)},
                }
            ],
            "complete": True,
            "digest": fingerprint([meter, start, end, rational(quantity)]),
        }
        return self.integrate(allocation)

    def revision_impact(self, rate):
        value = self.get(rate)
        closures = []
        for row in self.store.rows(
            "SELECT id FROM closures WHERE start<? AND end>?",
            (value["end"], value["start"]),
        ):
            _, closure = self.store.payload("closures", row["id"])
            if value["meter"] in closure["meters"]:
                closures.append(row["id"])
        return {
            "rate": rate,
            "revision": value["revision"],
            "closures": sorted(closures),
        }

    def daily_cost(self, allocation):
        result = self.integrate(allocation)
        from ..calendar import utc_days

        days = {}
        for row in result["details"]:
            duration = row["end"] - row["start"]
            for day in utc_days(row["start"], row["end"]):
                for product, amount in row["product_exact_minor"].items():
                    identity = (day["date"], row["currency"], product)
                    value = fraction(amount) * Fraction(day["seconds"], duration)
                    days[identity] = days.get(identity, Fraction(0)) + value
        return {
            "rows": [
                {
                    "date": date,
                    "currency": currency,
                    "product": product,
                    "exact_minor": rational(value),
                }
                for (date, currency, product), value in sorted(days.items())
            ],
            "basis": "exact_cost_before_period_rounding",
        }
