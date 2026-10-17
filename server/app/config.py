from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import yaml


@dataclass
class Config:
    api_key: str = ""
    # OpenAI 兼容服务地址：空 = 官方默认；套餐通道/自建服务（Ollama、vLLM 等）填各自地址
    base_url: str = ""
    # 对话/解读模型（任何 OpenAI 兼容服务的模型名）
    chat_model: str = ""
    # 推理档位（如 GLM-5.x 的 low）：非空才随对话请求发送思考参数，其他厂商留空
    chat_thinking: str = ""
    embedding_model: str = "embedding-3"
    embedding_dims: int = 1024
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "paper_agent"
    server_host: str = "127.0.0.1"
    server_port: int = 8787
    proxy: str = ""  # 例如 http://127.0.0.1:7890；arXiv 不可达时必填
    memoryhub_url: str = ""   # MemoryHub 记忆服务地址；空 = 论文对话不带跨会话记忆
    memoryhub_key: str = ""
    source: str = "auto"  # auto: arXiv 优先、OpenAlex 降级；也可 arxiv / openalex
    upload_dir: str = "uploads"  # 手工上传论文文件的存放目录（相对项目根）
    # 视觉识图模型：空 = 关闭识图功能（提炼行为与纯文本管线一致）
    vision_model: str = ""
    vision_max_figures: int = 10  # 每篇论文最多识图张数（成本闸门）
    # 论文对话注入原文全文（含识图转述），可回答提炼未覆盖的细节；关闭省 token
    chat_fulltext: bool = True

    @property
    def mysql_url(self) -> str:
        pwd = quote_plus(self.mysql_password)
        return (
            f"mysql+pymysql://{self.mysql_user}:{pwd}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {p.resolve()}。"
            "请复制 config.example.yaml 为 config.yaml，并填写 LLM API Key 与 MySQL 连接信息。"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    mysql = raw.get("mysql") or {}
    server = raw.get("server") or {}
    return Config(
        api_key=str(raw.get("api_key") or ""),
        base_url=str(raw.get("base_url") or ""),
        chat_model=str(raw.get("chat_model") or ""),
        chat_thinking=str(raw.get("chat_thinking") or ""),
        embedding_model=str(raw.get("embedding_model") or "embedding-3"),
        embedding_dims=int(raw.get("embedding_dims") or 1024),
        mysql_host=mysql.get("host", "localhost"),
        mysql_port=int(mysql.get("port", 3306)),
        mysql_user=mysql.get("user", "root"),
        mysql_password=str(mysql.get("password") or ""),
        mysql_database=mysql.get("database", "paper_agent"),
        server_host=server.get("host", "127.0.0.1"),
        server_port=int(server.get("port", 8787)),
        proxy=str(raw.get("proxy") or ""),
        memoryhub_url=str(raw.get("memoryhub_url") or ""),
        memoryhub_key=str(raw.get("memoryhub_key") or ""),
        source=str(raw.get("source") or "auto"),
        upload_dir=str(raw.get("upload_dir") or "uploads"),
        vision_model=str(raw.get("vision_model") or ""),
        chat_fulltext=bool(raw.get("chat_fulltext", True)),
        vision_max_figures=int(raw["vision_max_figures"]) if raw.get("vision_max_figures") is not None else 10,
    )
