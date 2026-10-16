import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app import figures as fig_mod
from app.figures import (
    MAX_EDGE,
    compress_to_jpeg,
    download_one,
    is_allowed_image_url,
    sniff_image_mime,
)


def _png_bytes(w=2000, h=1000):
    img = Image.new("RGB", (w, h), "red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _resp(content=b"", status=200, url="https://arxiv.org/x.png"):
    return SimpleNamespace(status_code=status, content=content, url=url)


def test_url_whitelist_blocks_ssrf():
    """图片 URL 来自外部 HTML，只允许 https + arxiv.org 系域名（防内网探测）。"""
    assert is_allowed_image_url("https://arxiv.org/html/x1.png")
    assert is_allowed_image_url("https://static.arxiv.org/html/x1.png")
    assert not is_allowed_image_url("http://arxiv.org/x.png")  # 非 https
    assert not is_allowed_image_url("https://169.254.169.254/metadata")
    assert not is_allowed_image_url("https://evil.com/arxiv.org.png")
    assert not is_allowed_image_url("https://arxiv.org.evil.io/x.png")


def test_sniff_image_mime_by_magic_bytes():
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 10) == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0" + b"0" * 10) == "image/jpeg"
    assert sniff_image_mime(b"RIFF1234WEBP" + b"0" * 10) == "image/webp"
    assert sniff_image_mime(b"MZ\x90\x00evil-exe") is None  # 伪装可执行文件拒收
    assert sniff_image_mime(b"") is None


def test_compress_to_jpeg_caps_edge():
    jpg = compress_to_jpeg(_png_bytes(3000, 1500))
    img = Image.open(io.BytesIO(jpg))
    assert max(img.size) <= MAX_EDGE
    assert img.format == "JPEG"


def test_download_one_rejects_oversize_and_fake_image(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.net.http_get",
        lambda url: _resp(content=b"x" * (fig_mod.MAX_DOWNLOAD_BYTES + 1)),
    )
    fig = {"idx": 1, "url": "https://arxiv.org/a.png", "caption": ""}
    download_one(fig, "2606.00001", tmp_path)
    assert fig["status"] == "skipped"  # 超限

    monkeypatch.setattr("app.net.http_get", lambda url: _resp(content=b"not-an-image" * 100))
    fig2 = {"idx": 2, "url": "https://arxiv.org/b.png", "caption": ""}
    download_one(fig2, "2606.00001", tmp_path)
    assert fig2["status"] == "skipped"  # magic bytes 不符


def test_download_one_success_and_skip_cached(monkeypatch, tmp_path):
    png = _png_bytes(800, 600)
    calls = {"n": 0}

    def counting_get(url):
        calls["n"] += 1
        return _resp(content=png)

    monkeypatch.setattr("app.net.http_get", counting_get)
    fig = {"idx": 3, "url": "https://arxiv.org/html/2606.00001/x3.png", "caption": "Figure 3"}
    download_one(fig, "2606.00001", tmp_path)
    assert fig["status"] == "pending"
    assert fig["mime"] == "image/jpeg"
    p = Path(fig["path"])
    assert p.is_file() and p.parent.name == "2606.00001" and p.name == "fig-003.jpg"

    download_one(fig, "2606.00001", tmp_path)  # 已落盘：不重复下载
    assert calls["n"] == 1


def test_download_one_marks_failed_on_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr("app.net.http_get", lambda url: _resp(status=500))
    fig = {"idx": 1, "url": "https://arxiv.org/x.png", "caption": ""}
    download_one(fig, "2606.00001", tmp_path)
    assert fig["status"] == "failed"
