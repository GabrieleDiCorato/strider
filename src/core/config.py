"""Application configuration for Strider.

Single source of truth for secrets and file-system layout. Values are loaded
from process environment variables and, for local development, a git-ignored
`.env` file at the repository root (see `.env.template` for the required
keys). No other module should read `os.environ` or call `dotenv` directly —
import `get_settings()` instead.

Settings are grouped by concern:
- `GarminSettings`   -> vendor credentials/session options for `garminconnect`.
- `DataPathSettings` -> medallion layer directories + state-store paths.
- `RateLimitSettings`-> client-side throttling for the Garmin ingestion adapter.

Nested env vars use a `__` delimiter, e.g. `GARMIN__EMAIL`, `GARMIN__PASSWORD`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.schemas import UserId

# Repository root, resolved from this file's location (src/core/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


class GarminSettings(BaseModel):
    """Login credentials and session options for the Garmin Connect vendor adapter.

    Maps to `garminconnect.Garmin(email, password, is_cn=...)`. The token
    store lets the adapter reuse a cached OAuth session across runs instead
    of re-authenticating (and potentially re-prompting for MFA) every time.

    This is *authentication material*, not identity: it says which Garmin account to log
    into, not who that account belongs to in Strider's own terms. The (non-secret) link
    between a Strider user and this Garmin account is recorded separately in
    `core.schemas.GarminAccountLink`, which never duplicates the password.
    """

    email: str = Field(..., description="Garmin Connect account email/username.")
    password: SecretStr = Field(..., description="Garmin Connect account password.")
    is_cn: bool = Field(
        default=False,
        description="Set True for accounts registered on the China (garmin.cn) service.",
    )
    tokenstore_path: Path | None = Field(
        default=None,
        description=(
            "Directory used by garminconnect to cache OAuth tokens between sessions, "
            "avoiding a fresh credential login on every run. Defaults to a user-scoped "
            "`<data.root_dir>/auth/{user_id}/garmin` (resolved by `Settings`), keeping "
            "token caches isolated per Strider user rather than one shared machine-wide "
            "directory."
        ),
    )


class DataPathSettings(BaseModel):
    """Filesystem locations for the medallion data layers and state stores.

    Layer subpaths default to living under `root_dir` but can each be
    overridden independently (e.g. to place Bronze on a different disk).
    """

    root_dir: Path = Field(default_factory=lambda: REPO_ROOT / "data")
    bronze_dir: Path | None = Field(
        default=None, description="Immutable raw API/FIT payloads. Defaults to `<root_dir>/bronze`."
    )
    silver_dir: Path | None = Field(
        default=None,
        description="Hive-partitioned, standardized Parquet output. Defaults to `<root_dir>/silver`.",
    )
    ledger_db_path: Path | None = Field(
        default=None,
        description="DuckDB file tracking ingestion idempotency (hash/updated_at per entity).",
    )
    gold_db_path: Path | None = Field(
        default=None,
        description=(
            "DuckDB catalog file storing Gold-layer view definitions over the Silver "
            "Parquet directories. Stores only view SQL text, never row data. Defaults to "
            "`<root_dir>/gold.duckdb`."
        ),
    )
    memory_db_path: Path | None = Field(
        default=None, description="Agent long-term semantic memory store. Defaults to `<root_dir>/memory.db`."
    )

    def model_post_init(self, __context: Any) -> None:
        if self.bronze_dir is None:
            self.bronze_dir = self.root_dir / "bronze"
        if self.silver_dir is None:
            self.silver_dir = self.root_dir / "silver"
        if self.ledger_db_path is None:
            self.ledger_db_path = self.root_dir / "ledger.duckdb"
        if self.gold_db_path is None:
            self.gold_db_path = self.root_dir / "gold.duckdb"
        if self.memory_db_path is None:
            self.memory_db_path = self.root_dir / "memory.db"


class RateLimitSettings(BaseModel):
    """Client-side throttling to stay well under Garmin Connect's tolerances."""

    min_request_interval_seconds: float = Field(
        default=1.0,
        description="Minimum delay enforced between consecutive Garmin API calls.",
    )
    retry_attempts: int = Field(
        default=3, description="Transient network/5xx retries per request (passed to garminconnect)."
    )
    retry_min_wait_seconds: float = Field(
        default=1.0, description="Initial backoff before a retry (passed to garminconnect; grows exponentially)."
    )
    retry_max_wait_seconds: float = Field(
        default=10.0, description="Upper bound on retry backoff (passed to garminconnect)."
    )
    login_cooldown_seconds: float = Field(
        default=3600.0,
        description=(
            "After Garmin rate-limits (HTTP 429) a fresh credential login, block all "
            "further login attempts for this many seconds. Garmin's SSO login endpoint "
            "is IP-rate-limited independently of the account, and repeated immediate "
            "retries risk extending the block — a persisted cooldown prevents scripts "
            "or notebook re-runs from hammering it."
        ),
    )


class Settings(BaseSettings):
    """Root application settings, populated from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    user_id: UserId = Field(
        ...,
        description=(
            "Stable identifier for the athlete this local instance serves. Required (not "
            "defaulted) so every ledger entry, file path, and partition key is explicitly "
            "user-scoped from day one, even though today's deployment model is single-user. "
            "This is Strider's own id, never a vendor account id — see `core.schemas.User` "
            "and `core.schemas.GarminAccountLink` for how vendor accounts reference it."
        ),
    )
    garmin: GarminSettings
    data: DataPathSettings = Field(default_factory=DataPathSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)

    def model_post_init(self, __context: Any) -> None:
        if self.garmin.tokenstore_path is None:
            self.garmin.tokenstore_path = self.data.root_dir / "auth" / self.user_id / "garmin"


@lru_cache
def get_settings() -> Settings:
    """Return a cached, process-wide `Settings` instance.

    Cached because `Settings()` re-reads and re-validates the environment on
    every call; call `get_settings.cache_clear()` in tests that need to
    re-read a modified environment.
    """
    return Settings()
