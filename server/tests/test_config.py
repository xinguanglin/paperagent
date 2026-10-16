import pytest

from app.config import load_config


def test_load_config_roundtrip(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        """
api_key: "test-key"
mysql:
  host: 127.0.0.1
  port: 3307
  user: alice
  password: "p@ss w:ord"
  database: paper_agent
server:
  port: 9000
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.api_key == "test-key"
    assert cfg.mysql_host == "127.0.0.1"
    assert cfg.mysql_port == 3307
    assert cfg.mysql_user == "alice"
    assert cfg.mysql_password == "p@ss w:ord"
    assert cfg.mysql_database == "paper_agent"
    assert cfg.server_port == 9000
    # 密码特殊字符需要被 URL 编码后拼进 DSN
    assert "p%40ss+w%3Aord" in cfg.mysql_url


def test_defaults_when_fields_missing(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("api_key: k\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.mysql_host == "localhost"
    assert cfg.mysql_port == 3306
    assert cfg.server_port == 8787
    assert cfg.mysql_password == ""


def test_missing_config_raises_with_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.yaml"):
        load_config(tmp_path / "nope.yaml")
