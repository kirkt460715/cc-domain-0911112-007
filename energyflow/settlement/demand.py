from fractions import Fraction
from ..values import period, integer, fraction, rational, round_even, fixed, reject
from ..calendar import boundaries


class Demand:
    def __init__(self, reconstructor):
        self.reconstructor = reconstructor

    @staticmethod
    def integrate_rows(rows, start, end):
        value = Fraction(0)
        covered = 0
        evidence = []
        for row in rows:
            left = max(start, row["start"])
            right = min(end, row["end"])
            if left >= right or not row["valid"]:
                continue
            value += fraction(row["microkwh"]) * Fraction(right - left, row["seconds"])
            covered += right - left
            evidence.append(row["id"])
        return value, covered, evidence

    def windows(self, meter, start, end, width=900, origin=0, include_partial=False):
        start, end = period(start, end)
        integer(width, 1, 86400)
        reconstruction = self.reconstructor.reconstruct(meter, start, end)
        points = boundaries(start, end, width, origin)
        result = []
        for left, right in zip(points, points[1:]):
            partial = right - left != width
            if partial and not include_partial:
                continue
            energy, covered, evidence = self.integrate_rows(
                reconstruction["intervals"], left, right
            )
            complete = covered == right - left
            demand = energy * Fraction(3600, right - left) if complete else None
            result.append(
                {
                    "start": left,
                    "end": right,
                    "seconds": right - left,
                    "microkwh": rational(energy),
                    "microkw": rational(demand) if demand is not None else None,
                    "complete": complete,
                    "partial": partial,
                    "evidence": evidence,
                }
            )
        eligible = [row for row in result if row["complete"] and not row["partial"]]
        peak = max(
            eligible,
            key=lambda row: (fraction(row["microkw"]), -row["start"]),
            default=None,
        )
        return {
            "meter": meter,
            "start": start,
            "end": end,
            "width": width,
            "origin": origin,
            "windows": result,
            "peak": peak,
            "complete": reconstruction["complete"],
            "basis": "fixed_utc_windows_uniform_counter_rate",
        }

    def rolling_peak(self, meter, start, end, width=900):
        start, end = period(start, end)
        integer(width, 1, 86400)
        if end - start < width:
            reject("time", "rolling demand interval shorter than window")
        report = self.reconstructor.reconstruct(meter, start, end)
        # For piecewise constant power, extrema occur at a reading boundary or one window before it.
        starts = {start, end - width}
        for row in report["intervals"]:
            for point in [row["start"], row["end"]]:
                if start <= point <= end - width:
                    starts.add(point)
                if start <= point - width <= end - width:
                    starts.add(point - width)
        candidates = []
        missing = 0
        for left in sorted(starts):
            energy, coverage, evidence = self.integrate_rows(
                report["intervals"], left, left + width
            )
            if coverage != width:
                missing += 1
                continue
            power = energy * Fraction(3600, width)
            candidates.append(
                {
                    "start": left,
                    "end": left + width,
                    "microkw": rational(power),
                    "evidence": evidence,
                }
            )
        peak = max(
            candidates,
            key=lambda row: (fraction(row["microkw"]), -row["start"]),
            default=None,
        )
        return {
            "meter": meter,
            "width": width,
            "peak": peak,
            "candidates": len(candidates),
            "incomplete_candidates": missing,
            "complete": report["complete"],
        }

    def charge(
        self, windows, price_per_kw, contracted_kw="0", charge_excess_only=False
    ):
        price = fixed(price_per_kw)
        contracted = fixed(contracted_kw)
        peak = windows.get("peak")
        if not windows["complete"] or peak is None:
            reject("coverage", "demand charge requires complete eligible windows")
        demand = fraction(peak["microkw"])
        billable = (
            max(Fraction(0), demand - contracted)
            if charge_excess_only
            else max(demand, Fraction(contracted))
        )
        exact_minor = billable * Fraction(price, 10**10)
        return {
            "peak_microkw": rational(demand),
            "contracted_microkw": contracted,
            "billable_microkw": rational(billable),
            "exact_minor": rational(exact_minor),
            "minor": round_even(exact_minor),
            "excess_only": bool(charge_excess_only),
            "peak_start": peak["start"],
            "peak_end": peak["end"],
        }

    def exceedances(self, windows, threshold_kw):
        threshold = fixed(threshold_kw)
        events = []
        current = None
        for row in windows["windows"]:
            exceeds = row["complete"] and fraction(row["microkw"]) > threshold
            if not exceeds:
                if current:
                    events.append(current)
                    current = None
                continue
            if current and current["end"] == row["start"]:
                current["end"] = row["end"]
                current["windows"] += 1
                current["peak_microkw"] = rational(
                    max(fraction(current["peak_microkw"]), fraction(row["microkw"]))
                )
            else:
                if current:
                    events.append(current)
                current = {
                    "start": row["start"],
                    "end": row["end"],
                    "windows": 1,
                    "peak_microkw": row["microkw"],
                }
        if current:
            events.append(current)
        return {
            "meter": windows["meter"],
            "threshold_microkw": threshold,
            "events": events,
            "seconds": sum(row["end"] - row["start"] for row in events),
        }

    def load_factor(self, windows):
        usable = [
            row for row in windows["windows"] if row["complete"] and not row["partial"]
        ]
        if not usable:
            return {"status": "no_complete_windows", "load_factor": None}
        energy = sum(fraction(row["microkwh"]) for row in usable)
        seconds = sum(row["seconds"] for row in usable)
        average = energy * Fraction(3600, seconds)
        peak = max(fraction(row["microkw"]) for row in usable)
        return {
            "status": "ok",
            "average_microkw": rational(average),
            "peak_microkw": rational(peak),
            "load_factor": rational(average / peak) if peak else None,
            "covered_seconds": seconds,
            "complete_period": windows["complete"],
        }
