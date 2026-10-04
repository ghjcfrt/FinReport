# Alembic migrations

Database schema changes are append-only migrations owned by the persistence adapter. The current local demo creates tables on startup; before PostgreSQL integration, generate the initial migration here and make deployment run `alembic upgrade head`.
