# strider
My personal running assistant.

A vendor-agnostic, local-first Personal AI Running Coach. Strider ingests wearable health and activity data (starting with Garmin), standardizes it through a medallion data pipeline, and will use a stateful AI agent to act as a proactive personal coach. See [docs/architecture_guidelines.md](docs/architecture_guidelines.md) for the full architecture and implementation roadmap.

## Tech Stack

- **Package Management:** [uv](https://docs.astral.sh/uv/)
- **Backend:** `fastapi`, `uvicorn`
- **Frontend / UI:** `jinja2` templates, `htmx`, and vanilla CSS
- **Data Processing:** `polars` for computation, `pandera` for typed DataFrame contracts
- **Storage:** `duckdb` + local Hive-partitioned `parquet` files
- **Parsing & Ingestion:** `garminconnect` for API, `fitdecode` for FIT binary files
- **Config & Validation:** `pydantic`, `pydantic-settings`
- **Agentic Layer:** `google-adk`

## Data Architecture

Data flows through a medallion pipeline:

1. **Bronze** — immutable raw API responses (JSON/FIT) on local disk.
2. **Silver** — standardized, strongly-typed, Hive-partitioned Parquet files.
3. **Gold** — analytical DuckDB views and Polars aggregates (sports science metrics, LLM context).

All pipeline steps are idempotent via a `sync_ledger` that tracks hashes/timestamps per entity.

## Getting Started

```bash
# Install dependencies
uv sync

# Start the development web server
uv run uvicorn src.main:app --port 8000 --reload
```

The web interface will be available at [http://127.0.0.1:8000](http://127.0.0.1:8000).

Requires Python 3.12 (see `.python-version`).

## Project Status

Early development. Following the roadmap in [docs/architecture_guidelines.md](docs/architecture_guidelines.md#7-implementation-roadmap-first-steps): configuration & schemas → ingestion → Bronze-to-Silver standardization → analytics → agentic layer.
