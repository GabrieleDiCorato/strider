# Strider — Copilot Instructions

Strider is a vendor-agnostic, local-first Personal AI Running Coach. Full architectural detail lives in [docs/architecture_guidelines.md](../docs/architecture_guidelines.md) — read it before making structural decisions.

## Architecture

- **Medallion data pipeline:** Bronze (raw API JSON/FIT, immutable) → Silver (standardized Hive-partitioned Parquet) → Gold (DuckDB views / Polars aggregates for analytics and LLM context).
- **Strict separation of concerns:** DuckDB = SQL storage/query interface. Polars = time-series computation. Pydantic = schema validation and configuration — never blur these boundaries.
- **Idempotency is mandatory:** every ingestion/processing step must consult and update the `sync_ledger` (hash + timestamp per entity) so re-runs and upstream mutations are handled safely.
- **Vendor-agnostic schemas:** universal metrics (HR, pace, distance) get strict typed columns; proprietary metrics (e.g. Garmin Body Battery) go into a `vendor_specific` JSON column.
- **Agentic layer is separate:** the stateful LLM coach (`src/agent/`) has its own scope/agent and must never receive raw high-frequency time-series — only pre-aggregated semantic summaries.
- Target module layout (`src/core`, `src/ingestion`, `src/processing`, `src/analytics`, `src/agent`, `src/ui`) is defined in section 6 of the architecture doc.

## Build and Test

- Package management is `uv` (Python 3.12 pinned in `.python-version`). Use `uv add <pkg>` / `uv sync`, not raw `pip`.
- Run scripts with `uv run <script>`.

## Conventions

- Follow the implementation roadmap order in section 7 of the architecture doc: config/schemas → ingestion/ledger → Bronze→Silver standardization → analytics/agent. Don't skip ahead to Gold/agent work before Silver ingestion is idempotent and operational.
- Use `pydantic-settings` for all config/secrets (`src/core/config.py`), never read `.env` directly.
- Use `Field(alias=...)` in Pydantic schemas to map upstream vendor field names to standardized names.
