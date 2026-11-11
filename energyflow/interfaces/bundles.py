import os
from pathlib import Path
from ..values import (
    canonical,
    parse_json,
    fingerprint,
    reject,
    object_fields,
    key,
    integer,
)


class Bundles:
    def __init__(self, closing):
        self.closing = closing

    def envelope(self, closure, effective=False):
        self.closing.verify(closure)
        if effective:
            row = self.closing.effective(closure)
            payload = row["payload"]
            chain = row["corrections"]
        else:
            payload = self.closing.get(closure)["payload"]
            chain = []
        envelope = {
            "format": "energyflow-settlement",
            "version": 1,
            "closure": closure,
            "payload": payload,
            "corrections": chain,
        }
        return {**envelope, "digest": fingerprint(envelope)}

    def export(self, closure, path, effective=False):
        envelope = self.envelope(closure, effective)
        target = Path(path)
        if target.exists() or target.is_symlink():
            reject("path", "export target already exists")
        if not target.parent.is_dir():
            reject("path", "export directory does not exist")
        content = canonical(envelope).encode("utf-8")
        import tempfile

        descriptor, name = tempfile.mkstemp(prefix=".energyflow-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                reject("path", "export target appeared concurrently")
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": str(target),
            "bytes": len(content),
            "digest": envelope["digest"],
        }

    @staticmethod
    def verify_content(content):
        if not isinstance(content, str) or len(content.encode()) > 50_000_000:
            reject("input", "settlement envelope exceeds size limit")
        envelope = parse_json(content)
        fields = {"format", "version", "closure", "payload", "corrections", "digest"}
        object_fields(envelope, fields, fields)
        if envelope["format"] != "energyflow-settlement" or envelope["version"] != 1:
            reject("version", "unsupported settlement format")
        expected = envelope.pop("digest")
        if fingerprint(envelope) != expected:
            reject("integrity", "settlement envelope fingerprint mismatch")
        payload = envelope["payload"]
        if payload.get("format_version") != 1:
            reject("version", "unsupported settlement payload")
        if sorted(payload["meters"]) != sorted(
            report["meter"] for report in payload["reports"]
        ):
            reject("integrity", "settlement meter manifest mismatch")
        for report in payload["reports"]:
            allocation = report["allocation"]
            if (
                sum(row["microkwh"] for row in allocation["products"])
                != allocation["microkwh"]
            ):
                reject("integrity", "allocation energy mismatch")
            for total in report["cost"]["totals"]:
                actual = sum(
                    row["minor"]
                    for row in report["cost"]["products"]
                    if row["currency"] == total["currency"]
                )
                if actual != total["minor"]:
                    reject("integrity", "allocation cost mismatch")
        return {
            "closure": envelope["closure"],
            "digest": expected,
            "verified": True,
            "meters": payload["meters"],
            "corrections": envelope["corrections"],
        }

    @staticmethod
    def verify(path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 50_000_000:
            reject("path", "bundle must be a bounded local regular file")
        return Bundles.verify_content(path.read_text("utf-8"))

    def csv(self, closure, effective=False):
        import csv
        import io

        envelope = self.envelope(closure, effective)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            ["kind", "medium_or_currency", "direction", "product", "value", "unit"]
        )
        for name, medium, value in [
            ("energy", "medium", "microkwh"),
            ("costs", "currency", "minor"),
            ("emissions", "medium", "milligrams"),
        ]:
            for row in envelope["payload"][name]:
                writer.writerow(
                    [
                        name,
                        row[medium],
                        row["direction"],
                        row["product"],
                        row[value],
                        value,
                    ]
                )
        return output.getvalue()

    def compare(self, first, second):
        self.verify_content(canonical(first))
        self.verify_content(canonical(second))
        if first["closure"] != second["closure"]:
            reject("input", "bundle comparison requires same closure")
        return {
            "closure": first["closure"],
            "changed": first["digest"] != second["digest"],
            "differences": self.closing.differences(
                first["payload"], second["payload"]
            ),
        }
