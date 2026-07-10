"""FastAPI entry point — RFP GraphRAG cloud app.

Serves a single page with three tabs (Data Prep, Community Summariser, Query
Agent) and mounts the per-step API routers. Run with:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.routers import community, data_prep, query

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="RFP GraphRAG", version="0.1.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.include_router(data_prep.router)
app.include_router(community.router)
app.include_router(query.router)


def _asset_version() -> int:
    """Cache-busting version — newest mtime of any static asset, so browsers
    pick up CSS/JS changes on every deploy without a hard refresh."""
    try:
        return int(max(p.stat().st_mtime for p in STATIC_DIR.rglob("*") if p.is_file()))
    except ValueError:
        return 0


@app.on_event("startup")
async def _startup():
    get_settings().ensure_dirs()


@app.get("/health")
async def health():
    settings = get_settings()
    return JSONResponse(
        {
            "status": "ok",
            "model_extract": settings.model_extract,
            "google_key_set": bool(settings.google_api_key),
            "data_dir": str(settings.data_dir),
        }
    )


@app.get("/")
async def index(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "model_label": settings.active_model_label,
            "provider": settings.llm_provider,
            "api_key_set": settings.api_key_set,
            "blob_mode": settings.blob_mode,
            "blob_container": settings.azure_storage_container_name,
            "asset_version": _asset_version(),
        },
    )
