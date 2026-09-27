# Strider: System Architecture & Design Guidelines

## 1. System Overview & Core Principles
This document serves as the architectural master plan for a vendor-agnostic, local-first Personal AI Running Coach. The system ingests wearable health and activity data, processes it into a standardized schema, and utilizes a stateful AI agent to act as a proactive personal coach.

**Core Directives for the Implementing LLM:**
*   **Local-First & Idempotent:** All data resides locally. Every data pipeline operation must be idempotent, relying on a `sync_ledger` that tracks hashes and timestamps to handle upstream API mutability safely.
*   **Medallion Data Architecture:** Bronze (Raw API JSON/FIT) -> Silver (Standardized Parquet with Hive partitioning) -> Gold (Analytical DuckDB Views & Polars DataFrames).
*   **Vendor-Agnostic Base + Extensibility:** Schemas must enforce strict typing for universal concepts (e.g., HR, Pace, Distance) while capturing proprietary metrics (e.g., Garmin Body Battery, Stamina) in flexible JSON columns to avoid premature modeling constraints.
*   **User-Scoped Data Isolation:** The current deployment model is single-user (credentials in `.env`, one user per app instance), and that is acceptable for now. However, every data layer — file paths, partition keys, ledger entries, agent memory — must include an explicit `user_id` dimension from day one. This is cheap to add at design time and extremely expensive to retrofit once real data exists in user-unaware paths and schemas. The runtime can default to a single configured user without any code caring about multi-tenancy yet, but the data model must never assume there's only one.
*   **Proactive & Stateful Agent:** The LLM is not just a reactive chatbot. It must maintain long-term memory of the athlete's context (injuries, preferences) and execute proactive evaluation loops (e.g., "Morning Briefs").
*   **Strict Separation of Concerns:**
    *   `DuckDB` = SQL storage interface & query router.
    *   `Polars` = High-performance time-series computation and data manipulation.
    *   `Pydantic` = Schema validation and configuration management.
    *   `Pandera` = Typed DataFrame contracts (Polars backend).

## 2. Tech Stack
*   **Package Management:** `uv`
*   **Configuration & Validation:** `pydantic` and `pydantic-settings`
*   **Ingestion:** `garminconnect` (or equivalent vendor API adapters)
*   **Parsing:** `fitdecode` (FIT binary) + `polars` (JSON shredding, time-series batching)
*   **Data Processing & DataFrame Contracts:** `polars` + `pandera` (Polars backend for typed DataFrame schemas)
*   **Database & Storage:** `duckdb` + Local `parquet` files (Hive-partitioned)
*   **Backend Server:** `fastapi`
*   **Agentic Layer:** Model-agnostic LLM interface; agent execution loop (specific framework TBD at implementation time)
*   **Agent Memory:** Local persistent store for long-term semantic context (specific backing store TBD)
*   **UI:** Lightweight web UI served by `fastapi` (rendering approach TBD)

## 3. Configuration
*   **Environment & Config:** Use `pydantic-settings` (`BaseSettings`) in `src/core/config.py` to manage `.env` secrets (Garmin credentials), database paths, and API rate limits.

## 4. Data Modeling & Schema Design
Schemas have different responsibilities at each layer of the medallion architecture. The guiding principles:

*   **Bronze — manifest/metadata schemas only.** Bronze payloads are opaque blobs (FIT binaries, JSON dumps). Schemas at this layer describe *what was fetched* (user, vendor, entity type, source identifier, content hash, fetch timestamp) for the sync ledger — not the payload contents.
*   **Silver — the vendor-agnostic data contract.** Silver schemas define strongly-typed, universal columns for domain concepts (HR, pace, distance, timestamps, zones) plus a flexible `vendor_specific` escape hatch for proprietary metrics. This is the convergence point where vendor differences are erased. Every Silver record carries `user_id` as a first-class column.
*   **Gold — derived/computed schemas.** Gold schemas define analytical aggregates and LLM-facing summaries. They consume Silver schemas and add computed fields. All Gold views and computations are scoped to a single user — never aggregate across users.
*   **Typed DataFrames via Pandera.** Functions that return Polars DataFrames must carry schema awareness at the type level using `pandera.polars`. The same schema definition used for record-level Pydantic validation should also determine the DataFrame column contract — a single source of truth, not parallel definitions that can drift.

## 5. Composable Pipeline Architecture
The ingestion-to-Silver pipeline is composed of four abstract stages, each defined as a protocol:

1.  **Source Connector** — vendor-specific: authentication, entity discovery, raw payload download → Bronze. Each vendor has its own connector implementation.
2.  **Format Parser** — format-specific, *not* vendor-specific: parses raw payloads (FIT binary, JSON arrays) into structured records. Reusable across vendors that share a format (most wearable vendors produce FIT files, so the FIT parser will be the most commonly reused implementation — but the abstraction is at the parser protocol level, not FIT-assumed).
3.  **Schema Validator** — universal: validates parsed records against Silver Pydantic/Pandera schemas, producing typed DataFrames.
4.  **Writer** — universal: writes validated DataFrames to Hive-partitioned Parquet.

A pipeline is assembled by composing concrete implementations of these protocols. Example: `GarminConnector + FITParser + SilverActivityValidator + ParquetWriter`. A future "import FIT from folder" source reuses stages 2–4 with a `LocalFolderConnector`.

**Key constraint:** Silver onward is vendor-blind. No module in `processing/`, `analytics/`, or `agent/` ever imports from a vendor-specific adapter.

## 6. Data Architecture (Medallion Pattern)

### Bronze Layer (Raw & Immutable)
*   **Purpose:** Immutable audit log of raw payloads. Never queried directly for analytics.
*   **Storage:** Local file system, partitioned by user first: `data/bronze/{user_id}/{vendor}/{entity_type}/...`. Even with a single user today, the path structure must include the user dimension so that file layouts never need migration.
*   **Three first-class entity families** (confirmed via `notebooks/garminconnect_test.ipynb` spikes):
    1.  **Activities** — original device-recorded FIT files (zipped).
    2.  **Wellness/monitoring** — per-day binary FIT snapshots (`WELLNESS`, `SLEEP_DATA`, `METRICS`). FIT is preferred over JSON endpoints for time-series data; JSON endpoints remain useful for ad-hoc reads but are not the Bronze source of truth. If a metric is JSON-only (no FIT equivalent), save the JSON in Bronze.
    3.  **Workouts (plan side)** — raw FIT for workout *definitions* (steps, targets, structure) + JSON for the scheduled *calendar* (which workout is assigned on which date). Calendar metadata is JSON-only since it isn't device-recorded.
*   Each entity is tracked via the `sync_ledger` with content hashes for idempotent re-ingestion.

### Silver Layer (Standardized & Partitioned)
*   **Purpose:** Clean, strongly-typed columnar data optimized for analytical scanning. Produced by the composable pipeline (§5).
*   **Storage:** Local Parquet files using Hive Partitioning with `user_id` as the outermost partition level: `data/silver/{entity_type}/user_id={uid}/year=YYYY/month=MM/...`. This ensures user data is physically isolated at the file system level, and any scan or query is naturally pruned to a single user's partitions.
*   **Mutability Handling:** Parquet is update-hostile. The `sync_ledger` tracks a hash/`updated_at` flag per entity, scoped by `user_id`. Updated payloads trigger a partition-aware overwrite (delete old records from target partition, rewrite).
*   **Modeling Strategy:**
    *   Clear primary keys per entity (e.g., `activity_id`, `workout_id`, `calendar_date`).
    *   Universal metrics in strict typed columns.
    *   Proprietary/unknown fields in a `vendor_specific` JSON string column.

### Gold Layer (Analytics & Aggregations)
*   **Purpose:** Business logic, sports science metrics (ACWR, TRIMP, HR Zones), and LLM context generation.
*   **Implementation:** Materialized dynamically via DuckDB views, or computed via Polars for complex rolling window calculations.

## 7. Qualitative Context (The Coaching Journal)

While the Gold layer provides quantitative metrics (Load, ACWR, readiness), human fitness involves realities that don't fit natively into numbers (e.g., "I hurt my foot", "slept poorly because of stress"). 

To bridge this gap without over-engineering rigid "Training Plan" state machines, the architecture introduces a simpler, holistic **Coaching Journal** as a first-class entity.

### Purpose
The Journal is a continuous, chronological timeline tied directly to the `user_id`. It sits alongside the data layers and gives the agentic framework its necessary context to interpret the numbers.

### Contents
1.  **Manual User Logs:** Free-form or semi-structured entries logged by the user (e.g., feeling logs, injury reports like "twisted ankle on trail," perceived exertion). This captures what vendor APIs miss.
2.  **AI Narrative:** Append-only observations generated by the agent over time (e.g., "Week 3: Athlete managed the intervals well, but accumulated heavy load from 3 hours of ad-hoc canicross. Suggested prioritizing rest."). 

By keeping the journal holistic (aware of scheduled workouts, ad-hoc activities, lifestyle, and manual logs), it avoids the trap of only caring about formal "training plans." The AI coach sees the whole athlete.

## 8. Agentic Framework & Execution

### Agent Memory vs. Journal Context
The agent draws on two distinct sources of qualitative context:
*   **Static Memory** — user-level facts: personal preferences ("prefers morning runs"), communication style preferences.
*   **The Journal** — evolving, chronological state: recent injuries, recent AI observations, subjective feeling logs. 

Both are injected into the agent's system prompt to provide the stable athlete profile and the dynamic training context.

### Context Window Protection
*   **Anti-Pattern:** Feeding raw 1Hz time-series data (e.g., every second of a 3-hour run) to the LLM.
*   **Solution:** All agent tools must return structured, pre-aggregated semantic summaries — time in HR zones, lap splits, peak metrics, cardiac drift. The Journal is itself a form of pre-aggregated semantic context.

### Execution Model (Manual / Local)
*   For the initial implementation, the app will run locally on the user's PC.
*   There are no background daemons, chron jobs, or overnight processing loops. 
*   **Triggering:** The agent and data pipelines are invoked manually on-demand by the user (e.g., booting the app, clicking "Sync", or asking "Give me my morning brief"). The architecture must support this manual, stateless operational model while relying on the persistent local DB/files for continuity.

## 9. Target Repository Structure

```text
strider/
├── .env                        # Local secrets (git-ignored)
├── pyproject.toml              # uv dependencies
├── src/
│   ├── main.py                 # FastAPI application & startup loops
│   ├── core/
│   │   ├── config.py           # pydantic-settings
│   │   ├── schemas.py          # Pydantic data models (per-layer contracts)
│   │   └── dataframes.py       # Pandera DataFrame schemas (typed Polars frames)
│   ├── ingestion/
│   │   ├── protocols.py        # Abstract SourceConnector protocol
│   │   ├── ledger.py           # Sync state, hashes, and idempotency logic
│   │   └── garmin/             # Garmin-specific connector implementation
│   │       └── connector.py
│   ├── processing/
│   │   ├── protocols.py        # Abstract FormatParser, SchemaValidator, Writer protocols
│   │   ├── pipeline.py         # Pipeline composition & orchestration
│   │   └── parsers/
│   │       └── fit.py          # FIT binary parser (reusable across FIT-producing vendors)
│   ├── analytics/
│   │   ├── queries.py          # DuckDB SQL strings and view definitions
│   │   └── science.py          # Polars timeseries math (ACWR, GAP, etc.)
│   ├── agent/
│   │   ├── coach.py            # Agent execution entry point
│   │   ├── memory.py           # Long-term user static memory
│   │   ├── journal.py          # Qualitative timeline (feelings, injuries, AI narrative)
│   │   └── tools.py            # Semantic summary tools for the LLM
│   └── ui/                     # Web UI templates and static files
├── data/
│   ├── ledger.duckdb           # Ingestion state tracking
│   ├── memory.db               # Agent's user-level semantic memory
│   ├── bronze/                 # Immutable raw files
│   └── silver/                 # Hive-partitioned Parquet files
└── README.md
```