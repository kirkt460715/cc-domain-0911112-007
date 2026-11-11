import argparse
import sys
from pathlib import Path
from .values import parse_json, canonical, EnergyError, reject
from .service import EnergyService


def load(path, limit=20000000):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        reject("path", "input must be a bounded local regular file")
    return path.read_text("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="energyflow")
    parser.add_argument("--db", default="energyflow.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("describe")
    run = commands.add_parser("run")
    run.add_argument("request")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("batch")
    ingest.add_argument("file")
    ingest.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    ingest.add_argument("--preview", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("file")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import run

            result = run()
        elif args.command == "verify":
            from .interfaces.bundles import Bundles

            result = Bundles.verify(args.file)
        else:
            with EnergyService(args.db) as service:
                if args.command == "describe":
                    result = service.describe()
                elif args.command == "ingest":
                    rows = service.readings.parse(load(args.file), args.format)
                    result = (
                        service.readings.preview(rows)
                        if args.preview
                        else service.readings.import_batch(args.batch, rows)
                    )
                else:
                    result = service.dispatch(parse_json(load(args.request)))
        print(canonical({"ok": True, "result": result}))
        return 0
    except EnergyError as exc:
        print(canonical({"ok": False, "error": exc.document()}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(
            canonical({"ok": False, "error": {"code": "io", "message": str(exc)}}),
            file=sys.stderr,
        )
        return 3
