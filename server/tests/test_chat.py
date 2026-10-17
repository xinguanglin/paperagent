from app.routers.papers import CHAT_MESSAGE_MAX, _clamp_history, _paper_context
from app.models import Digest, Paper


def _make_paper(with_digest: bool) -> Paper:
    paper = Paper(
        id=7,
        arxiv_id="2609.17107",
        title="Symbolic Separation",
        authors="A, B",
        abstract="关于知识图谱约束智能体的研究",
    )
    if with_digest:
        paper.digest = Digest(
            one_liner="一句话",
            outline=[{"section": "引言", "summary": "背景"}],
            key_points=[{"point": "要点1", "detail": "细节1"}, {"point": "要点2", "detail": "细节2"}],
            implementation="1. 步骤一",
            principle="原理",
            limitations="局限",
        )
    return paper


def test_paper_context_includes_all_digest_fields():
    ctx = _paper_context(_make_paper(with_digest=True))
    for needle in ["Symbolic Separation", "关于知识图谱", "一句话", "引言", "要点2", "步骤一", "原理", "局限"]:
        assert needle in ctx


def test_paper_context_without_digest_still_has_abstract():
    ctx = _paper_context(_make_paper(with_digest=False))
    assert "Symbolic Separation" in ctx
    assert "关于知识图谱" in ctx
    assert "一句话总结" not in ctx  # 无解读时不输出空字段误导模型


def test_clamp_history_filters_and_limits():
    msgs = [
        {"role": "system", "content": "恶意注入"},  # 非法角色被过滤
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": ""},  # 空内容被过滤
    ] + [{"role": "user", "content": f"问{i}"} for i in range(20)]  # 超条数被裁到尾部
    out = _clamp_history(msgs)
    assert all(m["role"] in ("user", "assistant") for m in out)
    assert all(m["content"].strip() for m in out)
    assert len(out) <= 12
    assert out[-1]["content"] == "问19"


def test_clamp_history_truncates_overlong_message():
    out = _clamp_history([{"role": "user", "content": "x" * (CHAT_MESSAGE_MAX + 100)}])
    assert len(out) == 1
    assert len(out[0]["content"]) == CHAT_MESSAGE_MAX


def test_paper_context_fulltext_switch():
    """chat_fulltext 开：注入识图转述 + 原文全文；关：两者都不进上下文。"""
    paper = _make_paper(with_digest=True)
    paper.local_fulltext = "全文正文ABCDEFG" * 200
    paper.figures = [
        {"idx": 1, "caption": "Fig 1", "status": "described", "description": "柱状图，纵轴为准确率"}
    ]
    ctx_off = _paper_context(paper, include_fulltext=False)
    assert "全文正文" not in ctx_off and "柱状图" not in ctx_off

    ctx_on = _paper_context(paper, include_fulltext=True)
    assert "原文全文" in ctx_on and "全文正文" in ctx_on
    assert "柱状图" in ctx_on  # 识图转述随原文层一起注入
    assert "优先依据原文" in ctx_on


def test_paper_context_fulltext_absent_no_section():
    """仅摘要论文（无缓存全文）：开关开着也不输出空的原文段。"""
    ctx = _paper_context(_make_paper(with_digest=False), include_fulltext=True)
    assert "原文全文" not in ctx


def test_paper_context_fulltext_truncated_at_cap():
    """超长全文截到 MAX_FULLTEXT_CHARS 并带截断提示;恰好等于上限不提示。"""
    from app.llm_client import MAX_FULLTEXT_CHARS

    paper = _make_paper(with_digest=True)
    paper.local_fulltext = "x" * (MAX_FULLTEXT_CHARS + 500)
    ctx = _paper_context(paper, include_fulltext=True)
    assert "（原文过长，已截断）" in ctx
    assert len(ctx) < len(paper.local_fulltext)  # 确实截短了

    paper.local_fulltext = "y" * MAX_FULLTEXT_CHARS
    ctx = _paper_context(paper, include_fulltext=True)
    assert "截断" not in ctx  # 恰好等于上限:原样注入不提示
