"""Replay saved claim validation and rendering offline, without a model or database."""
import argparse
import json
from pathlib import Path

from finance_adapters.providers.reproduction import replay_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--source-pdf", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = replay_bundle(json.loads(args.bundle.read_text(encoding="utf-8")), args.source_pdf)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result = {"status": "failed", "error": str(exc), "model_called": False, "database_required": False}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
