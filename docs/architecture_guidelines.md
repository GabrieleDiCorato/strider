# Personal AI Running Coach: System Architecture & Implementation Guidelines

## 1. System Overview & Core Principles
This document serves as the architectural master plan for a vendor-agnostic, local-first Personal AI Running Coach. The system ingests wearable health and activity data, processes it into a standardized schema, and utilizes a stateful AI agent to act as a proactive personal coach.

**Core Directives for the Implementing LLM:**
*   **Local-First & Idempotent:** All data resides locally. Every data pipeline operation must be idempotent, relying on a `sync_ledger` that tracks hashes and timestamps to handle upstream API mutability safely.
*   **Medallion Data Architecture:** Bronze (Raw API JSON/FIT) -> Silver (Standardized Parquet with Hive partitioning) -> Gold (Analytical DuckDB Views & Polars DataFrames).
*   **Vendor-Agnostic Base + Extensibility:** Schemas must enforce strict typing for universal concepts (e.g., HR, Pace, Distance) while capturing proprietary metrics (e.g., Garmin Body Battery, Stamina) in flexible JSON columns to avoid premature modeling constraints.
*   **Proactive & Stateful Agent:** The LLM is not just a reactive chatbot. It must maintain long-term memory of the athlete's context (injuries, preferences) and execute proactive evaluation loops (e.g., "Morning Briefs").
*   **Strict Separation of Concerns:** 
    *   `DuckDB` = SQL Storage Interface & Query Router.
    *   `Polars` = High-performance time-series computation and data manipulation.
    *   `Pydantic` = Schema validation and configuration management.

## 2. Tech Stack
*   **Package Management:** `uv`
*   **Configuration & Validation:** `pydantic` and `pydantic-settings`
*   **Ingestion:** `garminconnect` (or equivalent API adapters)
*   **Parsing:** `fitdecode` (metadata extraction) + `polars` (batching time-series arrays)
*   **Data Processing Engine:** `polars`
*   **Database & Storage:** `duckdb` + Local `parquet` files (Hive-partitioned)
*   **Backend Server:** `fastapi`
*   **Agentic Layer:** `litellm` (or direct OpenAI/Anthropic SDKs) + ReAct/LangGraph workflow.
*   **Agent Memory:** `sqlite` (or `LanceDB`) for storing semantic context.
*   **UI:** `fastapi` serving HTML/Jinja2 (HTMX recommended).

## 3. Configuration & Data Modeling (The Gateway)
Before data touches the analytical layers, it must be validated.
*   **Environment & Config:** Use `pydantic-settings` (`BaseSettings`) in `src/core/config.py` to manage `.env` secrets (Garmin credentials), database paths, and API rate limits.
*   **Data Contracts:** Use `pydantic` models in `src/core/schemas.py`. These models act as the bridge between Bronze and Silver. Use Pydantic's `Field(alias="...")` to map messy upstream API keys to the system's clean, standardized nomenclature.

## 4. Data Architecture (Medallion Pattern)

### Bronze Layer (Raw & Immutable)
*   **Purpose:** Immutable audit log of API responses. Never queried directly for analytics.
*   **Storage:** Local file system (`data/bronze/{vendor}/...`).
*   **Strategic Investigation (Wellness FIT vs. JSON):** REST APIs often default to JSON for high-frequency health data (all-day HR, HRV). JSON is highly inefficient for time-series. **The implementing engineer must investigate if the vendor API exposes raw Monitoring/Wellness FIT files.**
    *   *If FIT is available:* Prioritize binary parsing directly to Silver Parquet.
    *   *If JSON is forced:* Save the JSON in Bronze, then immediately use Polars to shred the nested arrays into flat DataFrames for the Silver layer.

### Silver Layer (Standardized & Partitioned)
*   **Purpose:** Clean, strongly-typed columnar data optimized for analytical scanning.
*   **Storage:** Local Parquet files using Hive Partitioning (e.g., `year=YYYY/month=MM/`).
*   **Mutability Handling:** Because Parquet is update-hostile, the `sync_ledger.duckdb` must track a hash/`updated_at` flag for each entity (e.g., `activity_id`). If an updated payload is detected in Bronze, the pipeline must perform a partition-aware overwrite (deleting the old records from the target partition and rewriting).
*   **General Modeling Strategy:** 
    *   Establish clear Primary Keys (e.g., `vendor_id`, `activity_id`, `calendar_date`).
    *   Extract universal metrics into strict columns.
    *   Dump unknown/proprietary fields into a `vendor_specific` JSON string column.

### Gold Layer (Analytics & Aggregations)
*   **Purpose:** Business logic, sports science metrics (ACWR, TRIMP, HR Zones), and LLM context generation.
*   **Implementation:** Materialized dynamically at runtime via DuckDB `CREATE VIEW` statements, or computed via Polars for complex rolling window calculations.

## 5. Agentic Framework & Execution

### Long-Term Memory
The agent requires continuous context to avoid repetitive advice.
*   Implement a local memory store.
*   Provide a `save_athlete_context(note: str)` tool to the LLM to persist constraints (e.g., "recovering from shin splints", "prefers morning runs"). Inject this context into the system prompt automatically.

### Context Window Protection
*   **Anti-Pattern:** Feeding raw 1Hz time-series data (e.g., every second of a 3-hour run) to the LLM.
*   **Solution:** Tools must return structured semantic summaries. Instead of raw HR data, a tool like `get_activity_summary(id)` should return Polars-computed aggregates: time in HR zones, lap splits, peak 5-min pace, and cardiac drift metrics.

### Proactive Execution Loop (The "Morning Brief")
*   Decouple the agent from strictly synchronous `/api/chat` interactions.
*   Implement a background task or startup script that runs overnight syncs, computes Gold metrics, and triggers a proactive LLM evaluation (e.g., generating a daily coaching brief based on last night's sleep and yesterday's load).

## 6. Target Repository Structure

```text
personal-run-coach/
├── .env                        # Local secrets (git-ignored)
├── pyproject.toml              # uv dependencies
├── src/
│   ├── main.py                 # FastAPI application & startup loops
│   ├── core/                   
│   │   ├── config.py           # pydantic-settings
│   │   └── schemas.py          # pydantic data models (Bronze -> Silver maps)
│   ├── ingestion/
│   │   ├── ledger.py           # Sync state, hashes, and idempotency logic
│   │   └── garmin_client.py    # Vendor API adapter
│   ├── processing/
│   │   ├── pipeline.py         # Polars ingestion & Parquet writing
│   │   └── parser_fit.py       # fitdecode binary parsing
│   ├── analytics/
│   │   ├── queries.py          # DuckDB SQL strings and view definitions
│   │   └── science.py          # Polars timeseries math (ACWR, GAP, etc.)
│   ├── agent/
│   │   ├── coach.py            # ReAct/LangGraph execution loop
│   │   ├── memory.py           # Long-term state persistence
│   │   └── tools.py            # Semantic summary tools for the LLM
│   └── ui/                     # FastAPI templates and static files
├── data/
│   ├── ledger.duckdb           # Ingestion state tracking
│   ├── memory.db               # Agent's semantic memory
│   ├── bronze/                 # Immutable raw files
│   └── silver/                 # Hive-partitioned Parquet files
└── README.md
```

## 7. Implementation Roadmap (First Steps)
When beginning implementation, the LLM should follow this strict sequence:
1.  **Foundation:** Setup `pyproject.toml`, `src/core/config.py`, and `src/core/schemas.py`. Define the data contract first.
2.  **Ingestion:** Build the `garmin_client.py` and the `ledger.py` state tracker. Ensure authentication works and raw payloads can be saved to the Bronze layer without duplication.
3.  **Standardization:** Build the Polars pipeline to convert Bronze payloads through Pydantic validation into Silver partitioned Parquet files.
*Do not proceed to Analytics or Agentic layers until step 3 is fully operational and idempotent.*