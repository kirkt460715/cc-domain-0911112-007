from ..values import (
    key,
    period,
    integer,
    fraction,
    rational,
    canonical,
    fingerprint,
    reject,
    object_fields,
    items,
    text,
)


class ProductionCalendar:
    def __init__(self, store, catalog):
        self.store = store
        self.catalog = catalog

    def normalize_weights(self, weights):
        if not isinstance(weights, dict) or not weights or len(weights) > 1000:
            reject("input", "product weights must be a nonempty object")
        normalized = {}
        for product, value in weights.items():
            key(product)
            weight = fraction(value)
            if weight < 0:
                reject("allocation", "product weight cannot be negative")
            normalized[product] = rational(weight)
        if sum(fraction(value) for value in normalized.values()) <= 0:
            reject("allocation", "at least one product must carry positive weight")
        return {product: normalized[product] for product in sorted(normalized)}

    def put(self, schedule, meter, start, end, weights, expected_revision=0, note=""):
        key(schedule)
        self.catalog.get(meter)
        start, end = period(start, end)
        integer(expected_revision)
        normalized = self.normalize_weights(weights)
        note = text(note, 1000, True)
        payload = {
            "id": schedule,
            "meter": meter,
            "start": start,
            "end": end,
            "weights": normalized,
            "note": note,
        }
        with self.store.atomic() as con:
            old = self.store.one(
                "SELECT revision FROM schedules WHERE id=?", (schedule,)
            )
            current = old["revision"] if old else 0
            if current != expected_revision:
                reject("conflict", "production schedule revision changed")
            overlap = self.store.one(
                "SELECT id FROM schedules WHERE meter=? AND start<? AND end>? AND id!=?",
                (meter, end, start, schedule),
            )
            if overlap:
                reject(
                    "overlap", "production schedules overlap", existing=overlap["id"]
                )
            if old:
                previous = self.get(schedule)
                if previous["meter"] != meter:
                    reject("conflict", "cannot move schedule to another meter")
            revision = current + 1
            con.execute(
                "INSERT INTO schedules VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET start=excluded.start,end=excluded.end,revision=excluded.revision,payload=excluded.payload,digest=excluded.digest",
                (
                    schedule,
                    meter,
                    start,
                    end,
                    revision,
                    canonical(payload),
                    fingerprint(payload),
                ),
            )
            self.store.event(
                "production_schedule_saved",
                schedule,
                {"revision": revision, "digest": fingerprint(payload)},
            )
        return self.get(schedule)

    def get(self, schedule):
        row, payload = self.store.payload("schedules", schedule)
        return {**payload, "revision": row["revision"], "digest": row["digest"]}

    def list(self, meter, start, end):
        self.catalog.get(meter)
        start, end = period(start, end)
        return [
            self.get(row["id"])
            for row in self.store.rows(
                "SELECT id FROM schedules WHERE meter=? AND start<? AND end>? ORDER BY start,id",
                (meter, end, start),
            )
        ]

    def at(self, meter, at):
        from ..values import instant

        at = instant(at)
        rows = self.store.rows(
            "SELECT id FROM schedules WHERE meter=? AND start<=? AND end>? ORDER BY id",
            (meter, at, at),
        )
        if len(rows) > 1:
            reject("integrity", "overlapping production schedules")
        return self.get(rows[0]["id"]) if rows else None

    def replace_span(self, meter, start, end, schedules, expected_digest):
        start, end = period(start, end)
        items(schedules, 1, 1000)
        with self.store.atomic() as con:
            previous = self.list(meter, start, end)
            if fingerprint(previous) != expected_digest:
                reject("conflict", "schedule span changed")
            if any(row["start"] < start or row["end"] > end for row in previous):
                reject("state", "replacement must fully contain previous schedules")
            identifiers = set()
            for row in schedules:
                object_fields(
                    row,
                    {"id", "start", "end", "weights", "note"},
                    {"id", "start", "end", "weights"},
                )
                if row["id"] in identifiers:
                    reject("duplicate", "duplicate replacement schedule")
                identifiers.add(row["id"])
                left, right = period(row["start"], row["end"])
                if left < start or right > end:
                    reject("time", "replacement schedule outside span")
                self.normalize_weights(row["weights"])
            for row in previous:
                con.execute("DELETE FROM schedules WHERE id=?", (row["id"],))
            result = []
            for row in schedules:
                result.append(
                    self.put(
                        row["id"],
                        meter,
                        row["start"],
                        row["end"],
                        row["weights"],
                        note=row.get("note", ""),
                    )
                )
            self.store.event(
                "production_span_replaced",
                meter,
                {
                    "start": start,
                    "end": end,
                    "previous": previous,
                    "replacement": result,
                },
            )
        return result

    def import_shifts(
        self, prefix, meter, start, end, shifts, products, offset_minutes=0
    ):
        from ..calendar import shift_intervals

        key(prefix)
        spans = shift_intervals(start, end, shifts, offset_minutes)
        if set(products) != {row["id"] for row in shifts}:
            reject("input", "weights must be supplied for every shift")
        result = []
        with self.store.atomic():
            for span in spans:
                identifier = prefix + ":" + span["date"] + ":" + span["id"]
                result.append(
                    self.put(
                        identifier,
                        meter,
                        span["start"],
                        span["end"],
                        products[span["id"]],
                        note="Fixed-offset production shift",
                    )
                )
        return {"schedules": result, "count": len(result)}

    def coverage(self, meter, start, end):
        from ..calendar import coverage_summary

        rows = self.list(meter, start, end)
        report = coverage_summary(
            *period(start, end), [(row["start"], row["end"]) for row in rows]
        )
        return {**report, "meter": meter, "schedule_digest": fingerprint(rows)}

    def products(self, meter, start, end):
        values = {}
        for row in self.list(meter, start, end):
            left = max(row["start"], period(start, end)[0])
            right = min(row["end"], period(start, end)[1])
            for product, weight in row["weights"].items():
                item = values.setdefault(
                    product, {"product": product, "active_seconds": 0, "schedules": []}
                )
                if fraction(weight) > 0:
                    item["active_seconds"] += right - left
                    item["schedules"].append(row["id"])
        return [values[product] for product in sorted(values)]
