-- Additive audit fields for PostgreSQL deployments. SQLite development startup
-- applies the equivalent additive columns automatically.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS business_model_tags JSONB NOT NULL DEFAULT '[]';
ALTER TABLE filing_versions ADD COLUMN IF NOT EXISTS source_priority INTEGER NOT NULL DEFAULT 3;
ALTER TABLE extraction_jobs ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE evidence_fragments ADD COLUMN IF NOT EXISTS extractor VARCHAR(128) NOT NULL DEFAULT 'pymupdf-text-v1';
ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS orchestration_version VARCHAR(64) NOT NULL DEFAULT 'analysis-v1';
ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS tool_trace JSONB;
ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE metric_results ADD COLUMN IF NOT EXISTS quality_status VARCHAR(32) NOT NULL DEFAULT 'strict_audited';
ALTER TABLE metric_results ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'consolidated';
ALTER TABLE metric_results ADD COLUMN IF NOT EXISTS period_basis VARCHAR(32) NOT NULL DEFAULT 'annual';
CREATE TABLE IF NOT EXISTS fx_source_registry (id BIGSERIAL PRIMARY KEY, source_name VARCHAR(128) NOT NULL, release_id VARCHAR(128) NOT NULL, license_note TEXT, timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai', version VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'frozen');
CREATE TABLE IF NOT EXISTS fx_rates (id BIGSERIAL PRIMARY KEY, source_registry_id BIGINT NOT NULL REFERENCES fx_source_registry(id), quote_pair VARCHAR(32) NOT NULL, quote_direction VARCHAR(16) NOT NULL, rate_type VARCHAR(32) NOT NULL, rate_date DATE NOT NULL, rate NUMERIC(24,12) NOT NULL, fallback_date DATE, captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), version VARCHAR(64) NOT NULL);
CREATE TABLE IF NOT EXISTS peer_universes (id BIGSERIAL PRIMARY KEY, version VARCHAR(64) UNIQUE NOT NULL, industry_code VARCHAR(64) NOT NULL, taxonomy_version VARCHAR(64) NOT NULL, data_cutoff DATE NOT NULL, filing_ids JSONB NOT NULL DEFAULT '[]', coverage NUMERIC(5,4) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'candidate', approval_trace JSONB NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS comparison_runs (id BIGSERIAL PRIMARY KEY, peer_universe_version VARCHAR(64) NOT NULL, metric_code VARCHAR(96) NOT NULL, input_snapshot_hash VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, output JSONB, failure_reason TEXT);
CREATE TABLE IF NOT EXISTS anomaly_observations (id BIGSERIAL PRIMARY KEY, filing_version_id BIGINT NOT NULL REFERENCES filing_versions(id), rule_code VARCHAR(128) NOT NULL, rule_version VARCHAR(64) NOT NULL DEFAULT 'anomaly-v1', status VARCHAR(32) NOT NULL, explanation TEXT NOT NULL, input_snapshot_hash VARCHAR(64), threshold VARCHAR(128), peer_universe_version VARCHAR(64), suppression_reason TEXT);
CREATE TABLE IF NOT EXISTS ocr_calls (id BIGSERIAL PRIMARY KEY, extraction_job_id BIGINT NOT NULL REFERENCES extraction_jobs(id), provider VARCHAR(64) NOT NULL, model VARCHAR(200) NOT NULL, page_no INTEGER NOT NULL, page_sha256 VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, response_id VARCHAR(200), response_hash VARCHAR(64), duration_ms INTEGER, error_detail TEXT);
CREATE TABLE IF NOT EXISTS table_candidates (id BIGSERIAL PRIMARY KEY, extraction_job_id BIGINT NOT NULL REFERENCES extraction_jobs(id), page_no INTEGER NOT NULL, rows JSONB NOT NULL, extractor VARCHAR(128) NOT NULL, confidence NUMERIC(5,4) NOT NULL, review_required BOOLEAN NOT NULL DEFAULT TRUE);
ALTER TABLE financial_facts ADD COLUMN IF NOT EXISTS comparative_period BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE financial_facts ADD COLUMN IF NOT EXISTS is_restatement BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fact_reviews ADD COLUMN IF NOT EXISTS evidence_digest VARCHAR(64);
ALTER TABLE validation_results ADD COLUMN IF NOT EXISTS tolerance VARCHAR(128);
ALTER TABLE analysis_claims ADD COLUMN IF NOT EXISTS review_status VARCHAR(32) NOT NULL DEFAULT 'not_required';
CREATE TABLE IF NOT EXISTS concepts (id BIGSERIAL PRIMARY KEY, code VARCHAR(96) UNIQUE NOT NULL, name VARCHAR(200) NOT NULL, statement_type VARCHAR(32) NOT NULL, normal_balance VARCHAR(16) NOT NULL, mapping_version VARCHAR(64) NOT NULL);
CREATE TABLE IF NOT EXISTS fact_dimensions (id BIGSERIAL PRIMARY KEY, fact_id BIGINT NOT NULL REFERENCES financial_facts(id), dimension_name VARCHAR(96) NOT NULL, dimension_value VARCHAR(256) NOT NULL);
CREATE TABLE IF NOT EXISTS metric_definitions (id BIGSERIAL PRIMARY KEY, code VARCHAR(96) UNIQUE NOT NULL, name VARCHAR(200) NOT NULL, formula_version VARCHAR(64) NOT NULL, unit VARCHAR(32) NOT NULL, required_concepts JSONB NOT NULL DEFAULT '[]', applicable_sectors JSONB NOT NULL DEFAULT '[]', exclusions JSONB NOT NULL DEFAULT '[]', annualization_policy VARCHAR(64) NOT NULL DEFAULT 'none', status VARCHAR(32) NOT NULL DEFAULT 'planned');
CREATE TABLE IF NOT EXISTS industry_modules (id BIGSERIAL PRIMARY KEY, code VARCHAR(64) UNIQUE NOT NULL, version VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'planned', sample_count INTEGER NOT NULL DEFAULT 0, coverage_note TEXT NOT NULL DEFAULT '');
