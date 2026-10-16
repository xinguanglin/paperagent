from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import deps
from app.database import get_session_factory
from app.models import Paper

router = APIRouter(prefix="/api/discover", tags=["discover"])

ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


class IngestBody(BaseModel):
    arxiv_id: str = Field(min_length=5, max_length=32)


def _to_item(raw) -> dict:
    return {
        "arxiv_id": raw.arxiv_id,
        "title": raw.title,
        "authors": ", ".join(raw.authors[:5]) if raw.authors else None,
        "abstract": raw.abstract,
        "categories": raw.categories,
        "published_at": raw.published_at.strftime("%Y-%m-%d") if raw.published_at else None,
        "abs_url": f"https://arxiv.org/abs/{raw.arxiv_id}",
        "pdf_url": raw.pdf_url,
    }


@router.get("")
def discover(q: str, limit: int = 10, sort: str = "relevance"):
    """意图识别检索：LLM 解析自然语言需求（作者/时间/关键词/排序）→ 构造双源查询。"""
    llm = deps.llm
    if llm is None:
        raise HTTPException(503, "未配置 LLM API Key，在线检索不可用")
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "检索内容为空")
    limit = max(1, min(limit, 50))

    from datetime import date

    from app.llm_client import LlmError

    try:
        intent = llm.parse_query_intent(q, date.today().isoformat())
    except LlmError as exc:
        raise HTTPException(502, str(exc)) from exc
    effective_sort = (
        sort
        if sort in ("relevance", "latest", "citations")
        else (intent.get("sort_hint") or "relevance")
    )

    from app import openalex_client
    from app.arxiv_client import build_intent_query, search_relevant

    arxiv_query = build_intent_query(intent)

    def _arxiv() -> list:
        return search_relevant("", max_results=limit, sort=effective_sort, search_query=arxiv_query)

    def _openalex() -> list:
        return openalex_client.search_intent(intent, max_results=limit, sort=effective_sort)

    # 被引排序只有 OpenAlex 能做 → 首选 OpenAlex；否则 arXiv 优先
    attempts = (_openalex, _arxiv) if effective_sort == "citations" else (_arxiv, _openalex)
    source, raws, errors = None, [], []
    for attempt in attempts:
        try:
            raws = attempt()
        except Exception as exc:
            errors.append(str(exc)[:120])
            raws = []
        if raws:
            source = "openalex" if attempt is _openalex else "arxiv"
            break
    if source is None and errors:
        raise HTTPException(502, f"论文源检索失败：{'；'.join(errors)}")

    items = [_to_item(r) for r in raws]
    if items:
        with get_session_factory()() as session:  # noqa: SLF001
            ids = {i["arxiv_id"] for i in items}
            in_lib = {
                arxiv_id: paper_id
                for paper_id, arxiv_id in session.execute(
                    select(Paper.id, Paper.arxiv_id).where(Paper.arxiv_id.in_(ids))
                ).fetchall()
            }
        for i in items:
            i["in_library"] = i["arxiv_id"] in in_lib
            i["paper_id"] = in_lib.get(i["arxiv_id"])  # 已在库时带内部 ID，前端直达详情页
    return {
        "query": q,
        "intent": intent,
        "arxiv_query": arxiv_query,
        "source": source,
        "sort": effective_sort,
        "results": items,
    }


@router.post("/ingest")
def ingest(body: IngestBody, background: BackgroundTasks):
    """提炼在线检索结果的某篇论文：新论文从 arXiv 抓元数据入库，已有论文直接重跑。

    已入库的论文（重试/重跑）跳过 arXiv 重抓——省一次请求，也让 arXiv 限流（429）时
    「重试提炼」依然可用。
    """
    from app.arxiv_client import fetch_by_ids
    from app.routers.papers import _run_redigest

    arxiv_id = body.arxiv_id.strip()
    if not ARXIV_ID_RE.match(arxiv_id):
        raise HTTPException(400, f"非法 arXiv ID：{arxiv_id}")

    pipeline = deps.pipeline
    if pipeline is None:
        raise HTTPException(503, "服务尚未初始化完成")

    with get_session_factory()() as session:
        existing = session.scalar(select(Paper).where(Paper.arxiv_id == arxiv_id))
    if existing is not None:
        paper_id, created = existing.id, False
    else:
        try:
            raws = fetch_by_ids([arxiv_id])
        except Exception as exc:
            raise HTTPException(502, f"获取论文元数据失败：{exc}") from exc
        target = next((r for r in raws if r.arxiv_id == arxiv_id), None)
        if target is None:
            raise HTTPException(404, f"arXiv 上未找到 {arxiv_id}")
        paper_id, created = pipeline.ingest_discover(target)
    state = deps.run_state
    busy = state is not None and (state.running or state.single_active)
    background.add_task(_run_redigest, paper_id)
    return {"id": paper_id, "created": created, "started": not busy}
