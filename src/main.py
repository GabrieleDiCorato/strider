from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.core.logging_config import setup_logging
from src.api.sync import router as sync_router
from src.api.actions import router as actions_router
from src.api.journal import router as journal_router
from src.ui.router import router as ui_router

# Initialize application logging
setup_logging()

app = FastAPI(title="Strider")

# Mount static files
app.mount("/static", StaticFiles(directory="src/ui/static"), name="static")

# Include API routers
app.include_router(sync_router)
app.include_router(actions_router)
app.include_router(journal_router)

# Include UI router
app.include_router(ui_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
