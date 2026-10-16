"""进程级共享对象，由 main.py 的 lifespan 初始化。"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Config
from app.pipeline import Pipeline, RunState
from app.llm_client import LlmClient

config: Config | None = None
llm: LlmClient | None = None
pipeline: Pipeline | None = None
run_state: RunState | None = None
scheduler: BackgroundScheduler | None = None
