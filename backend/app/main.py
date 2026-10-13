from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .config import get_settings
from .db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    get_settings().var_path.mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Inventory Rescue", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"], allow_credentials=False)
    app.include_router(router)
    return app


app = create_app()
