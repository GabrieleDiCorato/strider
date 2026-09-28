import hashlib
import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from src.core.config import get_settings
from src.core.duckdb_utils import from_utc_naive, to_utc_naive


class ActionStatus(str, Enum):
    SUCCESS = "success"
    NO_DATA = "no_data"
    FAILED = "failed"


class Metric(BaseModel):
    label: str
    value: str
    unit: str | None = None


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    action_id: str
    action_version: int
    status: ActionStatus
    metrics: list[Metric] = []
    narrative: dict | None = None
    message: str | None = None
    from_cache: bool = False
    created_at: datetime


class InsightRun(BaseModel):
    run_id: str
    user_id: str
    action_id: str
    action_version: int
    params_json: str
    cache_key: str
    status: ActionStatus
    context_json: str
    output_json: str | None
    error: str | None
    model: str
    created_at: datetime


class InsightsLog:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path) if db_path is not None else str(get_settings().data.memory_db_path)
        self._init_db()

    def _init_db(self):
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS insights_log (
                    run_id VARCHAR PRIMARY KEY,
                    user_id VARCHAR NOT NULL,
                    action_id VARCHAR,
                    action_version INTEGER,
                    params_json VARCHAR,
                    cache_key VARCHAR,
                    status VARCHAR,
                    context_json VARCHAR,
                    output_json VARCHAR,
                    error VARCHAR,
                    model VARCHAR,
                    created_at TIMESTAMP
                )
            """)

    def _compute_cache_key(self, action_id: str, action_version: int, model: str, params_json: str, context_json: str) -> str:
        s = f"{action_id}|{action_version}|{model}|{params_json}|{context_json}"
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def record(self, user_id: str, action_id: str, action_version: int, model: str, params_json: str, context_json: str, result: ActionResult, error: str | None = None) -> str:
        run_id = result.run_id
        cache_key = self._compute_cache_key(action_id, action_version, model, params_json, context_json)
        out_json = result.model_dump_json() if result.status == ActionStatus.SUCCESS else None
        
        with duckdb.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO insights_log (
                    run_id, user_id, action_id, action_version, params_json,
                    cache_key, status, context_json, output_json, error, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                run_id, user_id, action_id, action_version, params_json,
                cache_key, result.status.value, context_json, out_json, error, model,
                to_utc_naive(result.created_at)
            ])
        return cache_key

    def find_cached(self, user_id: str, cache_key: str) -> ActionResult | None:
        with duckdb.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT output_json FROM insights_log
                WHERE user_id = ? AND cache_key = ? AND status = 'success'
                ORDER BY created_at DESC LIMIT 1
            """, [user_id, cache_key]).fetchone()
            
            if row and row[0]:
                return ActionResult.model_validate_json(row[0])
        return None

    def list_runs(self, user_id: str, limit: int = 50) -> list[InsightRun]:
        with duckdb.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT run_id, user_id, action_id, action_version, params_json,
                       cache_key, status, context_json, output_json, error, model, created_at
                FROM insights_log
                WHERE user_id = ?
                ORDER BY created_at DESC LIMIT ?
            """, [user_id, limit]).fetchall()
            
            res = []
            for r in rows:
                res.append(InsightRun(
                    run_id=r[0], user_id=r[1], action_id=r[2], action_version=r[3],
                    params_json=r[4], cache_key=r[5], status=ActionStatus(r[6]),
                    context_json=r[7], output_json=r[8], error=r[9], model=r[10],
                    created_at=from_utc_naive(r[11])
                ))
            return res

