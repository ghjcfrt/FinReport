from __future__ import annotations

import hashlib
import json


def scoped_cache_key(*, company_id: int, filing_version_id: int, period_end: str, scope: str, currency: str, rule_version: str, industry_version: str, permission_scope: str, object_kind: str) -> str:
    payload = {
        "company_id": company_id,
        "filing_version_id": filing_version_id,
        "period_end": period_end,
        "scope": scope,
        "currency": currency,
        "rule_version": rule_version,
        "industry_version": industry_version,
        "permission_scope": permission_scope,
        "object_kind": object_kind,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"finreport:{object_kind}:{digest}"
