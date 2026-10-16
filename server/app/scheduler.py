from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.pipeline import PipelineBusy

logger = logging.getLogger(__name__)


def build_scheduler(pipeline, cron_expr: str) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()

    def daily_job() -> None:
        try:
            pipeline.run_batch("scheduled")
        except PipelineBusy:
            logger.warning("定时批次触发时已有任务在运行，跳过")
        except Exception:
            logger.exception("定时批次执行失败")

    scheduler.add_job(daily_job, CronTrigger.from_crontab(cron_expr), id="daily")
    return scheduler


def reschedule(scheduler: BackgroundScheduler, cron_expr: str) -> None:
    """设置页修改 cron 后重排定时任务；表达式非法会抛 ValueError。"""
    trigger = CronTrigger.from_crontab(cron_expr)  # 先校验再重排
    scheduler.reschedule_job("daily", trigger=trigger)
