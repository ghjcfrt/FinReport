# Native Fact Automatic Validation v1

This module defines a narrow, versioned source-binding contract for facts extracted from native PDF text. It does not alter the parser, strict gateway, database schema, API, OCR path, or historical admission policy by itself. The persistence layer may explicitly select the policy and use the receipt; an absent or unknown policy must never silently switch contracts.

## Input and output

`FactAutoValidationInput` binds the policy version, the completed full-document extraction job identity and parser version, filing identity, and an immutable projection of each active financial fact with all evidence rows and dimensions. Decimal values are serialized as decimal strings. Admission status and reviewer fields are excluded because the policy writes the former and does not use the latter.

`validate_native_fact_snapshot(pdf_bytes, snapshot)` reconstructs disclosures from the supplied original PDF bytes. The receipt records a decision for every active fact, the complete input snapshot hash, source hash, and a source proof when the disclosure is unique. `verify_native_fact_receipt` repeats extraction and compares the whole reconstructed receipt; outer hashes cannot conceal changed facts, spans, geometry, or decisions.

The current policy supports only full-document `pdf_text` extraction with parser `pymupdf-statements-v5`, a calendar-year annual filing, consolidated PRC accounting scope, and a unique native primary-statement row with same-page explicit unit, currency, table title, and year header. It stores original page-text offsets and glyph geometry for those anchors and the value cell. It does not treat bounding-box JSON fields as source proof.

## Decisions

- `auto_passed`: the active fact's concept, raw amount, normalized amount, source unit, period, declared scope, extractor, page hash, row/column labels, and original geometry exactly agree with one supported disclosure in the immutable source. This only means the source binding passed this policy.
- `needs_review`: source binding is absent, ambiguous, unsupported, inherited, or inconsistent. This status does not request or require a person to approve it. It stays outside strict calculations.
- `rejected`: the declared PDF bytes do not match the filing source, or fact identity/type/value/period/scope violates the contract.
- `candidate`: this contract does not apply, including OCR, selected-page extraction, unsupported filing periods/scopes, or a parser version outside the contract.

The parser's two known cash-flow labels `加：期初现金及现金等价物余额` and `加：年初现金及现金等价物余额` are explicit entries in the policy's source-label registry. The validator does not strip arbitrary `加：`, `减：`, `其中：`, negation, or other prefixes. Ambiguous repeated concepts, rows, columns, pages, duplicate facts, or duplicate evidence are never automatically admitted.

## Boundaries

OCR uses pixels and does not open or inspect the native PDF text layer in this validator. A source-binding pass does not independently establish that the issuer published the PDF, the issuer identity, audit opinion, actual consolidation population, accounting comparability, free-text semantic support, or financial truth. A matching balance-sheet or cash-flow equation remains only an additional financial consistency check; it cannot repair a wrong source label, certify an amount, or prove an interpretation. Source assurance and all required financial equations remain separate strict gates. No reviewer records are created.

Same-parser common-cause risk remains: the stored fact was initially produced by `pymupdf-statements-v5`, and the receipt re-extracts from the same PDF with a separate geometry-based matching algorithm. Exact re-extraction catches drift and bad bindings, but both paths may share PyMuPDF's interpretation of a malformed or adversarial text layer. It is not a human/visual gold standard. Publisher authenticity, issuer/period attribution outside the document, and audit opinion need separate evidence. Free-form explanations remain subject to their semantic-support policy and are never certified by this receipt.

## Local verification

```powershell
uv run --no-sync pytest tests/unit/infrastructure/test_fact_auto_validation.py -q
uv run --no-sync ruff check packages/finance-adapters/src/finance_adapters/providers/fact_auto_validation.py tests/unit/infrastructure/test_fact_auto_validation.py
```

The focused tests construct native PDFs and cover exact source spans, repeated same-value rows/pages, missing or conflicting unit/currency/period/scope anchors, prefix and restatement ambiguity, forged input DTOs, OCR isolation, hostile decimal contexts, and receipt reconstruction. They test the adapter contract; they do not measure financial accuracy or independently prove the underlying PDF's truth.
