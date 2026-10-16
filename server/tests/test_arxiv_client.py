from app.arxiv_client import parse_atom

ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2609.01234v2</id>
    <updated>2026-09-15T00:41:18Z</updated>
    <published>2026-09-14T17:59:59Z</published>
    <title>AgentFugue: Multi-Agent\n   Orchestration</title>
    <summary>A framework for multi-agent collaboration.</summary>
    <author><name>Hiro Tanaka</name></author>
    <author><name>Maria Lopez</name></author>
    <category term="cs.AI"/>
    <category term="cs.MA"/>
    <link href="http://arxiv.org/pdf/2609.01234v2" title="pdf" rel="related"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2608.99999</id>
    <published>2026-09-13T10:00:00Z</published>
    <title>Old Paper</title>
    <summary>Without pdf link.</summary>
    <author><name>Someone</name></author>
    <category term="cs.CL"/>
  </entry>
</feed>
"""


def test_parse_atom_fields():
    papers = parse_atom(ATOM_XML)
    assert len(papers) == 2

    first = papers[0]
    assert first.arxiv_id == "2609.01234"
    assert first.version == "v2"
    assert first.title == "AgentFugue: Multi-Agent Orchestration"  # 折叠换行空白
    assert first.authors == ["Hiro Tanaka", "Maria Lopez"]
    assert first.categories == ["cs.AI", "cs.MA"]
    assert first.pdf_url == "http://arxiv.org/pdf/2609.01234v2"
    assert first.html_url == "https://arxiv.org/html/2609.01234"
    assert first.published_at is not None
    assert first.published_at.year == 2026 and first.published_at.month == 9

    second = papers[1]
    assert second.version == "v1"
    assert second.pdf_url is None


def test_fetch_recent_query_uses_spaces_not_plus(monkeypatch):
    """回归：search_query 必须用空格连接。httpx 会把空格编码为 %20（arXiv 认可），
    而字面 + 会被编码成 %2B，arXiv 当作普通字符导致查询命中 0 条（批次"成功"但零抓取）。"""
    import app.arxiv_client as ac

    captured = {}

    class FakeResp:
        status_code = 200
        text = ATOM_XML

        def raise_for_status(self):
            pass

    def fake_get(url, params=None):
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr("app.net.http_get", fake_get)
    ac.fetch_recent(["cs.AI", "cs.CL", "cs.LG"], lookback_days=2, max_results=5)
    assert captured["params"]["search_query"] == "cat:cs.AI OR cat:cs.CL OR cat:cs.LG"


def test_build_all_query_terms_and_cap():
    from app.arxiv_client import build_all_query

    # 泛词（latest/advances）被过滤，避免全 AND 语义下清零结果
    assert build_all_query("knowledge graph latest advances") == "all:knowledge+AND+all:graph"
    # 中文/标点被过滤；小写归一；超过 5 个词截断
    assert build_all_query("知识图谱！!!   LLM   agent") == "all:llm+AND+all:agent"
    assert build_all_query("   ") == ""
    assert len(build_all_query(" ".join(f"w{i}" for i in range(30))).split("+AND+")) == 5


def test_build_intent_query_author_date_keywords():
    from app.arxiv_client import build_intent_query

    intent = {
        "keywords": ["spatial intelligence", "embodied", "research"],
        "authors": ["Fei-Fei Li"],
        "from_date": "2026-06-01",
        "to_date": None,
        "sort_hint": "latest",
    }
    q = build_intent_query(intent)
    assert 'au:"Fei-Fei Li"' in q
    assert 'all:"spatial intelligence"' in q  # 多词短语要加引号
    assert "all:embodied" in q
    assert "research" not in q  # 泛词被过滤
    assert "submittedDate:[202606010000 TO 202609162359]" in q or "submittedDate:[20260601" in q


def test_build_intent_query_author_only():
    from app.arxiv_client import build_intent_query

    q = build_intent_query({"keywords": [], "authors": ["Wei Wang"], "from_date": None, "to_date": None})
    assert q == 'au:"Wei Wang"'
