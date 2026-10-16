"""Key 有效性预检：一次最小 embedding 调用。密钥从 config.yaml 读取。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.llm_client import LlmClient

cfg = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
client = LlmClient(cfg.api_key, base_url=cfg.base_url or None,
                   embedding_model=cfg.embedding_model, embedding_dims=cfg.embedding_dims)
vec = client.embed("多智能体协作")
print(f"embedding OK: {len(vec)} dims, head={vec[:3]}")
