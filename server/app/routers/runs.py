from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app import deps
from app.database import get_session_factory
from app.models import Run

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
def list_runs(limit: int = 20):
    with get_session_factory()() as session:  # noqa: SLF001
        runs = (
            session.execute(select(Run).order_by(Run.id.desc()).limit(min(limit, 100)))
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "trigger_type": r.trigger_type,
                "status": r.status,
                "fetched_count": r.fetched_count,
                "succeeded_count": r.succeeded_count,
                "failed_count": r.failed_count,
                "total_tokens": r.total_tokens,
                "error_summary": r.error_summary,
                "started_at": r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else None,
                "finished_at": (
                    r.finished_at.strftime("%Y-%m-%d %H:%M") if r.finished_at else None
                ),
            }
            for r in runs
        ]


@router.get("/current")
def current_run():
    assert deps.run_state is not None
    return deps.run_state.to_dict()


@router.post("/trigger")
def trigger_run():
    pipeline = deps.pipeline
    state = deps.run_state
    if state is None or pipeline is None:
        raise HTTPException(503, "服务尚未初始化完成")
    if state.running or state.single_active:
        raise HTTPException(409, "已有任务在运行（批次或单篇重跑），请稍后再试")

    def _run() -> None:
        try:
            pipeline.run_batch("manual")
        except Exception:
            import logging

            logging.getLogger(__name__).exception("手动批次失败")

    threading.Thread(target=_run, daemon=True, name="manual-batch").start()
    return {"started": True}


@router.post("/stop")
def stop_run():
    """请求停止当前批次：已完成并入库的解读保留，未开始的论文跳过（下批自动续跑）。"""
    state = deps.run_state
    if state is None:
        raise HTTPException(503, "服务尚未初始化完成")
    if not state.running:
        raise HTTPException(409, "当前没有正在运行的批次")
    state.request_stop()
    return {"stopped": True}
