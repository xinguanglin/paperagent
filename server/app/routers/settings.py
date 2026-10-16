from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import deps
from app.database import get_session_factory
from app.models import Setting
from app.scheduler import reschedule
from app.llm_client import LlmClient

router = APIRouter(prefix="/api/settings", tags=["settings"])

SUPPORTED_SOURCES = ("arxiv", "openalex")


class SettingsBody(BaseModel):
    categories: str = Field(min_length=3, max_length=255)
    keywords: list[dict] = Field(default_factory=list)
    sources: list[str] = Field(min_length=1, max_length=8)
    top_n: int = Field(ge=1, le=50)
    cron_expr: str = Field(min_length=7, max_length=32)
    chat_model: str = Field(default="", max_length=64)  # 空 = 回退 config.yaml 的 chat_model
    # None = 客户端未传（旧前端兼容），保持库中值不回退；空串 = 关闭识图回退 config 默认
    vision_model: str | None = Field(default=None, max_length=64)


def _to_dict(setting: Setting) -> dict:
    # sources 为空时的回退与 pipeline 实际抓取行为一致（由 config.source 推导），
    # 避免设置页显示双源、用户原样保存后把单源部署静默改成双源
    fallback = ["arxiv", "openalex"]
    if deps.config is not None:
        fallback = {"arxiv": ["arxiv"], "openalex": ["openalex"]}.get(
            deps.config.source, ["arxiv", "openalex"]
        )
    return {
        "categories": setting.categories,
        "keywords": setting.keywords or [],
        "sources": setting.sources or fallback,
        "top_n": setting.top_n,
        "cron_expr": setting.cron_expr,
        "chat_model": setting.chat_model,
        "vision_model": setting.vision_model or "",
    }


@router.get("")
def get_settings():
    with get_session_factory()() as session:  # noqa: SLF001
        setting = session.get(Setting, 1)
        if setting is None:
            raise HTTPException(500, "settings 未初始化")
        return _to_dict(setting)


@router.put("")
def update_settings(body: SettingsBody):
    # cron 先校验后入库：非法表达式一旦落库，服务重启时 build_scheduler 会直接失败
    try:
        CronTrigger.from_crontab(body.cron_expr)
    except ValueError as exc:
        raise HTTPException(400, f"cron 表达式非法：{exc}") from exc

    unknown = [s for s in body.sources if s not in SUPPORTED_SOURCES]
    if unknown:
        raise HTTPException(400, f"不支持的论文来源：{'、'.join(unknown)}（可选：{'、'.join(SUPPORTED_SOURCES)}）")

    with get_session_factory()() as session:  # noqa: SLF001
        setting = session.get(Setting, 1)
        if setting is None:
            raise HTTPException(500, "settings 未初始化")
        setting.categories = body.categories
        setting.keywords = [
            {"word": str(kw.get("word", "")).strip(), "weight": int(kw.get("weight", 1))}
            for kw in body.keywords
            if str(kw.get("word", "")).strip()
        ]
        setting.sources = body.sources
        setting.top_n = body.top_n
        setting.chat_model = body.chat_model or ""
        if body.vision_model is not None:
            setting.vision_model = body.vision_model or None
        cron_changed = setting.cron_expr != body.cron_expr
        setting.cron_expr = body.cron_expr
        session.commit()

    if cron_changed and deps.scheduler is not None:
        reschedule(deps.scheduler, body.cron_expr)

    # 模型档位热更新（无需重启）：路由与批处理管线共用新客户端
    if deps.config is not None and deps.config.api_key:
        vision_model = setting.vision_model or deps.config.vision_model or None
        deps.llm = LlmClient(
            deps.config.api_key,
            body.chat_model,
            base_url=(deps.config.base_url or None),
            vision_model=vision_model,
            chat_thinking=deps.config.chat_thinking,
            embedding_model=deps.config.embedding_model,
            embedding_dims=deps.config.embedding_dims,
        )
        if deps.pipeline is not None:
            deps.pipeline.update_llm(deps.llm)
    return {"saved": True}
