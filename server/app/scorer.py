from __future__ import annotations


def score_paper(text: str, keywords: list[dict]) -> int:
    """关键词命中打分：命中的词权重求和，大小写不敏感。"""
    lowered = (text or "").lower()
    return sum(
        int(kw.get("weight", 1))
        for kw in keywords
        if str(kw.get("word", "")).strip() and str(kw["word"]).lower() in lowered
    )
