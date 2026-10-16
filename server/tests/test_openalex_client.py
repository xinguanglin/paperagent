from app.openalex_client import invert_abstract, parse_works

PAYLOAD = {
    "results": [
        {
            "title": "AgentFugue: Multi-Agent Orchestration.",
            "publication_date": "2026-09-14",
            "authorships": [
                {"author": {"display_name": "Hiro Tanaka"}},
                {"author": {"display_name": "Maria Lopez"}},
                {"author": {"display_name": None}},
            ],
            "primary_location": {
                "landing_page_url": "https://arxiv.org/abs/2609.04567",
                "pdf_url": "https://arxiv.org/pdf/2609.04567",
            },
            "concepts": [
                {"display_name": "Machine learning", "score": 0.9},
                {"display_name": "Artificial intelligence", "score": 0.8},
                {"display_name": "Computer science", "score": 0.5},
                {"display_name": "Physics", "score": 0.1},
            ],
            "abstract_inverted_index": {
                "A": [0], "framework": [2], "multi-agent": [1]
            },
        },
        {
            "title": "Not arXiv hosted",
            "publication_date": "2026-09-14",
            "primary_location": {"landing_page_url": "https://doi.org/10.1000/x"},
            "concepts": [],
        },
    ]
}


def test_invert_abstract_orders_by_position():
    assert invert_abstract({"A": [0], "multi-agent": [1], "framework": [2]}) == "A multi-agent framework"
    assert invert_abstract(None) == ""


def test_parse_works_maps_fields_and_skips_non_arxiv():
    papers = parse_works(PAYLOAD)
    assert len(papers) == 1  # DOI-only 的一条被跳过

    p = papers[0]
    assert p.arxiv_id == "2609.04567"
    assert p.title == "AgentFugue: Multi-Agent Orchestration"  # 去掉结尾句号
    assert p.authors == ["Hiro Tanaka", "Maria Lopez"]  # None 作者被过滤
    assert "Machine learning" in p.categories and "Physics" not in p.categories
    assert p.abstract == "A multi-agent framework"
    assert p.pdf_url and "/pdf/" in p.pdf_url
    assert p.published_at is not None and p.published_at.year == 2026


def test_build_intent_params():
    from app.openalex_client import build_intent_params

    intent = {
        "keywords": ["knowledge graph"],
        "authors": ["Fei-Fei Li"],
        "from_date": "2026-01-01",
        "to_date": None,
    }
    p = build_intent_params(intent, max_results=10, sort="citations")
    assert p["sort"] == "cited_by_count:desc"
    assert "raw_author_name.search:Fei-Fei Li" in p["filter"]
    assert "from_publication_date:2026-01-01" in p["filter"]
    assert p["search"] == "knowledge graph"
