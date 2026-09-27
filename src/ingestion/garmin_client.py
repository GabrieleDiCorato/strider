"""Garmin Connect vendor adapter: safe, idempotent authentication.

Garmin's SSO login endpoint is IP-rate-limited independently of account
standing. `garminconnect.Garmin.login()` already avoids a network credential
login when valid cached tokens are found at `tokenstore` (it loads them
locally and only refreshes if they're expiring soon) — but the *very first*
login, or any run where the cache is missing/rejected, falls through to a
multi-strategy credential flow that issues several requests against Garmin's
SSO service. Hammering that after a 429 risks extending the block.

`login()` in this module adds two guards on top of the library's own
token-cache short-circuiting:
1. **In-process guard** — skips re-authenticating if this `Garmin` instance is
   already authenticated (cheap local check, no network).
2. **Persisted cooldown** — after a `GarminConnectTooManyRequestsError`, writes
   a cooldown marker file next to the token cache. Further calls (even across
   process/kernel restarts) refuse to attempt a fresh login until the cooldown
   elapses, instead of retrying and risking a longer ban.

Data-fetching methods are intentionally NOT wrapped here — this module only
owns authentication. Bronze payload retrieval belongs to future ingestion
pipeline code that consumes an already-authenticated client.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from garminconnect import Garmin, GarminConnectTooManyRequestsError

from src.core.config import Settings
from src.core.schemas import GarminAccountLink

logger = logging.getLogger(__name__)

_COOLDOWN_FILENAME = "login_cooldown.json"


class GarminAuthCooldownError(RuntimeError):
    """Raised when a login attempt is blocked by an active cooldown.

    Set after a prior `GarminConnectTooManyRequestsError` to stop the caller
    (script re-run, notebook cell re-execution, retry loop, ...) from issuing
    another credential login before Garmin's IP rate limit has had time to
    clear.
    """


def _cooldown_marker_path(settings: Settings) -> Path:
    return Path(settings.garmin.tokenstore_path).expanduser() / _COOLDOWN_FILENAME


def _read_cooldown(path: Path) -> tuple[datetime, str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        blocked_until = datetime.fromisoformat(data["blocked_until"])
        reason = data.get("reason", "")
    except (OSError, ValueError, KeyError):
        logger.warning("Ignoring unreadable login cooldown marker at %s", path)
        return None
    return blocked_until, reason


def _write_cooldown(path: Path, blocked_until: datetime, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"blocked_until": blocked_until.isoformat(), "reason": reason}),
        encoding="utf-8",
    )


def clear_login_cooldown(settings: Settings) -> None:
    """Manually remove a persisted cooldown marker (e.g. once you're sure Garmin's block has lifted)."""
    _cooldown_marker_path(settings).unlink(missing_ok=True)


def build_client(settings: Settings) -> Garmin:
    """Construct a `Garmin` client from settings. Does not authenticate."""
    return Garmin(
        email=settings.garmin.email,
        password=settings.garmin.password.get_secret_value(),
        is_cn=settings.garmin.is_cn,
        retry_attempts=settings.rate_limit.retry_attempts,
        prompt_mfa=lambda: input("Enter Garmin MFA code: "),
    )


def login(client: Garmin, settings: Settings, *, force: bool = False) -> Garmin:
    """Idempotent, cooldown-guarded login.

    Safe to call on every run/cell-execution: it no-ops if `client` is already
    authenticated, refuses to attempt a fresh login while a prior 429 cooldown
    is active, and persists a new cooldown if Garmin rate-limits this attempt.

    :param force: Bypass both the in-process and cooldown guards. Only use
        this once you have manually confirmed Garmin's rate limit has cleared.
    :raises GarminAuthCooldownError: A cooldown from a prior 429 is still active.
    :raises GarminConnectTooManyRequestsError: Garmin rate-limited this attempt.
    """
    if not force and client.client.is_authenticated:
        logger.info("Already authenticated in this session; skipping login().")
        return client

    cooldown_path = _cooldown_marker_path(settings)
    if not force:
        cooldown = _read_cooldown(cooldown_path)
        if cooldown is not None:
            blocked_until, reason = cooldown
            now = datetime.now(timezone.utc)
            if now < blocked_until:
                remaining = (blocked_until - now).total_seconds()
                raise GarminAuthCooldownError(
                    f"Login is on cooldown for another {remaining:.0f}s ({reason}). "
                    f"Cooldown recorded at {cooldown_path}. Pass force=True once you've "
                    "confirmed Garmin's rate limit has cleared, or delete that file."
                )

    settings.garmin.tokenstore_path.mkdir(parents=True, exist_ok=True)
    try:
        client.login(str(settings.garmin.tokenstore_path))
    except GarminConnectTooManyRequestsError as e:
        blocked_until = datetime.now(timezone.utc).timestamp() + settings.rate_limit.login_cooldown_seconds
        _write_cooldown(
            cooldown_path,
            datetime.fromtimestamp(blocked_until, tz=timezone.utc),
            reason=str(e),
        )
        logger.error(
            "Garmin rate-limited this login attempt; cooldown set for %.0fs at %s.",
            settings.rate_limit.login_cooldown_seconds,
            cooldown_path,
        )
        raise
    else:
        # A successful login means any earlier cooldown no longer applies.
        cooldown_path.unlink(missing_ok=True)

    return client


def build_account_link(settings: Settings) -> GarminAccountLink:
    """Build the `GarminAccountLink` for the configured user directly from settings.

    No Garmin API call is needed: unlike a vendor-internal id, everything this link
    records (email, token store location) is already known from configuration. Persist
    the result via `ingestion.accounts.AccountStore.link_garmin_account()`.
    """
    return GarminAccountLink(
        user_id=settings.user_id,
        email=settings.garmin.email,
        tokenstore_path=str(settings.garmin.tokenstore_path),
        linked_at=datetime.now(timezone.utc),
    )
