from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

ARXIV_API = "http://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass
class RawPaper:
    arxiv_id: str
    version: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    pdf_url: str | None
    html_url: str | None
    published_at: datetime | None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_atom(xml_text: str) -> list[RawPaper]:
    root = ElementTree.fromstring(xml_text)
    papers: list[RawPaper] = []
    for entry in root.findall(f"{ATOM}entry"):
        id_url = (entry.findtext(f"{ATOM}id") or "").strip()
        m = re.search(r"abs/(.+?)(v\d+)?$", id_url)
        if not m:
            continue
        arxiv_id, version = m.group(1), m.group(2) or "v1"
        published = entry.findtext(f"{ATOM}published")
        pub_dt = None
        if published:
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00")).replace(tzinfo=None)
        pdf_url = None
        for link in entry.findall(f"{ATOM}link"):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")
        papers.append(
            RawPaper(
                arxiv_id=arxiv_id,
                version=version,
                title=_clean(entry.findtext(f"{ATOM}title") or ""),
                authors=[_clean(a.findtext(f"{ATOM}name") or "") for a in entry.findall(f"{ATOM}author")],
                abstract=_clean(entry.findtext(f"{ATOM}summary") or ""),
                categories=[c.get("term", "") for c in entry.findall(f"{ATOM}category")],
                pdf_url=pdf_url,
                html_url=f"https://arxiv.org/html/{arxiv_id}",
                published_at=pub_dt,
            )
        )
    return papers


_last_request_time = 0.0


QUERY_STOPWORDS = {
    "latest", "recent", "new", "novel", "current", "survey", "advances",
    "achievements", "academia", "academic", "research", "method", "methods",
    "approach", "approaches", "field", "fields", "progress", "state",
}


def build_all_query(text: str) -> str:
    """把（通常已翻译成英文的）自然语言查询转成 arXiv 的 all: 检索式，词间 AND。

    过滤泛词并最多取 5 个词——全词 AND 语义下多余词会把结果清零。
    """
    terms = [
        t.lower()
        for t in re.split(r"[^A-Za-z0-9\-]+", (text or "").strip())
        if len(t) >= 2 and t.lower() not in QUERY_STOPWORDS
    ]
    if not terms:
        return ""
    return "+AND+".join(f"all:{t}" for t in terms[:5])


def _filter_terms(words: list[str], limit: int = 4) -> list[str]:
    """停用词过滤 + 小写归一，供意图查询构造复用。"""
    return [
        w.lower()
        for w in words
        if len(w) >= 2 and w.lower() not in QUERY_STOPWORDS
    ][:limit]


def build_intent_query(intent: dict) -> str:
    """结构化意图 -> arXiv 检索式：au:"作者" AND all:关键词 AND submittedDate 范围。"""
    parts = []
    for author in intent.get("authors") or []:
        parts.append(f'au:"{author}"')
    for term in _filter_terms(intent.get("keywords") or []):
        # 多词关键词是短语，加引号防被拆成裸词 AND
        quoted = f'"{term}"' if " " in term else term
        parts.append(f"all:{quoted}")
    from_date, to_date = intent.get("from_date"), intent.get("to_date")
    if from_date or to_date:
        start = (from_date or "1991-01-01").replace("-", "") + "0000"
        from datetime import date as _date

        end = (to_date or _date.today().isoformat()).replace("-", "") + "2359"
        parts.append(f"submittedDate:[{start} TO {end}]")
    return "+AND+".join(parts)


def search_relevant(
    query: str,
    max_results: int = 10,
    sort: str = "relevance",
    timeout: float = 30.0,
    retries: int = 3,
    search_query: str | None = None,
) -> list[RawPaper]:
    """按语义化查询（英文）在线检索 arXiv；sort=relevance/latest。

    search_query 传入预构建的检索式（意图模式），此时忽略 query 参数。
    """
    global _last_request_time
    if search_query is None:
        search_query = build_all_query(query)
    if not search_query:
        return []
    params = {
        "search_query": search_query,
        "sortBy": "submittedDate" if sort == "latest" else "relevance",
        "sortOrder": "descending",
        "max_results": str(min(max_results, 50)),
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        wait = 3.0 - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        try:
            from app.net import http_get

            resp = http_get(ARXIV_API, params=params)
            resp.raise_for_status()
            _last_request_time = time.monotonic()
            return parse_atom(resp.text)
        except Exception as exc:
            last_error = exc
            _last_request_time = time.monotonic()
            time.sleep(2**attempt)
    raise RuntimeError(f"arXiv 检索失败（已重试 {retries} 次）：{last_error}")


def fetch_by_ids(arxiv_ids: list[str], timeout: float = 30.0) -> list[RawPaper]:
    """按 arXiv ID 精确取元数据（在线检索提炼入库用，服务端权威数据）。"""
    global _last_request_time
    if not arxiv_ids:
        return []
    ids = arxiv_ids[:20]
    params = {"id_list": ",".join(ids), "max_results": str(len(ids))}
    last_error: Exception | None = None
    for attempt in range(3):
        wait = 3.0 - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        try:
            from app.net import http_get

            resp = http_get(ARXIV_API, params=params)
            resp.raise_for_status()
            _last_request_time = time.monotonic()
            return parse_atom(resp.text)
        except Exception as exc:
            last_error = exc
            _last_request_time = time.monotonic()
            time.sleep(2**attempt)
    raise RuntimeError(f"arXiv 按 ID 取元数据失败（已重试 3 次）：{last_error}")


def fetch_recent(
    categories: list[str],
    lookback_days: int = 1,
    max_results: int = 100,
    timeout: float = 30.0,
    retries: int = 3,
) -> list[RawPaper]:
    """按分类取最近提交的论文，过滤 lookback 窗口外与解析失败的条目。

    arXiv 官方要求请求间隔 3 秒，函数内做全进程节流。
    """
    global _last_request_time
    params = {
        # 用空格连接：httpx 会把空格编码为 %20（arXiv 认可）；若用 + 会被编码成 %2B，
        # arXiv 把它当字面加号，整个查询失效返回 0 条
        "search_query": " OR ".join(f"cat:{c}" for c in categories),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(max_results),
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        wait = 3.0 - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        try:
            from app.net import http_get

            resp = http_get(ARXIV_API, params=params)
            resp.raise_for_status()
            _last_request_time = time.monotonic()
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)
            return [p for p in parse_atom(resp.text) if p.published_at and p.published_at >= cutoff]
        except Exception as exc:  # 网络抖动/限流统一退避重试
            last_error = exc
            _last_request_time = time.monotonic()
            time.sleep(2**attempt)
    raise RuntimeError(f"arXiv API 请求失败（已重试 {retries} 次）：{last_error}")
