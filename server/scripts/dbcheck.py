import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.database import init_engine, init_db
from sqlalchemy import text

cfg = load_config("../config.yaml")
engine = init_engine(cfg)
init_db(engine)
with engine.connect() as conn:
    tables = conn.execute(text("SHOW TABLES")).fetchall()
    print("tables:", [t[0] for t in tables])
    row = conn.execute(text("SELECT id, categories, top_n, chat_model FROM settings")).fetchall()
    print("settings:", row)
