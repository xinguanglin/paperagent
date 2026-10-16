from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import contains_eager

from app import deps
from app.database import get_session_factory
from app.models import Digest, Paper
from app.upload_parser import parse_paper_file, sanitize_filename

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/papers", tags=["papers"])

CHAT_HISTORY_LIMIT = 12  # 客户端携带的多轮历史上限，防 token 失控
CHAT_MESSAGE_MAX = 4000  # 单条消息字符上限
MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 上传大小上限 30MB

CHAT_SYSTEM = (
    "你是论文解读助手，正在与读者就一篇具体论文进行多轮对话。"
    "下方提供该论文的摘要与结构化解读，作为你回答的依据。回答要求："
    "1) 忠实于提供的材料与论文原文，不确定就明说，绝不编造；"
    "2) 读者说「没看懂」时，换更基础的语言、配生活化类比再讲一遍，并落到论文的具体机制；"
    "3) 材料不足以回答时，明确说明，并建议查看论文原文的哪个章节；"
    "4) 用中文回答，公式与术语需解释。\n\n"
    "===== 论文材料 =====\n"
)


def _date_str(dt) -> str | None:
    return dt.strftime("%Y-%m-%d") if dt else None


def paper_summary(paper: Paper) -> dict:
    digest = paper.digest
    return {
        "id": paper.id,
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "one_liner": digest.one_liner if digest else None,
        "difficulty": digest.difficulty if digest else None,
        "source_type": digest.source_type if digest else None,
        "model": digest.model if digest else None,
        "digested_at": (
            digest.digested_at.strftime("%m-%d %H:%M") if digest and digest.digested_at else None
        ),
        "status": paper.status,
        "fail_reason": paper.fail_reason,
        "score": paper.score,
        "published_at": _date_str(paper.published_at),
        "categories": (paper.categories or "").split(","),
        "origin": paper.origin or "fetch",
        "local_file": paper.local_file,
    }


@router.post("/upload")
async def upload_paper(background: BackgroundTasks, file: UploadFile = File(...)):
    """手工上传单篇论文（PDF/DOC/DOCX）：校验 → 落盘 → 解析 → 入库 → 后台提炼。

    忙时（批次/另一单篇在跑）不拒绝：论文入库为待提炼，由看板「重新提炼」补跑。
    """
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 30MB 上限")
    if not content:
        raise HTTPException(400, "上传的文件为空")

    filename = file.filename or "paper"
    try:
        _, text = parse_paper_file(content, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # 落盘：upload_dir/{日期}/{时间戳}_{随机}_{清洗后文件名}——路径全由服务端生成；
    # 相对目录一律锚定项目根（不随后端进程的启动目录漂移），库里只存相对路径
    upload_dir = Path(deps.config.upload_dir) if deps.config is not None else Path("uploads")
    rel_path = (
        upload_dir
        / datetime.now().strftime("%Y%m%d")
        / f"{datetime.now().strftime('%H%M%S')}_{uuid4().hex[:6]}_{sanitize_filename(filename)}"
    )
    abs_path = rel_path if rel_path.is_absolute() else Path(__file__).resolve().parents[3] / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)

    pipeline = deps.pipeline
    if pipeline is None:
        raise HTTPException(503, "服务尚未初始化完成")
    paper_id, title = pipeline.ingest_upload(filename, text, rel_path.as_posix())

    # 首轮提炼复用单篇重跑的后台任务与互斥语义；被互斥跳过时论文保持 fetched
    state = deps.run_state
    busy = state is not None and (state.running or state.single_active)
    background.add_task(_run_redigest, paper_id)
    return {"id": paper_id, "title": title, "started": not busy}


@router.get("")
def list_papers(date: str | None = None, status: str | None = None, page: int = 1, limit: int = 20):
    """看板论文列表：按提炼时间降序（未提炼的排最后），分页返回。"""
    page = max(1, page)
    limit = max(1, min(limit, 100))
    with get_session_factory()() as session:  # noqa: SLF001
        # contains_eager：把 join 出的 digest 一次带回来，避免每篇 paper_summary 再懒加载一次
        query = select(Paper).outerjoin(Paper.digest).options(contains_eager(Paper.digest))
        if date:
            query = query.where(func.date(Paper.published_at) == date)
        if status:
            query = query.where(Paper.status == status)
        total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
        papers = (
            session.execute(
                query.order_by(
                    func.isnull(Digest.digested_at),  # MySQL 无 NULLS LAST，isnull=0 的（已提炼）排前
                    Digest.digested_at.desc(),
                    Paper.id.desc(),
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
            .scalars()
            .unique()
            .all()
        )
        return {
            "items": [paper_summary(p) for p in papers],
            "total": total,
            "page": page,
            "page_size": limit,
            "pages": (total + limit - 1) // limit,
        }


@router.get("/{paper_id}")
def get_paper(paper_id: int):
    with get_session_factory()() as session:  # noqa: SLF001
        paper = session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(404, "论文不存在")
        digest = paper.digest
        return {
            **paper_summary(paper),
            "authors": paper.authors,
            "abstract": paper.abstract,
            "pdf_url": paper.pdf_url,
            "html_url": paper.html_url,
            "figures": paper.figures,
            "outline": digest.outline if digest else None,
            "key_points": digest.key_points if digest else None,
            "implementation": digest.implementation if digest else None,
            "diagrams": digest.diagrams if digest else None,
            "principle": digest.principle if digest else None,
            "limitations": digest.limitations if digest else None,
            "truncated": bool(digest.truncated) if digest else False,
            "model": digest.model if digest else None,
            "tokens_used": digest.tokens_used if digest else 0,
        }


@router.post("/{paper_id}/redigest")
def redigest(paper_id: int, background: BackgroundTasks):
    # 不做占用拦截：管线内部会把互斥冲突的请求排队，锁释放后自动执行
    background.add_task(_run_redigest, paper_id)
    return {"started": True}


def _run_redigest(paper_id: int) -> None:
    import logging

    from app.pipeline import PipelineBusy

    try:
        queued = not deps.pipeline.redigest(paper_id)
        if queued:
            logging.getLogger(__name__).info("论文 %s 的提炼已排队，锁释放后自动执行", paper_id)
    except PipelineBusy:
        logging.getLogger(__name__).warning("论文 %s 的重跑被跳过", paper_id)
    except Exception:
        logging.getLogger(__name__).exception("单篇重跑失败")


class ChatBody(BaseModel):
    messages: list[dict] = Field(min_length=1, max_length=30)


def _clamp_history(messages: list[dict]) -> list[dict]:
    """只保留合法的 user/assistant 轮次，限制条数与单条长度。"""
    out: list[dict] = []
    for m in messages[-CHAT_HISTORY_LIMIT:]:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(m.get("content") or "")[:CHAT_MESSAGE_MAX]
        if content.strip():
            out.append({"role": role, "content": content})
    return out


def _paper_context(paper: Paper) -> str:
    """把论文摘要与全部提炼内容拼成对话上下文。"""
    d = paper.digest
    parts = [
        f"论文标题：{paper.title}",
        f"作者：{paper.authors or '未知'}",
        f"摘要：{paper.abstract or '（无）'}",
    ]
    if d is not None:
        parts += [
            f"一句话总结：{d.one_liner or '（无）'}",
            f"论文提纲：{json.dumps(d.outline, ensure_ascii=False) if d.outline else '（无）'}",
            f"核心要点：{json.dumps(d.key_points, ensure_ascii=False) if d.key_points else '（无）'}",
            f"实现过程：{d.implementation or '（无）'}",
            f"原理拆解：{d.principle or '（无）'}",
            f"局限：{d.limitations or '（无）'}",
        ]
        if d.diagrams:
            # mermaid 源码一并注入，对话可回答"图中第二步是什么"这类问题
            for i, g in enumerate(d.diagrams, 1):
                kind = "流程/框架图" if g.get("type") == "flow" else "系统架构图"
                parts.append(f"图示{i}（{kind}）{g.get('caption', '')}：\n{g.get('mermaid', '')}")
    return "\n\n".join(parts)


@router.post("/{paper_id}/chat")
def chat_with_paper(paper_id: int, body: ChatBody):
    """就某篇论文与 LLM 多轮对话（SSE 流式返回）。会话历史由客户端携带。

    配置 memoryhub_url 后带跨会话记忆：注入历史记忆（begin），对话结束写回（end）。
    """
    llm = deps.llm
    if llm is None:
        raise HTTPException(503, "未配置 LLM API Key，对话不可用")
    with get_session_factory()() as session:  # noqa: SLF001
        paper = session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(404, "论文不存在")
        context = _paper_context(paper)  # 在 session 内取完关联数据，避免 detach 后懒加载
    history = _clamp_history(body.messages)
    if not history:
        raise HTTPException(400, "对话消息为空")

    from app.memory_bridge import get_memory_bridge

    memory = get_memory_bridge(deps.config) if deps.config is not None else None
    latest_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    memory_context = memory.begin(latest_user) if memory else ""
    system = CHAT_SYSTEM + context + memory_context

    reply_parts: list[str] = []

    def gen():
        try:
            for kind, delta in llm.chat_stream(system, history):
                if kind == "content":
                    reply_parts.append(delta)
                yield f"data: {json.dumps({'kind': kind, 'delta': delta}, ensure_ascii=False)}\n\n"
        except Exception:
            # 细节只进服务端日志（可在抓取日志页排查）；SSE 只给前端通用文案，
            # 不外泄 SDK/上游异常原文（可能含内部端点、错误响应体）
            logger.exception("论文 %s 对话流出错", paper_id)
            yield (
                "data: "
                + json.dumps({"error": "对话服务暂时不可用，请稍后重试"}, ensure_ascii=False)
                + "\n\n"
            )
        finally:
            if memory and latest_user and reply_parts:
                threading.Thread(
                    target=memory.end,
                    args=([{"role": "user", "content": latest_user},
                           {"role": "assistant", "content": "".join(reply_parts)[:8000]}],),
                    daemon=True,
                ).start()
        yield 'data: {"done": true}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream")
