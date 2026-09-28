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

*   **Bronze — manifest/metadata schemas only.** Bronze payloads are opaque blobs (FIT binaries, JSON dumps). Schemas at this layer describe *what was fetched* (user, vendor, entity type, source identifier, content hash, fetch timestamp) for the sync ledger — not the payload contents. `entity_type` is an `EntityType` enum, not a free-form string.
*   **Silver — the vendor-blind data contract.** Silver is the convergence point where vendor differences are erased. Silver schemas define strongly-typed, universal columns for domain concepts (HR, pace, distance, timestamps, zones) plus a flexible `vendor_specific` escape hatch for proprietary metrics. There is **no `vendor` field on Silver records** — vendor provenance is tracked exclusively in the Bronze ledger. Every Silver record carries `user_id` as a first-class column.
*   **Gold — derived/computed schemas.** Gold schemas define analytical aggregates and LLM-facing summaries. They consume Silver schemas and add computed fields. Gold computations may also read directly from Bronze FIT files for on-demand deep analysis (e.g. cardiac drift, pace decoupling) where materializing the raw time-series in Silver would be wasteful. All Gold views and computations are scoped to a single user — never aggregate across users.
*   **Enum-driven type safety.** Universal domain concepts that the architecture calls "strict typed columns" are modeled as `(str, Enum)` types — `SportType`, `StepType`, `DurationType`, `TargetType`, `TrainingStatus`, `HrvStatus`. These serialize cleanly to Parquet/JSON (they're `str` subclasses) while enabling exhaustive pattern matching in code. Free-form strings are reserved for truly open-ended fields (e.g. `sport_sub_type` for vendor-specific subtypes like `treadmill_running`).
*   **Silver stores pre-aggregated summaries, not raw time-series.** Activity time-series (1 Hz HR, GPS, cadence, power) and daily wellness time-series (HR epochs, stress curves, body battery values) stay in Bronze as FIT files. Silver stores one summary row per entity instance — with pre-computed aggregates like HR zone distribution (`hr_zone_seconds`), per-lap breakdowns (`lap_summaries`), and sleep stage durations. This avoids a 100×+ storage multiplication while keeping the coaching agent's most common queries (daily readiness, activity summary) as single-scan operations. Gold computations that need raw time-series read Bronze FIT files on-demand via `fitdecode` + Polars.
*   **Typed DataFrames via Pandera.** Functions that return Polars DataFrames must carry schema awareness at the type level using `pandera.polars`. Pandera `DataFrameModel` schemas are auto-derived from the Pydantic models in `core.schemas` via `derive_schema()` in `core.dataframes` — a single source of truth. JSON-serialized types (`dict`, `list`, nested `BaseModel`) and `str`-based Enums are auto-detected and mapped to `Series[str]` without manual allowlists.

## 5. Composable Pipeline Architecture
The ingestion-to-Silver pipeline is composed of three abstract stages, each defined as a protocol:

1.  **Source Connector** (`ingestion.protocols.SourceConnector`) — vendor-specific: authentication, entity discovery, raw payload download → Bronze. Each vendor has its own connector implementation. The connector exposes a **single `fetch(user_id, entity_type, start, end)` method** parameterised by an `EntityType` enum rather than one method per entity family. A `supported_entities()` method lets the orchestrator discover what this connector can fetch. Adding a new entity type means adding an `EntityType` member and a handler inside the connector — not changing the protocol interface.
2.  **Entity Extractor** (`processing.protocols.EntityExtractor[SilverT]`) — entity-specific, format-aware: reads Bronze payloads (FIT binary via `fitdecode`, JSON via `json.loads`) and produces **typed Silver Pydantic models directly**. Each implementation is bound to one Silver entity type via the `SilverT` type parameter (e.g. `GarminActivityExtractor` → `EntityExtractor[SilverActivity]`). This stage deliberately collapses the former `FormatParser` + `SchemaValidator` two-step: `fitdecode` already IS the format parser (it returns typed `FitDataMessage` objects), and the field mapping from FIT messages to Silver columns is inherently entity-specific — splitting it across two protocols only created an untyped `list[dict]` gap in the middle.
3.  **Writer** (`processing.protocols.Writer[SilverT]`) — universal: receives typed Pydantic model instances, converts to a Polars DataFrame, validates against the Pandera schema (from `core.dataframes`), and writes to Hive-partitioned Parquet.

A pipeline is assembled by composing concrete implementations of these protocols:

```python
connector = GarminConnector(settings)
extractors: dict[EntityType, EntityExtractor] = {
    EntityType.ACTIVITY: GarminActivityExtractor(),
    EntityType.DAILY_SUMMARY: GarminDailySummaryExtractor(),
    EntityType.WORKOUT_DEFINITION: GarminWorkoutExtractor(),
    EntityType.WORKOUT_CALENDAR: GarminCalendarExtractor(),
}
writer = ParquetWriter(settings)

for entity_type in connector.supported_entities():
    entries = connector.fetch(user_id, entity_type, start, end)
    records = extractors[entity_type].extract(entries)
    writer.write(records, entity_type)
```

A future "import FIT from folder" source reuses the same extractors and writer with a `LocalFolderConnector`.

**Key constraint:** Silver onward is vendor-blind. No module in `processing/`, `analytics/`, or `agent/` ever imports from a vendor-specific adapter.

## 6. Data Architecture (Medallion Pattern)

### Bronze Layer (Raw & Immutable)
*   **Purpose:** Immutable audit log of raw payloads. Never queried directly for analytics — **except** by Gold-layer computations that need raw time-series data (e.g. cardiac drift, pace decoupling analysis). In those cases, `fitdecode` + Polars reads the FIT files on-demand and computes aggregates in memory.
*   **Storage:** Local file system, partitioned by user first: `data/bronze/{user_id}/{vendor}/{entity_type}/...`. Even with a single user today, the path structure must include the user dimension so that file layouts never need migration.
*   **Entity families** (confirmed via `notebooks/garminconnect_test.ipynb` spikes), aligned with the `EntityType` enum:
    1.  **Activities** (`EntityType.ACTIVITY`) — original device-recorded FIT files (zipped).
    2.  **Daily summary** (`EntityType.DAILY_SUMMARY`) — per-day binary FIT snapshots (`WELLNESS`, `SLEEP_DATA`, `METRICS`). FIT is preferred over JSON endpoints for time-series data; JSON endpoints remain useful for ad-hoc reads but are not the Bronze source of truth. If a metric is JSON-only (no FIT equivalent), save the JSON in Bronze.
    3.  **Workout definitions** (`EntityType.WORKOUT_DEFINITION`) — raw FIT for workout *definitions* (steps, targets, structure). The FIT file is not zipped (unlike activities).
    4.  **Workout calendar** (`EntityType.WORKOUT_CALENDAR`) — JSON for the scheduled *calendar* (which workout is assigned on which date). Calendar metadata is JSON-only since it isn't device-recorded.
*   Each entity is tracked via the `sync_ledger` with content hashes for idempotent re-ingestion.
*   **Bronze is the source of truth for all time-series data.** Raw 1 Hz activity records (HR, GPS, cadence, power) and daily wellness epochs (HR curves, stress values, body battery) are retained in FIT files and are NOT materialized in Silver. This avoids 100×+ storage multiplication for data that is only consumed to produce summary aggregates.

### Silver Layer (Standardized & Partitioned)
*   **Purpose:** Clean, strongly-typed columnar data optimized for analytical scanning. Produced by the composable pipeline (§5). **One summary row per entity instance** — no raw time-series.
*   **Storage:** Local Parquet files using Hive Partitioning with `user_id` as the outermost partition level: `data/silver/{entity_type}/user_id={uid}/year=YYYY/month=MM/...`. This ensures user data is physically isolated at the file system level, and any scan or query is naturally pruned to a single user's partitions.
*   **Mutability Handling:** Parquet is update-hostile. The `sync_ledger` tracks a hash/`updated_at` flag per entity, scoped by `user_id`. Updated payloads trigger a partition-aware overwrite (delete old records from target partition, rewrite).
*   **Concrete Silver entities:**
    *   `SilverActivity` — one row per recorded activity (~31 fields): sport type, timing (elapsed + moving), distance, elevation (ascent + descent), HR (avg, max, zone distribution), cadence, power (avg, max, normalized), training effect, temperature, pre-aggregated lap summaries, workout linkage.
    *   `SilverDailySummary` — one row per user per calendar day (~32 fields): sleep architecture (score, stage durations, start/end times, overnight HR/HRV), readiness (resting HR + 7-day trend, HRV status, body battery, stress, training status, training load balance, VO2max), body metrics (SpO2, respiration, steps, calories).
    *   `SilverWorkoutDefinition` — one row per workout definition: title, sport type, estimated duration/distance, recursive step structure with repeat groups.
    *   `SilverWorkoutCalendar` — one row per scheduled workout: date, workout link, training plan id, status.
*   **Modeling Strategy:**
    *   Clear primary keys per entity (e.g., `activity_id`, `workout_id`, `calendar_date`).
    *   Universal metrics in strict typed columns with enum types (`SportType`, `TrainingStatus`, `HrvStatus`, etc.).
    *   Pre-aggregated time-series summaries (HR zone distribution, lap breakdowns, sleep stage durations) stored as JSON-serialized nested structures.
    *   Proprietary/unknown fields in a `vendor_specific` JSON string column — never needed for core coaching logic.

### Gold Layer (Analytics & Aggregations)
*   **Purpose:** Business logic, sports science metrics (ACWR, TRIMP, HR Zones), and LLM context generation.
*   **Implementation:** Materialized dynamically via DuckDB views over Silver Parquet, or computed via Polars for complex rolling window calculations. For analyses requiring raw time-series (cardiac drift, pace decoupling, split-by-split HR recovery), Gold computations read Bronze FIT files on-demand via `fitdecode` + Polars — this is acceptable because: (a) it only happens for specific ad-hoc analyses, not routine daily queries, (b) parsing a typical FIT file takes ~50–100 ms, imperceptible for a single-user local app, (c) Bronze files are immutable and always available.

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
│   │   ├── protocols.py        # Abstract EntityExtractor, Writer protocols
│   │   ├── pipeline.py         # Pipeline composition & orchestration
│   │   └── extractors/         # Format-aware, entity-specific extractors (e.g. Garmin activity FIT extractor)
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

## 10. Future Feature Explorations

*   **Geographic Data Visualization:** The first step is displaying GPS trace points on interactive maps. Aligning with the time-series storage strategy, these traces will either be read on-demand directly from Bronze `.FIT` files, or materialized into a dedicated, separate `SilverGpsTrace` entity (to avoid bloating the `SilverActivity` summary table). This will allow the athlete to visually review their completed routes.
*   **Route Planning:** A subsequent, distinct feature to enable the athlete (or the AI Coach) to plan, generate, or explore new running routes (e.g., using open routing APIs like Mapbox or OSRM).