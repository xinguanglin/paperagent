from app.scorer import score_paper

KEYWORDS = [
    {"word": "LLM", "weight": 5},
    {"word": "agent", "weight": 4},
    {"word": "RAG", "weight": 3},
]


def test_hits_sum_weights_case_insensitive():
    text = "Scaling LLM agents with RAG pipelines"
    assert score_paper(text, KEYWORDS) == 12


def test_no_hit_returns_zero():
    assert score_paper("A survey of database indexes", KEYWORDS) == 0


def test_single_hit():
    assert score_paper("Agent swarms for logistics", KEYWORDS) == 4


def test_substring_match_on_lowered_text():
    # "agents" 包含 "agent"，按子串命中
    assert score_paper("Multi-agent systems", KEYWORDS) == 4


def test_empty_inputs():
    assert score_paper("", KEYWORDS) == 0
    assert score_paper("LLM rocks", []) == 0
    assert score_paper("LLM rocks", [{"word": "", "weight": 9}]) == 0
