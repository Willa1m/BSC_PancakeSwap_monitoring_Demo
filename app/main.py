"""入口：启动 FastAPI + 后台采集线程。运行：uvicorn app.main:app --reload 不要用 --reload（会起两份采集线程）"""
import logging
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pathlib import Path
from . import collector, notifier
from .models import init_db
from .api import router
from .admin import router as admin_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = FastAPI(title="BSC LP Monitor (demo)")
app.include_router(router)
app.include_router(admin_router)
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup():
    init_db()
    collector.start()
    notifier.start()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
