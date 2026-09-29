import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.core.config import Settings, get_settings
from src.core.schemas import EntityType
from src.ingestion.garmin.garmin_connector import GarminConnector
from src.processing.pipeline import run_pipeline
from src.processing.writer import ParquetWriter
from src.processing.extractors.garmin_adapter import (
    ActivityExtractor,
    DailySummaryExtractor,
    WorkoutExtractor,
    CalendarExtractor,
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="src/ui/templates")

router = APIRouter(prefix="/api/sync", tags=["sync"])

@router.post("")
def sync_garmin_data(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)]
):
    is_htmx = request.headers.get("HX-Request") == "true"
    logger.info("Starting Garmin sync for user_id=%s (is_htmx=%s)", settings.user_id, is_htmx)

    try:
        connector = GarminConnector(settings)
        extractors = {
            EntityType.ACTIVITY: ActivityExtractor(),
            EntityType.DAILY_SUMMARY: DailySummaryExtractor(),
            EntityType.WORKOUT_DEFINITION: WorkoutExtractor(),
            EntityType.WORKOUT_CALENDAR: CalendarExtractor(),
        }
        writer = ParquetWriter(settings.data.silver_dir)
        
        end = date.today()
        start = end - timedelta(days=30)
        
        counts = run_pipeline(
            connector=connector,
            extractors=extractors,
            writer=writer,
            user_id=settings.user_id,
            start=start,
            end=end
        )
        total_count = sum(counts.values())
        logger.info("Garmin sync completed successfully for user_id=%s: %s records", settings.user_id, total_count)
        
        if is_htmx:
            return templates.TemplateResponse(
                request=request,
                name="partials/sync_banner.html",
                context={
                    "status": "success",
                    "total_count": total_count,
                    "counts": {k.value: v for k, v in counts.items()},
                }
            )
        return {"status": "success", "counts": {k.value: v for k, v in counts.items()}}
    except Exception as e:
        logger.exception("Garmin sync failed for user_id=%s: %s", settings.user_id, e)
        if is_htmx:
            return templates.TemplateResponse(
                request=request,
                name="partials/sync_banner.html",
                context={
                    "status": "error",
                    "error_message": str(e),
                },
                status_code=200,
            )
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})
