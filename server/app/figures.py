"""原文图片的下载、校验与压缩（多模态识图的一环，见 docs/design/multimodal-figure-understanding-design.md）。

安全边界：图片 URL 来自外部 HTML（不可信输入）——
仅允许 https + arxiv.org 域（SSRF 防线）、magic bytes 鉴型（不信 Content-Type）、
5MB 下载上限、文件名服务端生成（防穿越）。
"""
from __future__ import annotations

import io
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024  # 5MB 下载上限（压缩前）
MAX_EDGE = 1568  # 压缩后长边上限：视觉 token 与 base64 体积同时受控


def is_allowed_image_url(url: str) -> bool:
    """SSRF 白名单：仅 https + arxiv.org（含子域）。"""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    return host == "arxiv.org" or host.endswith(".arxiv.org")


def sniff_image_mime(data: bytes) -> str | None:
    """按文件头鉴型，不信响应 Content-Type。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def compress_to_jpeg(data: bytes) -> bytes:
    """统一压缩：长边 ≤ MAX_EDGE、JPEG q85。GIF 动图取首帧（Image.open 默认第 0 帧）。"""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    scale = MAX_EDGE / max(w, h)
    if scale < 1:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _safe_dir_name(arxiv_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", arxiv_id)


def download_one(fig: dict, arxiv_id: str, base_dir: Path) -> None:
    """下载并落盘单张图片（网络错重试 1 次，设计 §4.2），原地更新 fig 的 path/bytes/mime/status。

    status 流转：pending(初始) → pending(带 path，可识图) / skipped(白名单外/超限/类型不符) / failed(网络错)。
    任何异常不外抛——单图失败不阻塞其他图与后续解读。
    """
    url = fig.get("url", "")
    if not is_allowed_image_url(url):
        fig["status"] = "skipped"
        return
    # 已下载且文件在、字节一致：跳过（崩溃恢复/重跑场景，设计 §5）
    out_dir = Path(base_dir) / _safe_dir_name(arxiv_id)
    path = fig.get("path")
    if path:
        p = Path(path)
        if p.is_file() and p.parent == out_dir and p.stat().st_size == fig.get("bytes", -1):
            return
    for attempt in range(2):
        try:
            from app.net import http_get

            resp = http_get(url)
            # 重定向可能跳离白名单域：最终 URL 也要过检（SSRF 纵深防御）
            if not is_allowed_image_url(str(resp.url)):
                fig["status"] = "skipped"
                return
            if resp.status_code == 200:
                data = resp.content
                if len(data) > MAX_DOWNLOAD_BYTES:
                    fig["status"] = "skipped"
                    return
                if sniff_image_mime(data) is None:
                    fig["status"] = "skipped"
                    return
                jpg = compress_to_jpeg(data)
                out_dir.mkdir(parents=True, exist_ok=True)
                out = out_dir / f"fig-{fig['idx']:03d}.jpg"
                out.write_bytes(jpg)
                fig.update(
                    path=str(out).replace("\\", "/"), bytes=len(jpg), mime="image/jpeg", status="pending"
                )
                return
            if 400 <= resp.status_code < 500:
                break  # 4xx 重试无意义
        except Exception:
            logger.warning("图片下载失败(第 %s 次) %s", attempt + 1, url, exc_info=True)
        if attempt == 0:
            time.sleep(1.0)
    fig["status"] = "failed"
