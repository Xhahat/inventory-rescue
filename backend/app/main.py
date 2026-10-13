from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.assistant_routes import router as assistant_router
from .api.eval_routes import router as eval_router
from .api.routes import router
from .config import get_settings
from .db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    s = get_settings()
    if s.storage_backend.strip().lower() == "local":
        s.var_path.mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Inventory Rescue", version="0.2.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_methods=["GET", "POST", "PUT"],
                       allow_headers=["Content-Type"], allow_credentials=False)
    app.include_router(router)
    app.include_router(assistant_router)
    app.include_router(eval_router)
    static = Path(s.static_dir)
    if (static / "index.html").is_file():
        # Single-service deployment (Cloud Run): the built React app is served from the same origin as /api.
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app


app = create_app()
