from __future__ import annotations

from fastapi import APIRouter, Query

from app.logs import query_logs

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def list_logs(
    limit: int = 200,
    level: str = "DEBUG",
    keyword: str = Query(default="", max_length=200),
):
    """最近的运行日志（读取日志文件尾部，级别阈值 + 关键词过滤）。"""
    return query_logs(limit=max(1, min(limit, 1000)), level=level, keyword=keyword)
