from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app import deps
from app.config import load_config
from app.database import get_session_factory, init_db, init_engine
from app.logs import setup_logging
from app.models import Digest, Paper, Run, Setting
from app.net import configure_http
from app.pipeline import Pipeline, RunState
from app.scheduler import build_scheduler
from app.llm_client import LlmClient

setup_logging()
logger = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    deps.config = cfg
    configure_http(cfg.proxy)
    engine = init_engine(cfg)
    init_db(engine)

    with get_session_factory()() as session:
        setting = session.get(Setting, 1)
        # 模型档位：settings 覆盖 config 默认（chat_model 为空时回退 config）
        chat_model = (setting.chat_model if setting else "") or cfg.chat_model
        cron_expr = setting.cron_expr if setting else "0 8 * * *"
        vision_model = (setting.vision_model if setting else None) or cfg.vision_model

    deps.llm = (
        LlmClient(
            cfg.api_key,
            chat_model,
            base_url=cfg.base_url or None,
            vision_model=vision_model or None,
            chat_thinking=cfg.chat_thinking,
            embedding_model=cfg.embedding_model,
            embedding_dims=cfg.embedding_dims,
        )
        if cfg.api_key
        else None
    )
    if deps.llm is None:
        logger.warning("未配置 LLM API Key：论文将入库但提炼会失败，语义检索不可用")

    deps.run_state = RunState()
    deps.pipeline = Pipeline(
        get_session_factory(),
        deps.llm,
        deps.run_state,
        source=cfg.source,
        vision_max_figures=cfg.vision_max_figures,
        figures_dir=str(Path(cfg.upload_dir) / "figures"),
    )
    deps.scheduler = build_scheduler(deps.pipeline, cron_expr)
    deps.scheduler.start()
    logger.info("paper-agent 启动完成，端口 %s，定时 %s", cfg.server_port, cron_expr)
    yield
    deps.scheduler.shutdown(wait=False)


app = FastAPI(title="paper-agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import discover, logs, papers, runs, search, settings  # noqa: E402

app.include_router(papers.router)
app.include_router(search.router)
app.include_router(runs.router)
app.include_router(settings.router)
app.include_router(logs.router)
app.include_router(discover.router)


@app.get("/api/health")
def health():
    job = deps.scheduler.get_job("daily") if deps.scheduler else None
    return {
        "status": "ok",
        "key_configured": deps.llm is not None,
        "chat_model": deps.llm.model if deps.llm else None,
        "next_run_at": str(job.next_run_time) if job and job.next_run_time else None,
    }


@app.get("/api/stats")
def stats():
    factory = get_session_factory()
    today = date.today()
    month_start = today.replace(day=1)
    with factory() as session:
        # 今日新增按入库时间统计（created_at）；published_at 是 arXiv 发布日，
        # 今天抓到的论文通常是昨天发布的，按它统计会恒为 0
        today_count = (
            session.execute(
                select(func.count(Paper.id)).where(func.date(Paper.created_at) == today)
            ).scalar_one()
        )
        total_count = session.execute(select(func.count(Paper.id))).scalar_one()
        month_tokens = (
            session.execute(
                select(func.coalesce(func.sum(Digest.tokens_used), 0)).where(
                    Digest.digested_at >= datetime(month_start.year, month_start.month, month_start.day)
                )
            ).scalar_one()
        )
        last_run = session.execute(select(Run).order_by(Run.id.desc()).limit(1)).scalar_one_or_none()
    job = deps.scheduler.get_job("daily") if deps.scheduler else None
    return {
        "today_count": today_count,
        "total_count": total_count,
        "month_tokens": month_tokens,
        "next_run_at": str(job.next_run_time) if job and job.next_run_time else None,
        "last_run": {
            "id": last_run.id,
            "status": last_run.status,
            "fetched_count": last_run.fetched_count,
            "succeeded_count": last_run.succeeded_count,
            "failed_count": last_run.failed_count,
            "total_tokens": last_run.total_tokens,
            "started_at": last_run.started_at.strftime("%Y-%m-%d %H:%M") if last_run.started_at else None,
        }
        if last_run
        else None,
    }


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="web-assets")

    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("assets/"):
            from fastapi import HTTPException

            raise HTTPException(404, "Not Found")
        if full_path:
            candidate = (WEB_DIST / full_path).resolve()
            # 防路径穿越：解析后的真实路径必须仍位于 dist 内
            if candidate.is_file() and candidate.is_relative_to(WEB_DIST.resolve()):
                return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")
