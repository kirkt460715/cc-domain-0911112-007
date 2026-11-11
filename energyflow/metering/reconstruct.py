from fractions import Fraction
from ..values import period, instant, rational, fingerprint, reject, fraction, integer
from ..calendar import subtract, coverage_summary


class Reconstructor:
    def __init__(self, store, catalog, readings):
        self.store = store
        self.catalog = catalog
        self.readings = readings

    def segment_intervals(self, segment):
        config = self.catalog.segment(segment)
        previous = {
            "id": "baseline:" + segment,
            "at": config["start"],
            "value": config["baseline"],
            "kind": "normal",
            "revision": 0,
            "digest": fingerprint(config),
        }
        result = []
        for current in self.readings.segment(segment):
            duration = current["at"] - previous["at"]
            evidence = {
                "left": {
                    "id": previous["id"],
                    "revision": previous["revision"],
                    "digest": previous["digest"],
                },
                "right": {
                    "id": current["id"],
                    "revision": current["revision"],
                    "digest": current["digest"],
                },
                "segment_digest": fingerprint(config),
            }
            flags = []
            quantity = None
            ticks = None
            if current["kind"] == "missing" or previous["kind"] == "missing":
                flags.append("missing_reading")
            else:
                ticks = current["value"] - previous["value"]
                if current["kind"] == "rollover":
                    if config["modulus"] is None:
                        flags.append("unconfigured_rollover")
                    else:
                        ticks += config["modulus"]
                        if ticks < 0 or ticks >= config["modulus"]:
                            flags.append("invalid_rollover")
                elif ticks < 0:
                    flags.append("counter_decrease")
                if duration > config["max_gap"]:
                    flags.append("gap_too_long")
                if ticks is not None and ticks >= 0:
                    quantity = ticks * config["microkwh_per_tick"]
                    power = Fraction(quantity * 3600, duration)
                    if power > config["max_microkw"]:
                        flags.append("power_exceeded")
            valid = not flags
            result.append(
                {
                    "id": fingerprint({"segment": segment, **evidence})[:24],
                    "meter": config["meter"],
                    "segment": segment,
                    "start": previous["at"],
                    "end": current["at"],
                    "seconds": duration,
                    "ticks": ticks,
                    "microkwh": rational(quantity) if valid else None,
                    "observed_microkwh": quantity,
                    "flags": flags,
                    "valid": valid,
                    "evidence": evidence,
                }
            )
            previous = current
        return result

    def reconstruct(self, meter, start, end, strict=False):
        start, end = period(start, end)
        self.catalog.get(meter)
        intervals = []
        for config in self.catalog.segments(meter):
            if config["start"] >= end or (
                config["end"] is not None and config["end"] <= start
            ):
                continue
            for interval in self.segment_intervals(config["id"]):
                left = max(start, interval["start"])
                right = min(end, interval["end"])
                if left >= right:
                    continue
                row = dict(interval)
                row["source_start"] = interval["start"]
                row["source_end"] = interval["end"]
                row["start"] = left
                row["end"] = right
                row["seconds"] = right - left
                if row["valid"]:
                    quantity = fraction(interval["microkwh"]) * Fraction(
                        right - left, interval["seconds"]
                    )
                    row["microkwh"] = rational(quantity)
                intervals.append(row)
        intervals.sort(key=lambda row: (row["start"], row["end"], row["segment"]))
        gaps = subtract(start, end, [(row["start"], row["end"]) for row in intervals])
        for left, right in gaps:
            intervals.append(
                {
                    "id": "gap:" + str(left) + ":" + str(right),
                    "meter": meter,
                    "segment": None,
                    "start": left,
                    "end": right,
                    "seconds": right - left,
                    "microkwh": None,
                    "observed_microkwh": None,
                    "flags": ["no_reading_coverage"],
                    "valid": False,
                    "evidence": {},
                }
            )
        intervals.sort(key=lambda row: (row["start"], row["end"], row["id"]))
        valid = [row for row in intervals if row["valid"]]
        total = sum((fraction(row["microkwh"]) for row in valid), Fraction(0))
        coverage = coverage_summary(
            start, end, [(row["start"], row["end"]) for row in valid]
        )
        report = {
            "meter": meter,
            "start": start,
            "end": end,
            "intervals": intervals,
            "microkwh": rational(total),
            "complete": coverage["complete"],
            "coverage": coverage,
            "quality": {
                "valid_intervals": len(valid),
                "invalid_intervals": len(intervals) - len(valid),
            },
        }
        report["digest"] = fingerprint(report)
        if strict and not report["complete"]:
            reject(
                "coverage",
                "cannot account for incomplete meter interval",
                report=report,
            )
        return report

    def revision_impact(self, reading_id, expected_revision):
        current = self.readings.get(reading_id)
        integer(expected_revision, 1)
        if current["revision"] != expected_revision:
            reject("conflict", "reading revision changed")
        segment = self.catalog.segment(current["segment"])
        ordered = self.readings.segment(current["segment"])
        index = next(i for i, row in enumerate(ordered) if row["id"] == reading_id)
        left = ordered[index - 1]["at"] if index else segment["start"]
        right = ordered[index + 1]["at"] if index + 1 < len(ordered) else current["at"]
        affected = []
        for row in self.store.rows(
            "SELECT id,start,end,state FROM closures WHERE start<? AND end>? ORDER BY start,id",
            (right, left),
        ):
            _, payload = self.store.payload("closures", row["id"])
            if segment["meter"] in payload["meters"]:
                affected.append(
                    {
                        "id": row["id"],
                        "state": row["state"],
                        "start": row["start"],
                        "end": row["end"],
                    }
                )
        return {
            "reading": reading_id,
            "revision": expected_revision,
            "meter": segment["meter"],
            "start": left,
            "end": right,
            "closures": affected,
            "affected_interval_ids": [
                row["id"]
                for row in self.segment_intervals(current["segment"])
                if row["start"] < right and row["end"] > left
            ],
        }

    def compare_revision(self, reading_id, old_revision):
        old = self.readings.get(reading_id, old_revision)
        current = self.readings.get(reading_id)
        segment = self.catalog.segment(current["segment"])
        factor = segment["microkwh_per_tick"]
        change = (current["value"] - old["value"]) * factor
        return {
            "reading": reading_id,
            "before": old,
            "after": current,
            "counter_difference_microkwh": change,
            "note": "Adjacent interval deltas have opposite signs; totals may remain unchanged.",
        }

    def fleet(self, meters, start, end, strict=False):
        from ..values import items

        items(meters, 1, 1000)
        if len(meters) != len(set(meters)):
            reject("input", "duplicate meters in fleet")
        reports = [
            self.reconstruct(meter, start, end, strict) for meter in sorted(meters)
        ]
        groups = {}
        for report in reports:
            meter = self.catalog.get(report["meter"])
            group = (meter["medium"], meter["direction"])
            groups[group] = groups.get(group, Fraction(0)) + fraction(
                report["microkwh"]
            )
        return {
            "reports": reports,
            "complete": all(row["complete"] for row in reports),
            "totals": [
                {"medium": medium, "direction": direction, "microkwh": rational(value)}
                for (medium, direction), value in sorted(groups.items())
            ],
        }

    def gaps(self, meter, start, end):
        report = self.reconstruct(meter, start, end)
        result = []
        for interval in report["intervals"]:
            if interval["valid"]:
                continue
            result.append(
                {
                    "start": interval["start"],
                    "end": interval["end"],
                    "flags": interval["flags"],
                    "evidence": interval["evidence"],
                }
            )
        return {
            "meter": meter,
            "gaps": result,
            "missing_seconds": report["coverage"]["missing_seconds"],
        }

    def power_profile(self, meter, start, end):
        report = self.reconstruct(meter, start, end)
        rows = []
        for interval in report["intervals"]:
            if interval["valid"]:
                power = fraction(interval["microkwh"]) * Fraction(
                    3600, interval["seconds"]
                )
                rows.append(
                    {
                        "start": interval["start"],
                        "end": interval["end"],
                        "microkw": rational(power),
                        "evidence": interval["evidence"],
                    }
                )
        peak = max((fraction(row["microkw"]) for row in rows), default=Fraction(0))
        return {
            "meter": meter,
            "rows": rows,
            "peak_microkw": rational(peak),
            "complete": report["complete"],
            "basis": "uniform_between_counter_readings",
        }
