"""Replay the deterministic calculation from a saved period-comparison response."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_adapters.persistence.comparative_gateway import calculate_period_comparison


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        saved = json.loads(args.bundle.read_text(encoding="utf-8"))
        saved = saved.get("data", saved)
        replay = calculate_period_comparison(saved["inputs"])
        same = all(replay[key] == saved[key] for key in replay)
        print(json.dumps({"status": "matched" if same else "mismatch", "replayed": replay}, ensure_ascii=False))
        return 0 if same else 1
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, AttributeError) as exc:
        print(json.dumps({"status": "invalid_bundle", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
