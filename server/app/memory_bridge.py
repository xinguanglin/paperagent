"""paper-agent 接入 MemoryHub 记忆服务（一期验收锚点）。

行为：
- 对话前：以用户最新问题为 query 调 sessions/begin，把返回的记忆上下文
  拼进 system prompt（论文材料 + 历史记忆）
- 对话后：用户问题 + 助力回复经 sessions/end 写回记忆（异步加工）
- 未配置/不可达：静默降级为无记忆对话（不阻断主功能），日志留痕
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

NAMESPACE = "paper-chat"
SUBJECT = "default-user"


class MemoryBridge:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout

    def begin(self, query: str, token_budget: int = 800) -> str:
        """拿记忆注入上下文；失败返回空串（降级）。"""
        try:
            r = httpx.post(
                f"{self._base}/v1/sessions/begin",
                headers=self._headers,
                json={
                    "namespace": NAMESPACE,
                    "subject": SUBJECT,
                    "query": query[:500],
                    "token_budget": token_budget,
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
            ctx = (r.json() or {}).get("context") or ""
            return f"\n\n{ctx}" if ctx else ""
        except Exception:
            logger.info("MemoryHub 记忆注入降级（不可达或报错）", exc_info=True)
            return ""

    def end(self, messages: list[dict]) -> None:
        """写回记忆；失败仅记日志。"""
        try:
            httpx.post(
                f"{self._base}/v1/sessions/end",
                headers=self._headers,
                json={"namespace": NAMESPACE, "subject": SUBJECT, "messages": messages},
                timeout=self._timeout,
            )
        except Exception:
            logger.info("MemoryHub 记忆写回失败（不影响对话）", exc_info=True)


def get_memory_bridge(cfg) -> MemoryBridge | None:
    """配置了 memoryhub_url 才启用；未配置返回 None（功能关闭）。"""
    url = getattr(cfg, "memoryhub_url", "") or ""
    key = getattr(cfg, "memoryhub_key", "") or ""
    if not url or not key:
        return None
    return MemoryBridge(url, key)
