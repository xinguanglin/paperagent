from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Config, load_config
from app.models import Base, Setting

_engine: Engine | None = None
SessionLocal: sessionmaker | None = None


def default_keywords() -> list[dict]:
    return [
        {"word": word, "weight": weight}
        for word, weight in [
            ("LLM", 5),
            ("agent", 4),
            ("reasoning", 4),
            ("RAG", 3),
            ("multimodal", 3),
            ("RLHF", 3),
        ]
    ]


def init_engine(config: Config | None = None) -> Engine:
    global _engine, SessionLocal
    if _engine is not None:
        return _engine
    cfg = config or load_config()
    _engine = create_engine(cfg.mysql_url, pool_pre_ping=True, pool_recycle=3600)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    return init_engine()


def get_session_factory() -> sessionmaker:
    if SessionLocal is None:
        init_engine()
    assert SessionLocal is not None
    return SessionLocal


def init_db(engine: Engine) -> None:
    """建表（幂等）并保证 settings 单行存在。测试可传入 sqlite 引擎。"""
    Base.metadata.create_all(engine)
    ensure_columns(engine)
    _backfill_chat_model(engine)
    with Session(engine) as session:
        if session.get(Setting, 1) is None:
            session.add(Setting(id=1, keywords=default_keywords()))
            session.commit()


def _backfill_chat_model(engine: Engine) -> None:
    """一次性迁移：旧列 glm_model 的值搬进新列 chat_model 后删除旧列（真·改名）。

    全新库里没有 glm_model 列，静默跳过；chat_model 已有值时不覆盖。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "settings" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("settings")}
    if "glm_model" not in columns or "chat_model" not in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("UPDATE settings SET chat_model = glm_model WHERE chat_model = ''"))
        conn.execute(text("ALTER TABLE settings DROP COLUMN glm_model"))


# create_all 只建新表、不会给存量表补新列；远端存量库升级靠这里做最小迁移。
# 新增列时同步维护（列名, DDL），MySQL 8 与 sqlite 均兼容。
_PENDING_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "digests": (
        ("implementation", "TEXT"),
        ("diagrams", "JSON"),
    ),
    "settings": (
        ("sources", "JSON"),
        ("vision_model", "VARCHAR(64)"),
        ("chat_model", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ),
    "papers": (
        ("origin", "VARCHAR(16) NOT NULL DEFAULT 'fetch'"),
        ("local_file", "VARCHAR(512)"),
        ("local_fulltext", "MEDIUMTEXT"),
        ("figures", "JSON"),
    ),
}


def ensure_columns(engine: Engine) -> None:
    """为存量表补齐缺失的新列（幂等）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table, columns in _PENDING_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue  # 不存在的表交给 create_all
        existing = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
