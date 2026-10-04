CREATE TABLE IF NOT EXISTS file_scans (
    id BIGSERIAL PRIMARY KEY,
    quarantine_id VARCHAR(36) NOT NULL,
    file_sha256 VARCHAR(64) NOT NULL,
    file_size BIGINT NOT NULL,
    original_filename VARCHAR(255),
    disposition VARCHAR(32) NOT NULL,
    threat_name VARCHAR(512),
    error_code VARCHAR(128),
    database_version VARCHAR(128) NOT NULL DEFAULT 'unknown',
    engine_version VARCHAR(128) NOT NULL DEFAULT 'unknown',
    scanner_state VARCHAR(32) NOT NULL DEFAULT 'healthy',
    scan_time_ms INTEGER NOT NULL DEFAULT 0,
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_file_scans_quarantine_id ON file_scans (quarantine_id);
CREATE INDEX IF NOT EXISTS ix_file_scans_file_sha256 ON file_scans (file_sha256);
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    filing_version_id BIGINT NOT NULL REFERENCES filing_versions(id),
    category VARCHAR(32) NOT NULL,
    action VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    source_ref VARCHAR(500),
    tool_name VARCHAR(128),
    tool_version VARCHAR(128),
    input_hash VARCHAR(64),
    output_hash VARCHAR(64),
    detail JSONB NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_audit_events_filing_version_id ON audit_events (filing_version_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_occurred_at ON audit_events (occurred_at);
