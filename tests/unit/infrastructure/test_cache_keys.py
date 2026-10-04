from finance_adapters.cache_keys import scoped_cache_key


def test_cache_key_contains_scope_dimensions():
    key = scoped_cache_key(company_id=1, filing_version_id=2, period_end="2025-12-31", scope="consolidated", currency="CNY", rule_version="v1", industry_version="CSRC-2024", permission_scope="research", object_kind="metrics")
    assert key.startswith("finreport:metrics:")
    assert scoped_cache_key(company_id=1, filing_version_id=2, period_end="2025-12-31", scope="parent", currency="CNY", rule_version="v1", industry_version="CSRC-2024", permission_scope="research", object_kind="metrics") != key
