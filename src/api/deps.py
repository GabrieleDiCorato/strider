from functools import lru_cache
from typing import Annotated, Generator
from fastapi import Depends

from src.core.config import Settings, get_settings
from src.agent.coach import Coach
from src.agent.journal import JournalStore
from src.agent.memory import MemoryStore
from src.agent.insights_log import InsightsLog
from src.agent.tools import ToolContext
from src.agent.llm import AdkNarrativeGenerator
from src.ingestion.ledger import SyncLedger
from src.analytics.queries import get_connection

@lru_cache
def get_insights_log() -> InsightsLog:
    return InsightsLog()

@lru_cache
def get_journal_store() -> JournalStore:
    return JournalStore()

@lru_cache
def get_memory_store() -> MemoryStore:
    return MemoryStore()

@lru_cache
def get_sync_ledger() -> SyncLedger:
    settings = get_settings()
    return SyncLedger(settings.data.ledger_db_path)

def get_tool_context(
    settings: Annotated[Settings, Depends(get_settings)],
    journal: Annotated[JournalStore, Depends(get_journal_store)],
    memory: Annotated[MemoryStore, Depends(get_memory_store)],
    ledger: Annotated[SyncLedger, Depends(get_sync_ledger)],
) -> Generator[ToolContext, None, None]:
    conn = get_connection(
        db_path=settings.data.gold_db_path,
        silver_dir=settings.data.silver_dir
    )
    try:
        yield ToolContext(
            user_id=settings.user_id,
            gold=conn,
            journal=journal,
            memory=memory,
            ledger=ledger
        )
    finally:
        conn.close()

def get_coach(
    settings: Annotated[Settings, Depends(get_settings)],
    tool_context: Annotated[ToolContext, Depends(get_tool_context)],
    insights: Annotated[InsightsLog, Depends(get_insights_log)]
) -> Coach:
    generator = AdkNarrativeGenerator(settings.llm)
    return Coach(
        settings=settings,
        generator=generator,
        tool_context=tool_context,
        insights=insights
    )
