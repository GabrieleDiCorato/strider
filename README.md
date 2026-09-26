# strider
My personal running assistant.

A vendor-agnostic, local-first Personal AI Running Coach. Strider ingests wearable health and activity data (starting with Garmin), standardizes it through a medallion data pipeline, and will use a stateful AI agent to act as a proactive personal coach. See [docs/architecture_guidelines.md](docs/architecture_guidelines.md) for the full architecture and implementation roadmap.

## Tech Stack

- **Package management:** [uv](https://docs.astral.sh/uv/)
- **Config & validation:** `pydantic`, `pydantic-settings`
- **Ingestion:** `garminconnect`
- **Parsing:** `fitdecode` (FIT files), `polars` (JSON/time-series shredding)
- **Data processing:** `polars`
- **Storage:** `duckdb` + local Hive-partitioned `parquet` files
- **Backend:** `fastapi`
- **Agentic layer:** LLM-based ReAct/LangGraph coach (planned)

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

# Run a script
uv run <script>
```

Requires Python 3.12 (see `.python-version`).

## Project Status

Early development. Following the roadmap in [docs/architecture_guidelines.md](docs/architecture_guidelines.md#7-implementation-roadmap-first-steps): configuration & schemas → ingestion → Bronze-to-Silver standardization → analytics → agentic layer.
