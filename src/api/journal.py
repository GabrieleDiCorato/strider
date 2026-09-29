from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form
from pydantic import BaseModel

from src.core.config import Settings, get_settings
from src.core.schemas import EntrySource, JournalCategory
from src.agent.journal import JournalStore
from src.api.deps import get_journal_store

router = APIRouter(prefix="/api/journal", tags=["journal"])

class ManualLogRequest(BaseModel):
    category: JournalCategory
    text: str
    rating: Optional[int] = None
    related_activity_id: Optional[str] = None

@router.get("")
async def get_journal(
    settings: Annotated[Settings, Depends(get_settings)],
    journal: Annotated[JournalStore, Depends(get_journal_store)],
    limit: int = 50,
    category: Optional[str] = None
):
    categories = None
    if category:
        categories = [JournalCategory(c) for c in category.split(",")]
        
    entries = journal.list_entries(
        settings.user_id,
        limit=limit,
        categories=categories
    )
    return entries

@router.post("")
async def add_manual_log(
    req: ManualLogRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    journal: Annotated[JournalStore, Depends(get_journal_store)]
):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    
    entry = journal.add_entry(
        user_id=settings.user_id,
        entry_date=today,
        source=EntrySource.USER,
        category=req.category,
        text=req.text,
        rating=req.rating,
        related_activity_id=req.related_activity_id
    )
    return entry

class QuickCaptureParseRequest(BaseModel):
    text: str

@router.post("/quick-capture/parse")
async def quick_capture_parse(req: QuickCaptureParseRequest):
    return {
        "text": req.text,
        "parsed": [
            {"item": "Dummy item", "qty": "1"}
        ]
    }

class QuickCaptureConfirmRequest(BaseModel):
    text: str
    
@router.post("/quick-capture/confirm")
async def quick_capture_confirm(
    req: QuickCaptureConfirmRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    journal: Annotated[JournalStore, Depends(get_journal_store)]
):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    entry = journal.add_entry(
        user_id=settings.user_id,
        entry_date=today,
        source=EntrySource.USER,
        category=JournalCategory.NOTE,
        text=req.text
    )
    return entry
