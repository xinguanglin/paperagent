from __future__ import annotations

import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

MAX_TEXT_CHARS = 60_000


def _make_soup(html: str) -> BeautifulSoup:
    """arXiv HTML → 去噪后的 soup：剔除脚本/导航，公式替换为 LaTeX 源码。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "aside"]):
        tag.decompose()
    # latexml 公式带 alttext 属性（即 LaTeX 源码）：替换为 $...$ 再取文本，
    # 公式零成本精确恢复；无 alttext 的（罕见）留空避免取出一串 MathML 碎片
    for math in soup.find_all("math"):
        alt = math.get("alttext")
        math.replace_with(f" ${alt}$ " if alt else " ")
    return soup


def _text_from(soup: BeautifulSoup) -> str:
    text = soup.get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()
    return text[:MAX_TEXT_CHARS]


def extract_text(html: str) -> str:
    """arXiv HTML → 纯文本：去掉脚本/导航等噪音，公式替换为 LaTeX，折叠空白，截断上限。"""
    return _text_from(_make_soup(html))


def extract_figures(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """提取正文图片清单：排除站点图标，相对路径按页面 URL 解析，caption 就近配对。

    返回 [{idx, url, caption}]；只提清单不下载，下载校验见 app.figures。
    """
    seen: set[str] = set()
    out: list[dict] = []

    def _icon_like(img) -> bool:
        src = (img.get("src") or img.get("data-src") or "")
        if src.startswith("/static/") or "logo" in src.lower() or "icon" in src.lower():
            return True
        cls = img.get("class")
        cls_text = " ".join(cls) if isinstance(cls, list) else str(cls or "")
        return "logo" in cls_text.lower() or "icon" in cls_text.lower()

    for img in soup.find_all("img"):
        if _icon_like(img):
            continue
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        url = urljoin(page_url, src)
        if url in seen:
            continue
        seen.add(url)
        caption = ""
        container = img.find_parent("figure")
        if container is not None:
            cap = container.find("figcaption")
            if cap is not None:
                caption = cap.get_text(" ", strip=True)
        if not caption:
            caption = (img.get("alt") or "").strip()
        out.append({"idx": len(out) + 1, "url": url, "caption": caption[:300], "status": "pending"})
    return out


def fetch_fulltext(
    html_url: str, timeout: float = 30.0, retries: int = 2
) -> tuple[str, bool, list[dict]] | None:
    """抓取 arXiv HTML 全文，返回 (正文, 是否被截断, 图片清单)。

    任何失败返回 None（调用方降级用摘要）。HTTP 客户端（含代理）由 app.net 统一提供。
    """
    from app.net import http_get

    for attempt in range(retries + 1):
        try:
            resp = http_get(html_url)
            if resp.status_code == 200:
                soup = _make_soup(resp.text)
                figures = extract_figures(soup, html_url)
                text = _text_from(soup)
                if len(text) >= 500:  # 太短多半是错误页/占位页
                    return text, len(resp.text) > MAX_TEXT_CHARS, figures
                return None
            if 400 <= resp.status_code < 500:
                return None  # 404 等客户端错误，重试无意义
        except httpx.HTTPError:
            pass
        if attempt < retries:
            time.sleep(1.0)
    return None
