from datetime import datetime, timezone, timedelta
from fractions import Fraction
from .values import period, instant, integer, reject, fraction, rational, distribute


def boundaries(start, end, width=3600, origin=0):
    start, end = period(start, end)
    integer(width, 1, 366 * 86400)
    origin = instant(origin)
    first = origin + ((start - origin) // width + 1) * width
    result = [start]
    while first < end:
        result.append(first)
        first += width
        if len(result) > 100001:
            reject("capacity", "too many time boundaries")
    result.append(end)
    return result


def split(start, end, cuts):
    start, end = period(start, end)
    values = {start, end}
    for value in cuts:
        value = instant(value)
        if start < value < end:
            values.add(value)
    ordered = sorted(values)
    return list(zip(ordered, ordered[1:]))


def intersect(left, right):
    a, b = period(*left)
    c, d = period(*right)
    start = max(a, c)
    end = min(b, d)
    return (start, end) if end > start else None


def subtract(start, end, covered):
    start, end = period(start, end)
    normalized = []
    for left, right in covered:
        part = intersect((start, end), (left, right))
        if part:
            normalized.append(part)
    normalized.sort()
    cursor = start
    result = []
    for left, right in normalized:
        if left > cursor:
            result.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        result.append((cursor, end))
    return result


def utc_days(start, end):
    start, end = period(start, end)
    begin = datetime.fromtimestamp(start, timezone.utc)
    day = begin.replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    while int(day.timestamp()) < end:
        following = day + timedelta(days=1)
        left = max(start, int(day.timestamp()))
        right = min(end, int(following.timestamp()))
        result.append(
            {
                "date": day.date().isoformat(),
                "start": left,
                "end": right,
                "seconds": right - left,
            }
        )
        day = following
    return result


def shift_intervals(start, end, shifts, offset_minutes=0):
    start, end = period(start, end)
    integer(offset_minutes, -840, 840)
    if not isinstance(shifts, list) or not shifts:
        reject("calendar", "at least one shift definition required")
    normalized = []
    names = set()
    from .values import key, object_fields

    for shift in shifts:
        object_fields(
            shift,
            {"id", "start_minute", "end_minute"},
            {"id", "start_minute", "end_minute"},
        )
        name = key(shift["id"])
        if name in names:
            reject("calendar", "duplicate shift name")
        names.add(name)
        left = integer(shift["start_minute"], 0, 1439)
        right = integer(shift["end_minute"], 1, 2880)
        if right <= left or right - left > 1440:
            reject("calendar", "shift must span at most one day")
        normalized.append((name, left, right))
    local_start = start + offset_minutes * 60
    midnight = local_start - local_start % 86400 - 86400
    result = []
    while midnight - offset_minutes * 60 < end:
        for name, left, right in normalized:
            shift_start = midnight + left * 60 - offset_minutes * 60
            shift_end = midnight + right * 60 - offset_minutes * 60
            a = max(start, shift_start)
            b = min(end, shift_end)
            if a < b:
                label = (
                    datetime.fromtimestamp(midnight, timezone.utc).date().isoformat()
                )
                result.append({"id": name, "date": label, "start": a, "end": b})
        midnight += 86400
    result.sort(key=lambda row: (row["start"], row["end"], row["id"]))
    for previous, current in zip(result, result[1:]):
        if previous["end"] > current["start"]:
            reject("calendar", "shift definitions overlap")
    return result


def slice_quantity(quantity, start, end, cuts):
    quantity = fraction(quantity)
    if quantity < 0:
        reject("input", "quantity cannot be negative")
    start, end = period(start, end)
    result = []
    for left, right in split(start, end, cuts):
        part = quantity * Fraction(right - left, end - start)
        result.append({"start": left, "end": right, "quantity": rational(part)})
    if sum(fraction(row["quantity"]) for row in result) != quantity:
        reject("integrity", "time slicing lost quantity")
    return result


def integer_slices(quantity, start, end, cuts):
    integer(quantity)
    pieces = split(start, end, cuts)
    names = {str(index): right - left for index, (left, right) in enumerate(pieces)}
    allocated = distribute(quantity, names)
    return [
        {"start": left, "end": right, "quantity": allocated[str(index)]}
        for index, (left, right) in enumerate(pieces)
    ]


def coverage_summary(start, end, intervals):
    start, end = period(start, end)
    normalized = []
    overlap_seconds = 0
    cursor = start
    for left, right in sorted(intervals):
        part = intersect((start, end), (left, right))
        if part is None:
            continue
        a, b = part
        if a < cursor:
            overlap_seconds += max(0, min(cursor, b) - a)
        cursor = max(cursor, b)
        normalized.append((a, b))
    gaps = subtract(start, end, normalized)
    missing = sum(right - left for left, right in gaps)
    return {
        "seconds": end - start,
        "covered_seconds": end - start - missing,
        "missing_seconds": missing,
        "overlap_seconds": overlap_seconds,
        "gaps": [{"start": left, "end": right} for left, right in gaps],
        "complete": missing == 0 and overlap_seconds == 0,
    }
