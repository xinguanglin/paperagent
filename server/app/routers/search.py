from __future__ import annotations

import numpy as np
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app import deps
from app.database import get_session_factory
from app.models import Paper
from app.routers.papers import paper_summary

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
def semantic_search(q: str, top_k: int = 10):
    llm = deps.llm
    if llm is None:
        raise HTTPException(400, "未配置 LLM API Key，语义检索不可用。请在 config.yaml 填写 api_key 后重启服务。")
    if not q.strip():
        return []

    try:
        query_vec = np.asarray(llm.embed(q), dtype=np.float32)
    except Exception as exc:
        raise HTTPException(502, f"查询向量化失败：{exc}") from exc

    with get_session_factory()() as session:  # noqa: SLF001
        papers = (
            session.execute(select(Paper).where(Paper.embedding.is_not(None)))
            .scalars()
            .unique()
            .all()
        )
        hits = []
        for paper in papers:
            vec = np.frombuffer(paper.embedding, dtype=np.float32)
            if vec.shape != query_vec.shape:
                continue
            denom = float(np.linalg.norm(query_vec) * np.linalg.norm(vec))
            similarity = float(np.dot(query_vec, vec) / (denom + 1e-10))
            hits.append((similarity, paper))
        hits.sort(key=lambda pair: pair[0], reverse=True)
        return [ {**paper_summary(p), "similarity": round(sim, 4)} for sim, p in hits[: max(1, min(top_k, 50))] ]
