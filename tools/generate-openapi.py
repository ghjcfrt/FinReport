"""Regenerate the versioned API contract from the FastAPI composition root."""

import json
from pathlib import Path

from finreport_api.main import app

target = Path(__file__).parents[1] / "contracts" / "openapi" / "v1.json"
target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {target}")
