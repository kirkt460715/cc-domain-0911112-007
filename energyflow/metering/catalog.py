from ..values import (
    key,
    text,
    integer,
    instant,
    canonical,
    fingerprint,
    reject,
    object_fields,
)


class Catalog:
    def __init__(self, store):
        self.store = store

    def register(
        self, meter, name, medium="electricity", direction="import", unit="kWh"
    ):
        key(meter)
        name = text(name, 300)
        if medium not in {"electricity", "heat", "gas"}:
            reject("input", "unknown energy medium")
        if direction not in {"import", "export"}:
            reject("input", "unknown energy direction")
        if unit != "kWh":
            reject(
                "unit", "energy must be normalized to kWh at the measurement boundary"
            )
        payload = {
            "id": meter,
            "name": name,
            "medium": medium,
            "direction": direction,
            "unit": unit,
        }
        with self.store.atomic() as con:
            old = self.store.one("SELECT id FROM meters WHERE id=?", (meter,))
            if old:
                if self.get(meter) != payload:
                    reject("conflict", "meter definition is immutable")
                return payload
            con.execute(
                "INSERT INTO meters VALUES(?,?,?)",
                (meter, canonical(payload), fingerprint(payload)),
            )
            self.store.event("meter_registered", meter, payload)
        return payload

    def get(self, meter):
        return self.store.payload("meters", meter)[1]

    def list(self, medium=None, direction=None):
        rows = [
            self.get(row["id"])
            for row in self.store.rows("SELECT id FROM meters ORDER BY id")
        ]
        if medium is not None:
            rows = [row for row in rows if row["medium"] == medium]
        if direction is not None:
            rows = [row for row in rows if row["direction"] == direction]
        return rows

    def install(
        self,
        segment,
        meter,
        start,
        baseline,
        microkwh_per_tick=1000000,
        modulus=None,
        max_gap=3600,
        max_kw="1000000",
    ):
        from ..values import fixed

        key(segment)
        self.get(meter)
        start = instant(start)
        baseline = integer(baseline, 0, 10**12)
        factor = integer(microkwh_per_tick, 1, 10**9)
        max_gap = integer(max_gap, 1, 31 * 86400)
        maximum = fixed(max_kw)
        if maximum <= 0:
            reject("input", "maximum meter power must be positive")
        if modulus is not None:
            integer(modulus, 2, 10**12)
            if baseline >= modulus:
                reject("input", "baseline must be less than counter modulus")
        payload = {
            "id": segment,
            "meter": meter,
            "start": start,
            "end": None,
            "baseline": baseline,
            "microkwh_per_tick": factor,
            "modulus": modulus,
            "max_gap": max_gap,
            "max_microkw": maximum,
        }
        with self.store.atomic() as con:
            old = self.store.one("SELECT id FROM segments WHERE id=?", (segment,))
            if old:
                current = self.segment(segment)
                current["end"] = None
                if current != payload:
                    reject("conflict", "segment identity differs")
                return self.segment(segment)
            conflict = self.store.one(
                "SELECT id FROM segments WHERE meter=? AND (end IS NULL OR end>?)",
                (meter, start),
            )
            future = self.store.one(
                "SELECT id FROM segments WHERE meter=? AND start>=?", (meter, start)
            )
            if conflict or future:
                reject(
                    "overlap", "close previous segment before installing replacement"
                )
            con.execute(
                "INSERT INTO segments VALUES(?,?,?,NULL,?,?)",
                (segment, meter, start, canonical(payload), fingerprint(payload)),
            )
            self.store.event("meter_installed", segment, payload)
        return payload

    def segment(self, segment):
        return self.store.payload("segments", segment)[1]

    def segments(self, meter):
        self.get(meter)
        return [
            self.segment(row["id"])
            for row in self.store.rows(
                "SELECT id FROM segments WHERE meter=? ORDER BY start,id", (meter,)
            )
        ]

    def retire(self, segment, end):
        end = instant(end)
        with self.store.atomic() as con:
            payload = self.segment(segment)
            if payload["end"] is not None:
                if payload["end"] != end:
                    reject("conflict", "segment already retired at another instant")
                return payload
            if end <= payload["start"]:
                reject("time", "retirement must follow installation")
            if self.store.one(
                "SELECT id FROM readings WHERE segment=? AND at>?", (segment, end)
            ):
                reject("state", "readings exist after proposed retirement")
            last = self.store.one(
                "SELECT kind FROM readings WHERE segment=? AND at=?", (segment, end)
            )
            if last is None or last["kind"] == "missing":
                reject("coverage", "retirement requires a usable terminal reading")
            payload["end"] = end
            con.execute(
                "UPDATE segments SET end=?,payload=?,digest=? WHERE id=?",
                (end, canonical(payload), fingerprint(payload), segment),
            )
            self.store.event("meter_retired", segment, {"end": end})
        return payload

    def replacement(
        self,
        previous,
        segment,
        at,
        baseline,
        microkwh_per_tick=None,
        modulus=None,
        max_gap=None,
        max_kw=None,
    ):
        from ..values import decimal_text

        with self.store.atomic():
            old = self.segment(previous)
            self.retire(previous, at)
            result = self.install(
                segment,
                old["meter"],
                at,
                baseline,
                old["microkwh_per_tick"]
                if microkwh_per_tick is None
                else microkwh_per_tick,
                modulus,
                old["max_gap"] if max_gap is None else max_gap,
                decimal_text(old["max_microkw"]) if max_kw is None else max_kw,
            )
            self.store.event(
                "meter_replaced", segment, {"previous": previous, "at": instant(at)}
            )
        return result

    def coverage(self, meter, start, end):
        from ..values import period

        start, end = period(start, end)
        spans = []
        gaps = []
        cursor = start
        for segment in self.segments(meter):
            left = max(start, segment["start"])
            right = min(end, segment["end"] if segment["end"] is not None else end)
            if right <= left:
                continue
            if left > cursor:
                gaps.append(
                    {"start": cursor, "end": left, "reason": "no_installed_meter"}
                )
            spans.append({"segment": segment["id"], "start": left, "end": right})
            cursor = max(cursor, right)
        if cursor < end:
            gaps.append({"start": cursor, "end": end, "reason": "no_installed_meter"})
        return {
            "meter": meter,
            "start": start,
            "end": end,
            "segments": spans,
            "gaps": gaps,
            "complete": not gaps,
        }

    def inspect(self):
        findings = []
        for meter in self.list():
            previous_end = None
            for index, segment in enumerate(self.segments(meter["id"])):
                if index and (previous_end is None or previous_end > segment["start"]):
                    findings.append(
                        {"kind": "segment_overlap", "segment": segment["id"]}
                    )
                previous_end = segment["end"]
                for reading in self.store.rows(
                    "SELECT id,at,value FROM readings WHERE segment=?", (segment["id"],)
                ):
                    if reading["at"] <= segment["start"] or (
                        segment["end"] is not None and reading["at"] > segment["end"]
                    ):
                        findings.append(
                            {
                                "kind": "reading_outside_segment",
                                "reading": reading["id"],
                            }
                        )
                    if (
                        segment["modulus"] is not None
                        and reading["value"] >= segment["modulus"]
                    ):
                        findings.append(
                            {
                                "kind": "reading_outside_modulus",
                                "reading": reading["id"],
                            }
                        )
        return {"ok": not findings, "findings": findings}
