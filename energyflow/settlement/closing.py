from fractions import Fraction
from ..values import (
    key,
    period,
    items,
    canonical,
    fingerprint,
    parse_json,
    integer,
    text,
    reject,
    rational,
    fraction,
)


class Closing:
    def __init__(self, store, catalog, allocator, tariffs, factors):
        self.store = store
        self.catalog = catalog
        self.allocator = allocator
        self.tariffs = tariffs
        self.factors = factors

    def calculate(
        self, meters, start, end, require_assigned=True, require_factors=False
    ):
        start, end = period(start, end)
        items(meters, 1, 1000)
        if len(meters) != len(set(meters)):
            reject("duplicate", "duplicate closure meter")
        reports = []
        for meter in sorted(meters):
            allocation = (
                self.allocator.require_assigned(meter, start, end)
                if require_assigned
                else self.allocator.allocate(meter, start, end)
            )
            cost = self.tariffs.integrate(allocation)
            emissions = self.factors.integrate(allocation, require_factors)
            reports.append(
                {
                    "meter": meter,
                    "definition": self.catalog.get(meter),
                    "allocation": allocation,
                    "cost": cost,
                    "emissions": emissions,
                }
            )
        energy = {}
        costs = {}
        carbon = {}
        for report in reports:
            definition = report["definition"]
            for row in report["allocation"]["products"]:
                identity = (
                    definition["medium"],
                    definition["direction"],
                    row["product"],
                )
                energy[identity] = energy.get(identity, 0) + row["microkwh"]
            for row in report["cost"]["products"]:
                identity = (row["currency"], definition["direction"], row["product"])
                costs[identity] = costs.get(identity, 0) + row["minor"]
            for row in report["emissions"]["products"]:
                identity = (
                    definition["medium"],
                    definition["direction"],
                    row["product"],
                )
                carbon[identity] = carbon.get(identity, 0) + row["milligrams"]
        return {
            "start": start,
            "end": end,
            "meters": sorted(meters),
            "reports": reports,
            "require_assigned": bool(require_assigned),
            "require_factors": bool(require_factors),
            "energy": [
                {"medium": m, "direction": d, "product": p, "microkwh": v}
                for (m, d, p), v in sorted(energy.items())
            ],
            "costs": [
                {"currency": c, "direction": d, "product": p, "minor": v}
                for (c, d, p), v in sorted(costs.items())
            ],
            "emissions": [
                {"medium": m, "direction": d, "product": p, "milligrams": v}
                for (m, d, p), v in sorted(carbon.items())
            ],
            "format_version": 1,
        }

    def preview(self, meters, start, end, require_assigned=True, require_factors=False):
        with self.store.atomic():
            payload = self.calculate(
                meters, start, end, require_assigned, require_factors
            )
        return {"payload": payload, "digest": fingerprint(payload)}

    def freeze(
        self,
        closure,
        meters,
        start,
        end,
        expected_digest,
        operator,
        require_assigned=True,
        require_factors=False,
    ):
        key(closure)
        key(operator)
        start, end = period(start, end)
        with self.store.atomic() as con:
            existing = self.store.one("SELECT id FROM closures WHERE id=?", (closure,))
            if existing:
                current = self.get(closure)
                if current["digest"] != expected_digest:
                    reject("conflict", "closure identity differs")
                return current
            for row in self.store.rows(
                "SELECT id FROM closures WHERE start<? AND end>?", (end, start)
            ):
                previous = self.get(row["id"])
                if set(previous["payload"]["meters"]) & set(meters):
                    reject(
                        "overlap",
                        "meter already belongs to overlapping closure",
                        closure=row["id"],
                    )
            preview = self.preview(
                meters, start, end, require_assigned, require_factors
            )
            if preview["digest"] != expected_digest:
                reject("conflict", "accounting inputs changed after preview")
            con.execute(
                "INSERT INTO closures VALUES(?,?,?,'frozen',1,?,?)",
                (closure, start, end, canonical(preview["payload"]), preview["digest"]),
            )
            self.store.event(
                "period_frozen",
                closure,
                {"digest": preview["digest"], "operator": operator},
            )
        return self.get(closure)

    def get(self, closure):
        row, payload = self.store.payload("closures", closure)
        return {
            "id": closure,
            "state": row["state"],
            "revision": row["revision"],
            "payload": payload,
            "digest": row["digest"],
        }

    def verify(self, closure):
        record = self.get(closure)
        payload = record["payload"]
        for report in payload["reports"]:
            allocation = report["allocation"]
            if (
                sum(row["microkwh"] for row in allocation["products"])
                != allocation["microkwh"]
            ):
                reject("integrity", "frozen energy does not conserve total")
            for total in report["cost"]["totals"]:
                actual = sum(
                    row["minor"]
                    for row in report["cost"]["products"]
                    if row["currency"] == total["currency"]
                )
                if actual != total["minor"]:
                    reject("integrity", "frozen cost does not conserve currency total")
        return {"id": closure, "verified": True, "digest": record["digest"]}

    def differences(self, before, after):
        results = {}
        definitions = [
            ("energy", ("medium", "direction", "product"), "microkwh"),
            ("costs", ("currency", "direction", "product"), "minor"),
            ("emissions", ("medium", "direction", "product"), "milligrams"),
        ]
        for name, keys, value_key in definitions:
            old = {
                tuple(row[key] for key in keys): row[value_key] for row in before[name]
            }
            new = {
                tuple(row[key] for key in keys): row[value_key] for row in after[name]
            }
            results[name] = []
            for identity in sorted(old.keys() | new.keys()):
                delta = new.get(identity, 0) - old.get(identity, 0)
                if delta:
                    results[name].append(
                        {
                            **dict(zip(keys, identity)),
                            "before": old.get(identity, 0),
                            "after": new.get(identity, 0),
                            "delta": delta,
                            "unit": value_key,
                        }
                    )
        return results

    def current(self, closure):
        frozen = self.get(closure)["payload"]
        return self.preview(
            frozen["meters"],
            frozen["start"],
            frozen["end"],
            frozen["require_assigned"],
            frozen["require_factors"],
        )

    def effective(self, closure):
        frozen = self.get(closure)
        rows = self.store.rows(
            "SELECT id FROM corrections WHERE closure=? AND approved=1 ORDER BY rowid",
            (closure,),
        )
        payload = frozen["payload"]
        checksum = frozen["digest"]
        chain = []
        for row in rows:
            correction = self.correction(row["id"])
            if correction["payload"]["from_digest"] != checksum:
                reject("integrity", "correction chain is discontinuous")
            payload = correction["payload"]["after"]
            checksum = fingerprint(payload)
            chain.append(row["id"])
        return {"payload": payload, "digest": checksum, "corrections": chain}

    def propose_correction(self, correction, closure, reason):
        key(correction)
        reason = text(reason)
        with self.store.atomic() as con:
            if self.store.one("SELECT id FROM corrections WHERE id=?", (correction,)):
                old = self.correction(correction)
                if old["closure"] != closure or old["payload"]["reason"] != reason:
                    reject("conflict", "correction identity differs")
                return old
            before = self.effective(closure)
            after = self.current(closure)
            if after["digest"] == before["digest"]:
                reject("state", "accounting inputs have not changed")
            payload = {
                "closure": closure,
                "from_digest": before["digest"],
                "after": after["payload"],
                "after_digest": after["digest"],
                "reason": reason,
                "differences": self.differences(before["payload"], after["payload"]),
            }
            con.execute(
                "INSERT INTO corrections VALUES(?,?,?,?,0,NULL)",
                (correction, closure, canonical(payload), fingerprint(payload)),
            )
            self.store.event(
                "correction_proposed",
                correction,
                {"closure": closure, "digest": fingerprint(payload)},
            )
        return self.correction(correction)

    def correction(self, correction):
        row, payload = self.store.payload("corrections", correction)
        return {
            "id": correction,
            "closure": row["closure"],
            "approved": bool(row["approved"]),
            "operator": row["operator"],
            "payload": payload,
            "digest": row["digest"],
        }

    def approve_correction(self, correction, expected_digest, operator):
        key(operator)
        with self.store.atomic() as con:
            row = self.correction(correction)
            if row["digest"] != expected_digest:
                reject("conflict", "correction review fingerprint differs")
            if row["approved"]:
                return row
            effective = self.effective(row["closure"])
            if effective["digest"] != row["payload"]["from_digest"]:
                reject("conflict", "another correction was approved first")
            current = self.current(row["closure"])
            if current["digest"] != row["payload"]["after_digest"]:
                reject("conflict", "inputs changed after correction review")
            con.execute(
                "UPDATE corrections SET approved=1,operator=? WHERE id=?",
                (operator, correction),
            )
            con.execute(
                "UPDATE closures SET revision=revision+1 WHERE id=?", (row["closure"],)
            )
            self.store.event(
                "correction_approved",
                correction,
                {"operator": operator, "closure": row["closure"]},
            )
        return self.correction(correction)

    def drift(self, closure):
        effective = self.effective(closure)
        current = self.current(closure)
        return {
            "closure": closure,
            "changed": effective["digest"] != current["digest"],
            "effective_digest": effective["digest"],
            "current_digest": current["digest"],
            "differences": self.differences(effective["payload"], current["payload"]),
        }

    def list(self, meter=None):
        result = []
        for row in self.store.rows(
            "SELECT id,start,end,revision FROM closures ORDER BY start,id"
        ):
            closure = self.get(row["id"])
            if meter is None or meter in closure["payload"]["meters"]:
                result.append(
                    {
                        **row,
                        "meters": closure["payload"]["meters"],
                        "digest": closure["digest"],
                    }
                )
        return result
