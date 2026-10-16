"""ensure_columns 存量库轻量迁移的回归测试。"""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from app.database import ensure_columns


def _legacy_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 模拟未升级的旧库：digests 缺 implementation、settings 缺 sources
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE digests ("
                "id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL, one_liner TEXT)"
            )
        )
        conn.execute(
            text("CREATE TABLE settings (id INTEGER PRIMARY KEY, keywords JSON)")
        )
    return engine


def test_ensure_columns_adds_missing_columns():
    engine = _legacy_engine()
    ensure_columns(engine)
    digest_cols = {c["name"] for c in inspect(engine).get_columns("digests")}
    setting_cols = {c["name"] for c in inspect(engine).get_columns("settings")}
    assert "implementation" in digest_cols
    assert "sources" in setting_cols


def test_ensure_columns_is_idempotent():
    engine = _legacy_engine()
    ensure_columns(engine)
    ensure_columns(engine)  # 第二次不报"列已存在"
    assert "implementation" in {c["name"] for c in inspect(engine).get_columns("digests")}


def test_ensure_columns_skips_missing_tables():
    engine = _legacy_engine()  # 没建 papers 表
    ensure_columns(engine)  # 不存在的表不处理，交给 create_all


def test_chat_model_rename_backfills_and_drops_old_column():
    """glm_model → chat_model 真·改名：值搬过去、旧列删除、幂等。"""
    from app.database import init_db

    engine = _legacy_engine()
    with engine.begin() as conn:
        # 模拟真实的未改名旧库：老功能列齐全 + 旧列 glm_model 有值
        for ddl in (
            "ALTER TABLE settings ADD COLUMN categories VARCHAR(255)",
            "ALTER TABLE settings ADD COLUMN top_n INTEGER",
            "ALTER TABLE settings ADD COLUMN cron_expr VARCHAR(32)",
            "ALTER TABLE settings ADD COLUMN updated_at DATETIME",
            "ALTER TABLE settings ADD COLUMN glm_model VARCHAR(64)",
        ):
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO settings (id, glm_model) VALUES (1, 'old-model')"))

    init_db(engine)  # ensure_columns 补 chat_model + 回填 + 删旧列

    cols = {c["name"] for c in inspect(engine).get_columns("settings")}
    assert "chat_model" in cols
    assert "glm_model" not in cols
    with engine.begin() as conn:
        assert conn.execute(text("SELECT chat_model FROM settings WHERE id=1")).scalar() == "old-model"

    init_db(engine)  # 第二次：旧列已不存在，静默跳过不报错
