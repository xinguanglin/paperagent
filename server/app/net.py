"""共享 HTTP 客户端：统一代理配置、UA、超时。"""
from __future__ import annotations

import httpx
import ssl

DEFAULT_TIMEOUT = 30.0
USER_AGENT = "paper-agent/0.1 (personal research digest tool)"

# 2026-09 起 arXiv WAF 按 TLS 指纹拦截 httpx 默认握手（一律 406 Not Acceptable，
# 换 IP/UA 均无效，curl 与 requests 正常）。改用常见浏览器密码套件后恢复。
TLS_CIPHERS = (
    "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES256-GCM-SHA384:AES128-SHA"
)

_client: httpx.Client | None = None


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_ciphers(TLS_CIPHERS)
    return ctx


def configure_http(proxy: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
    global _client
    if _client is not None:
        _client.close()
    _client = httpx.Client(
        proxy=(proxy or None),
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        verify=_tls_context(),
    )


def http_get(url: str, params: dict | None = None) -> httpx.Response:
    if _client is None:
        configure_http()
    assert _client is not None
    return _client.get(url, params=params)
