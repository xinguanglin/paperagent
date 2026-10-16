"""OpenAlex 数据源：arXiv API 不可达时的降级源。

只取 primary_location 为 arXiv 仓库（S4306400194）的作品，内容边界仍是 arXiv 论文。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.arxiv_client import RawPaper
from app.net import http_get

OPENALEX_API = "https://api.openalex.org/works"
ARXIV_SOURCE_ID = "S4306400194"

ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/([^v?#]+)")


def invert_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for idx in indexes:
            positions.append((idx, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def parse_works(payload: dict) -> list[RawPaper]:
    papers: list[RawPaper] = []
    for work in payload.get("results", []):
        location = (work.get("primary_location") or {})
        landing = location.get("landing_page_url") or ""
        m = ARXIV_ID_RE.search(landing)
        if not m:
            continue
        arxiv_id = m.group(1)
        published = work.get("publication_date") or ""
        pub_dt = None
        if published:
            try:
                pub_dt = datetime.strptime(published, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).replace(tzinfo=None)
            except ValueError:
                pass
        concepts = [
            c.get("display_name", "")
            for c in sorted(
                work.get("concepts", []),
                key=lambda c: c.get("score", 0),
                reverse=True,
            )[:3]
        ]
        title = (work.get("title") or "").strip().rstrip(".")
        papers.append(
            RawPaper(
                arxiv_id=arxiv_id,
                version="v1",
                title=title or arxiv_id,
                authors=[
                    a.get("author", {}).get("display_name", "")
                    for a in work.get("authorships", [])[:20]
                    if a.get("author", {}).get("display_name")
                ],
                abstract=invert_abstract(work.get("abstract_inverted_index")),
                categories=[c for c in concepts if c],
                pdf_url=location.get("pdf_url"),
                html_url=f"https://arxiv.org/html/{arxiv_id}",
                published_at=pub_dt,
            )
        )
    return papers


# OpenAlex 概念名与 arXiv 分类的粗映射（client 侧过滤，保持内容边界接近 cs.* 分类）
CATEGORY_CONCEPTS = {
    "cs.AI": "artificial intelligence",
    "cs.CL": "natural language processing",
    "cs.LG": "machine learning",
    "cs.CV": "computer vision",
    "cs.IR": "information retrieval",
    "cs.MA": "multi-agent",
    "cs.SE": "software engineering",
    "cs.RO": "robotics",
    "cs.SD": "speech recognition",
}


def _matches_categories(work_categories: list[str], requested: list[str]) -> bool:
    if not requested:
        return True
    wanted = {CATEGORY_CONCEPTS.get(cat, cat.lower().replace("cs.", "")) for cat in requested}
    lowered = " | ".join(work_categories).lower()
    return any(w in lowered for w in wanted)


OA_SORT = {
    "latest": "publication_date:desc",
    "citations": "cited_by_count:desc",
    "relevance": "relevance_score:desc",
}


def build_intent_params(intent: dict, max_results: int = 10, sort: str = "relevance") -> dict:
    """结构化意图 -> OpenAlex 检索参数（纯函数，便于测试）。"""
    filters = [f"primary_location.source.id:{ARXIV_SOURCE_ID}"]
    for author in intent.get("authors") or []:
        filters.append(f"raw_author_name.search:{author}")
    if intent.get("from_date"):
        filters.append(f"from_publication_date:{intent['from_date']}")
    if intent.get("to_date"):
        filters.append(f"to_publication_date:{intent['to_date']}")
    params = {
        "filter": ",".join(filters),
        "sort": OA_SORT.get(sort, "relevance_score:desc"),
        "per-page": str(min(max_results, 50)),
        "mailto": "paper-agent-user@example.com",
    }
    keywords = intent.get("keywords") or []
    if keywords:
        params["search"] = " ".join(keywords)
    return params


def search_intent(
    intent: dict,
    max_results: int = 10,
    sort: str = "relevance",
    timeout: float = 30.0,
    retries: int = 2,
) -> list[RawPaper]:
    """按结构化意图检索 OpenAlex（作者/日期/被引排序的完整支持）。"""
    return _fetch_with_retries(build_intent_params(intent, max_results, sort), retries)


def search_relevant(
    query: str,
    max_results: int = 10,
    sort: str = "relevance",
    timeout: float = 30.0,
    retries: int = 2,
) -> list[RawPaper]:
    """按语义化查询（英文）在线检索 OpenAlex（限定 arXiv 仓库作品），在线发现的降级源。"""
    params = {
        "search": query,
        "filter": f"primary_location.source.id:{ARXIV_SOURCE_ID}",
        "sort": "publication_date:desc" if sort == "latest" else "relevance_score:desc",
        "per-page": str(min(max_results, 50)),
        "mailto": "paper-agent-user@example.com",
    }
    return _fetch_with_retries(params, retries)


def _fetch_with_retries(params: dict, retries: int = 2) -> list[RawPaper]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = http_get(OPENALEX_API, params=params)
            resp.raise_for_status()
            return parse_works(resp.json())
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"OpenAlex 检索失败（已重试 {retries + 1} 次）：{last_error}")


def fetch_recent(
    categories: list[str],
    lookback_days: int = 1,
    max_results: int = 100,
    timeout: float = 30.0,
    retries: int = 2,
) -> list[RawPaper]:
    """按 arXiv 源 + 日期窗口取最新作品，再用调用方的关键词打分排序。"""
    today = datetime.now(timezone.utc).date()
    from_date = today - timedelta(days=lookback_days)
    params = {
        "filter": (
            f"primary_location.source.id:{ARXIV_SOURCE_ID},"
            f"from_publication_date:{from_date.isoformat()},"
            f"to_publication_date:{today.isoformat()}"
        ),
        "sort": "publication_date:desc",
        "per-page": str(min(max_results, 200)),
        "mailto": "paper-agent-user@example.com",
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = http_get(OPENALEX_API, params=params)
            resp.raise_for_status()
            papers = parse_works(resp.json())
            return [p for p in papers if _matches_categories(p.categories, categories)]
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"OpenAlex API 请求失败（已重试 {retries + 1} 次）：{last_error}")
