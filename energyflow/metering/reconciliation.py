from fractions import Fraction
from ..values import (
    key,
    period,
    fraction,
    rational,
    integer,
    fingerprint,
    reject,
    items,
)
from ..calendar import split


class MeterBalance:
    def __init__(self, catalog, reconstructor):
        self.catalog = catalog
        self.reconstructor = reconstructor

    def compare(self, parent, children, start, end, tolerance_microkwh=0):
        start, end = period(start, end)
        items(children, 1, 1000)
        if len(children) != len(set(children)) or parent in children:
            reject("input", "parent and child meter identities must be distinct")
        tolerance = integer(tolerance_microkwh)
        definition = self.catalog.get(parent)
        for child in children:
            meter = self.catalog.get(child)
            if (meter["medium"], meter["direction"]) != (
                definition["medium"],
                definition["direction"],
            ):
                reject("unit", "balance requires same energy medium and direction")
        reports = {
            meter: self.reconstructor.reconstruct(meter, start, end)
            for meter in [parent] + sorted(children)
        }
        cuts = {start, end}
        for report in reports.values():
            for interval in report["intervals"]:
                cuts.update([interval["start"], interval["end"]])
        rows = []
        known_parent = Fraction(0)
        known_children = Fraction(0)
        complete = True
        for left, right in split(start, end, cuts):
            quantities = {}
            missing = []
            evidence = {}
            for meter, report in reports.items():
                interval = next(
                    (
                        row
                        for row in report["intervals"]
                        if row["start"] <= left and row["end"] >= right
                    ),
                    None,
                )
                if interval is None or not interval["valid"]:
                    missing.append(meter)
                    continue
                value = fraction(interval["microkwh"]) * Fraction(
                    right - left, interval["seconds"]
                )
                quantities[meter] = value
                evidence[meter] = interval["id"]
            if missing:
                complete = False
                rows.append(
                    {
                        "start": left,
                        "end": right,
                        "status": "incomplete",
                        "missing": missing,
                        "evidence": evidence,
                    }
                )
                continue
            parent_value = quantities[parent]
            child_value = sum(quantities[child] for child in children)
            residual = parent_value - child_value
            known_parent += parent_value
            known_children += child_value
            rows.append(
                {
                    "start": left,
                    "end": right,
                    "status": "ok"
                    if residual >= -tolerance
                    else "child_exceeds_parent",
                    "parent_microkwh": rational(parent_value),
                    "children_microkwh": rational(child_value),
                    "residual_microkwh": rational(residual),
                    "evidence": evidence,
                }
            )
        total_residual = known_parent - known_children
        return {
            "parent": parent,
            "children": sorted(children),
            "start": start,
            "end": end,
            "rows": rows,
            "complete": complete,
            "parent_microkwh": rational(known_parent),
            "children_microkwh": rational(known_children),
            "residual_microkwh": rational(total_residual),
            "loss_ratio": rational(total_residual / known_parent)
            if known_parent
            else None,
            "balanced": complete and all(row["status"] == "ok" for row in rows),
            "input_digests": {
                meter: report["digest"] for meter, report in reports.items()
            },
        }

    def tree(self, root, topology, start, end, tolerance_microkwh=0):
        key(root)
        if not isinstance(topology, dict):
            reject("input", "meter topology must be an adjacency object")
        owners = {}
        visited = set()
        active = set()
        order = []

        def visit(meter):
            self.catalog.get(meter)
            if meter in active:
                reject("cycle", "meter containment graph contains a cycle")
            if meter in visited:
                return
            active.add(meter)
            children = topology.get(meter, [])
            items(children, 0, 1000)
            if len(children) != len(set(children)):
                reject("duplicate", "duplicate child meter")
            for child in children:
                if child in owners and owners[child] != meter:
                    reject("topology", "child meter cannot have two parents")
                owners[child] = meter
                visit(child)
            active.remove(meter)
            visited.add(meter)
            order.append(meter)

        visit(root)
        if set(topology) - visited:
            reject("topology", "topology includes unreachable parent nodes")
        reports = []
        for meter in order:
            if topology.get(meter):
                reports.append(
                    self.compare(meter, topology[meter], start, end, tolerance_microkwh)
                )
        return {
            "root": root,
            "meters": sorted(visited),
            "reports": reports,
            "balanced": all(row["balanced"] for row in reports),
            "complete": all(row["complete"] for row in reports),
        }

    def allocate_residual(self, comparison, weights):
        from ..values import distribute, round_even

        if not comparison["complete"]:
            reject("coverage", "cannot allocate residual from incomplete comparison")
        residual = fraction(comparison["residual_microkwh"])
        if residual < 0:
            reject(
                "balance", "negative residual cannot be treated as distribution loss"
            )
        amounts = distribute(round_even(residual), weights)
        return {
            "parent": comparison["parent"],
            "comparison_digest": fingerprint(comparison),
            "basis": "explicit_residual_allocation",
            "exact_residual_microkwh": rational(residual),
            "allocated_microkwh": sum(amounts.values()),
            "products": [
                {"product": name, "microkwh": value} for name, value in amounts.items()
            ],
        }

    def compare_snapshots(self, before, after):
        if (before["parent"], before["children"], before["start"], before["end"]) != (
            after["parent"],
            after["children"],
            after["start"],
            after["end"],
        ):
            reject("input", "balance comparison boundaries differ")
        changed = []
        for meter in sorted(
            before["input_digests"].keys() | after["input_digests"].keys()
        ):
            if before["input_digests"].get(meter) != after["input_digests"].get(meter):
                changed.append(meter)
        return {
            "changed_meters": changed,
            "residual_delta_microkwh": rational(
                fraction(after["residual_microkwh"])
                - fraction(before["residual_microkwh"])
            ),
            "previous_complete": before["complete"],
            "current_complete": after["complete"],
        }
