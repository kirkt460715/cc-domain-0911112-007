import json
import re
import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction


class EnergyError(Exception):
    def __init__(self, code, message, **context):
        super().__init__(message)
        self.code = code
        self.context = context

    def document(self):
        return {"code": self.code, "message": str(self), "context": self.context}


def reject(code, message, **context):
    raise EnergyError(code, message, **context)


def key(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}", value
    ):
        reject("input", "invalid identifier")
    return value


def integer(value, low=0, high=10**15):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        reject("input", "integer outside permitted range", minimum=low, maximum=high)
    return value


def text(value, maximum=2000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        reject("input", "invalid text")
    if not empty and not value.strip():
        reject("input", "text is required")
    return value.strip()


def object_fields(value, allowed, required=()):
    if not isinstance(value, dict):
        reject("input", "object required")
    missing = set(required) - value.keys()
    extra = value.keys() - set(allowed)
    if missing or extra:
        reject(
            "input",
            "object fields differ",
            missing=sorted(missing),
            extra=sorted(extra),
        )
    return value


def items(value, low=0, high=10000):
    if not isinstance(value, list) or not low <= len(value) <= high:
        reject("input", "list outside permitted size")
    return value


def instant(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return integer(value, 0, 4133980800)
    if not isinstance(value, str):
        reject("time", "UTC timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        reject("time", "invalid ISO timestamp")
    if parsed.tzinfo is None or parsed.microsecond:
        reject("time", "timestamp requires offset and whole seconds")
    number = int(parsed.timestamp())
    return integer(number, 0, 4133980800)


def timestamp(value):
    return (
        datetime.fromtimestamp(instant(value), timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def period(start, end, maximum=366 * 86400):
    start = instant(start)
    end = instant(end)
    if end <= start or end - start > maximum:
        reject("time", "invalid half-open interval")
    return start, end


def fixed(value, places=6, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        reject("input", "exact decimal text required")
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        reject("input", "invalid decimal")
    if not decimal.is_finite() or abs(decimal) > Decimal("1e12"):
        reject("input", "decimal outside permitted range")
    scaled = decimal * 10**places
    if scaled != scaled.to_integral_value():
        reject("precision", "too many decimal places")
    if not signed and scaled < 0:
        reject("input", "negative value not permitted")
    return int(scaled)


def decimal_text(value, places=6):
    return format(Decimal(value) / 10**places, f".{places}f")


def fraction(value):
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    if isinstance(value, str):
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError):
            reject("input", "invalid exact fraction")
        if abs(result.numerator) > 10**24 or result.denominator > 10**12:
            reject("input", "fraction outside permitted range")
        return result
    reject("input", "exact fraction text required")


def rational(value):
    value = fraction(value)
    return str(value.numerator) + "/" + str(value.denominator)


def canonical(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ValueError, TypeError):
        reject("input", "value cannot be encoded as canonical JSON")


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def parse_json(content):
    def pairs(values):
        result = {}
        for name, value in values:
            if name in result:
                reject("input", "duplicate JSON field", field=name)
            result[name] = value
        return result

    def constant(value):
        reject("input", "nonfinite JSON value", value=value)

    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError) as exc:
        reject("input", "malformed JSON", reason=str(exc))


def round_even(value):
    value = fraction(value)
    sign = -1 if value < 0 else 1
    quotient, remainder = divmod(abs(value.numerator), value.denominator)
    twice = remainder * 2
    if twice > value.denominator or (twice == value.denominator and quotient % 2):
        quotient += 1
    return sign * quotient


def distribute(total, weighted):
    integer(total)
    if not isinstance(weighted, dict) or not weighted:
        reject("allocation", "at least one weighted destination required")
    weights = {key(name): fraction(value) for name, value in weighted.items()}
    if any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
        reject("allocation", "weights must be nonnegative with positive sum")
    denominator = sum(weights.values())
    exact = {name: total * weight / denominator for name, weight in weights.items()}
    result = {
        name: value.numerator // value.denominator for name, value in exact.items()
    }
    remainder = total - sum(result.values())
    order = sorted(weights, key=lambda name: (-(exact[name] - result[name]), name))
    for name in order[:remainder]:
        result[name] += 1
    return {name: result[name] for name in sorted(result)}
