from ..values import (
    key,
    integer,
    instant,
    text,
    object_fields,
    items,
    canonical,
    fingerprint,
    parse_json,
    reject,
)


class Readings:
    def __init__(self, store, catalog):
        self.store = store
        self.catalog = catalog

    def normalize(self, data):
        object_fields(
            data,
            {"id", "segment", "at", "value", "kind", "note"},
            {"id", "segment", "at", "value"},
        )
        ident = key(data["id"])
        segment = self.catalog.segment(key(data["segment"]))
        at = instant(data["at"])
        value = integer(data["value"], 0, 10**12)
        kind = data.get("kind", "normal")
        if kind not in {"normal", "rollover", "missing"}:
            reject("input", "unknown reading kind")
        note = text(data.get("note", ""), 1000, True)
        if at <= segment["start"] or (
            segment["end"] is not None and at > segment["end"]
        ):
            reject("time", "reading outside installed segment")
        if segment["modulus"] is not None and value >= segment["modulus"]:
            reject("input", "reading exceeds modulus")
        if kind == "rollover" and segment["modulus"] is None:
            reject("input", "rollover requires configured modulus")
        if kind == "missing" and value != 0:
            reject("input", "missing reading uses zero placeholder with explicit kind")
        return {
            "id": ident,
            "segment": segment["id"],
            "at": at,
            "value": value,
            "kind": kind,
            "note": note,
        }

    def add(self, data):
        normalized = self.normalize(data)
        checksum = fingerprint(normalized)
        with self.store.atomic() as con:
            old = self.store.one(
                "SELECT id FROM readings WHERE id=?", (normalized["id"],)
            )
            if old:
                first = self.store.one(
                    "SELECT payload,digest FROM reading_history WHERE id=? AND revision=1",
                    (normalized["id"],),
                )
                if first["digest"] != checksum:
                    reject(
                        "conflict", "reading identity differs from original ingestion"
                    )
                return {**self.get(normalized["id"]), "reused": True}
            occupied = self.store.one(
                "SELECT id FROM readings WHERE segment=? AND at=?",
                (normalized["segment"], normalized["at"]),
            )
            if occupied:
                reject(
                    "conflict",
                    "another reading occupies this segment instant",
                    existing=occupied["id"],
                )
            con.execute(
                "INSERT INTO readings VALUES(?,?,?,?,?,1,?,?)",
                (
                    normalized["id"],
                    normalized["segment"],
                    normalized["at"],
                    normalized["value"],
                    normalized["kind"],
                    canonical(normalized),
                    checksum,
                ),
            )
            con.execute(
                "INSERT INTO reading_history VALUES(?,1,?,?,?)",
                (
                    normalized["id"],
                    canonical(normalized),
                    checksum,
                    "initial ingestion",
                ),
            )
            self.store.event("reading_added", normalized["id"], {"digest": checksum})
        return {**self.get(normalized["id"]), "reused": False}

    def get(self, ident, revision=None):
        if revision is None:
            row, payload = self.store.payload("readings", ident)
            return {**payload, "revision": row["revision"], "digest": row["digest"]}
        integer(revision, 1)
        row = self.store.one(
            "SELECT * FROM reading_history WHERE id=? AND revision=?", (ident, revision)
        )
        if row is None:
            reject("missing", "reading revision not found")
        payload = parse_json(row["payload"])
        if fingerprint(payload) != row["digest"]:
            reject("integrity", "reading history differs")
        return {
            **payload,
            "revision": revision,
            "digest": row["digest"],
            "reason": row["reason"],
        }

    def correct(self, ident, expected_revision, value, kind, reason, note=""):
        integer(expected_revision, 1)
        reason = text(reason)
        with self.store.atomic() as con:
            current = self.get(ident)
            if current["revision"] != expected_revision:
                reject("conflict", "reading was already corrected")
            proposal = {
                name: current[name]
                for name in ["id", "segment", "at", "value", "kind", "note"]
            }
            proposal.update(value=value, kind=kind, note=note)
            proposal = self.normalize(proposal)
            if fingerprint(proposal) == current["digest"]:
                reject("input", "correction must change the reading")
            revision = expected_revision + 1
            checksum = fingerprint(proposal)
            con.execute(
                "UPDATE readings SET value=?,kind=?,revision=?,payload=?,digest=? WHERE id=?",
                (
                    proposal["value"],
                    proposal["kind"],
                    revision,
                    canonical(proposal),
                    checksum,
                    ident,
                ),
            )
            con.execute(
                "INSERT INTO reading_history VALUES(?,?,?,?,?)",
                (ident, revision, canonical(proposal), checksum, reason),
            )
            self.store.event(
                "reading_corrected",
                ident,
                {
                    "revision": revision,
                    "previous": current["digest"],
                    "digest": checksum,
                    "reason": reason,
                },
            )
        return self.get(ident)

    def history(self, ident):
        self.get(ident)
        return [
            self.get(ident, row["revision"])
            for row in self.store.rows(
                "SELECT revision FROM reading_history WHERE id=? ORDER BY revision",
                (ident,),
            )
        ]

    def segment(self, segment):
        self.catalog.segment(segment)
        return [
            self.get(row["id"])
            for row in self.store.rows(
                "SELECT id FROM readings WHERE segment=? ORDER BY at,id", (segment,)
            )
        ]

    def import_batch(self, batch, rows):
        key(batch)
        items(rows, 1, 100000)
        checksum = fingerprint(rows)
        with self.store.atomic() as con:
            old = self.store.one("SELECT * FROM imports WHERE id=?", (batch,))
            if old:
                if old["digest"] != checksum:
                    reject("conflict", "import batch identity differs")
                return {**parse_json(old["result"]), "reused": True}
            seen = set()
            result = []
            for row in rows:
                if not isinstance(row, dict) or "id" not in row:
                    reject("input", "reading object with id required")
                if row["id"] in seen:
                    reject("duplicate", "duplicate reading in batch")
                seen.add(row["id"])
                result.append(self.add(row))
            payload = {
                "batch": batch,
                "inserted": sum(not row["reused"] for row in result),
                "existing": sum(row["reused"] for row in result),
                "ids": [row["id"] for row in result],
            }
            con.execute(
                "INSERT INTO imports VALUES(?,?,?)",
                (batch, checksum, canonical(payload)),
            )
            self.store.event("readings_imported", batch, payload)
        return {**payload, "reused": False}

    def parse(self, content, format_name="jsonl"):
        if not isinstance(content, str) or len(content.encode()) > 16_000_000:
            reject("input", "reading import must be bounded text")
        if format_name == "jsonl":
            rows = []
            for line, value in enumerate(content.splitlines(), 1):
                if not value.strip():
                    continue
                from ..values import EnergyError

                try:
                    rows.append(parse_json(value))
                except EnergyError as exc:
                    reject(
                        "input", "invalid JSONL row", line=line, error=exc.document()
                    )
        elif format_name == "csv":
            import csv
            import io

            reader = csv.DictReader(io.StringIO(content), strict=True)
            headers = reader.fieldnames
            if not headers or len(set(headers)) != len(headers):
                reject("input", "duplicate or empty CSV headers")
            rows = []
            try:
                for raw in reader:
                    if None in raw or any(value is None for value in raw.values()):
                        reject("input", "CSV row width mismatch")
                    if "value" not in raw or not raw["value"].isdigit():
                        reject("input", "CSV counter value must be unsigned integer")
                    raw["value"] = int(raw["value"])
                    if not raw.get("kind"):
                        raw.pop("kind", None)
                    rows.append(raw)
            except csv.Error as exc:
                reject("input", "invalid CSV", reason=str(exc))
        else:
            reject("input", "format must be jsonl or csv")
        items(rows, 1, 100000)
        return rows

    def preview(self, rows):
        from ..values import EnergyError

        items(rows, 1, 100000)
        normalized = []
        failures = []
        seen = set()
        occupied = set()
        for index, row in enumerate(rows):
            try:
                value = self.normalize(row)
                slot = (value["segment"], value["at"])
                if value["id"] in seen or slot in occupied:
                    reject("duplicate", "batch contains duplicate identity or instant")
                seen.add(value["id"])
                occupied.add(slot)
                normalized.append(value)
            except EnergyError as exc:
                failures.append({"row": index, "error": exc.document()})
        return {
            "valid": not failures,
            "rows": normalized,
            "errors": failures,
            "digest": fingerprint(rows),
        }
