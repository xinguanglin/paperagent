"""手工上传论文：解析器与入库/提炼链路的测试。"""

import io

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import init_db
from app.models import Paper
from app.pipeline import Pipeline, RunState
from app.upload_parser import detect_format, guess_title, parse_paper_file, sanitize_filename

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ---------- 解析器 ----------

def test_detect_format_by_magic_bytes():
    assert detect_format(PDF_MAGIC + b"rest") == "pdf"
    assert detect_format(ZIP_MAGIC + b"rest") == "docx"
    assert detect_format(OLE_MAGIC + b"rest") == "doc"
    with pytest.raises(ValueError, match="无法识别"):
        detect_format(b"MZ\x90\x00garbage")


def test_sanitize_filename_blocks_path_traversal():
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert "\\" not in sanitize_filename("..\\..\\windows\\win.ini")
    assert sanitize_filename("我的 论文*final?.pdf") == "我的_论文_final_.pdf"
    assert sanitize_filename("") == "paper"


def test_guess_title_prefers_title_like_line():
    text = "\n\n  A Study of LLM Agents  \n这是一段很长的正文……" + "字" * 300
    assert guess_title(text, "fallback") == "A Study of LLM Agents"
    assert guess_title("", "fallback.pdf") == "fallback.pdf"


def _make_pdf(title: str, body: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 90), title, fontsize=16)
    page.insert_textbox(fitz.Rect(72, 120, 520, 700), body)
    data = doc.tobytes()
    doc.close()
    return data


def test_parse_pdf_roundtrip():
    data = _make_pdf("Attention Is All You Need", "本文提出 Transformer 架构。" * 20)
    fmt, text = parse_paper_file(data, "whatever.doc")  # 扩展名骗人也不影响：按魔数走
    assert fmt == "pdf"
    assert "Attention Is All You Need" in text
    assert "Transformer" in text


def _make_docx(paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_parse_docx_roundtrip_with_table():
    data = _make_docx(["深度学习综述", "第一节介绍背景。"])
    fmt, text = parse_paper_file(data, "paper.docx")
    assert fmt == "docx"
    assert "深度学习综述" in text


def test_parse_fake_zip_rejected():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "not a word document")
    with pytest.raises(ValueError, match="Word 文档"):
        parse_paper_file(buf.getvalue(), "fake.docx")


def test_parse_empty_pdf_text_rejected():
    import fitz

    doc = fitz.open()  # 无任何文本的空 PDF（模拟扫描版）
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    with pytest.raises(ValueError, match="未从文件中提取到文本"):
        parse_paper_file(data, "scan.pdf")


# ---------- 入库与提炼链路 ----------

class FakeLlm:
    model = "test-chat-model"

    def __init__(self):
        self.fulltexts = []

    def digest_with_tokens(self, title, abstract, fulltext=None, figure_notes=None):
        self.fulltexts.append(fulltext)
        return {
            "one_liner": "一句话",
            "outline": [{"section": "引言", "summary": "背景"}],
            "key_points": [{"point": "要点", "detail": "细节"}],
            "implementation": "实现",
            "principle": "原理",
            "limitations": "局限",
            "difficulty": 2,
        }, 800

    def embed(self, text):
        return [0.5] * 1024


def _pipeline(engine, llm):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    pipeline = Pipeline(
        session_factory=factory,
        llm=llm,
        state=RunState(),
        fetcher=lambda categories, lookback_days, max_results: [],
        fulltext_fetcher=lambda url: None,
        max_workers=1,
    )
    return pipeline, factory


def test_ingest_upload_then_redigest_uses_local_fulltext():
    """上传入库 → 单篇提炼：全文来自 local_fulltext，不再抓 html_url。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    llm = FakeLlm()
    pipeline, factory = _pipeline(engine, llm)

    paper_id, title = pipeline.ingest_upload(
        "我的论文.pdf", "Attention Is All You Need\n正文" * 100, "uploads/20260916/x.pdf"
    )
    with factory() as session:
        paper = session.get(Paper, paper_id)
        assert paper.origin == "upload"
        assert paper.arxiv_id.startswith("up-") and len(paper.arxiv_id) <= 32
        assert paper.status == "fetched"
        assert paper.title == "Attention Is All You Need"

    assert pipeline.redigest(paper_id) is True
    with factory() as session:
        paper = session.get(Paper, paper_id)
        assert paper.status == "digested"
        assert paper.digest.source_type == "fulltext"
    assert llm.fulltexts == ["Attention Is All You Need\n正文" * 100]


def test_ingest_upload_succeeds_while_pipeline_busy():
    """忙时降级：管线被占也不影响上传入库（论文保持待提炼，稍后补跑）。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    pipeline, factory = _pipeline(engine, FakeLlm())

    acquired = pipeline._mutex.acquire(blocking=False)  # 模拟批次占锁
    assert acquired
    try:
        paper_id, _ = pipeline.ingest_upload("busy.pdf", "标题\n正文内容", "uploads/x.pdf")
    finally:
        pipeline._mutex.release()

    with factory() as session:
        paper = session.get(Paper, paper_id)
        assert paper.status == "fetched"  # 未提炼，但不丢

    assert pipeline.redigest(paper_id) is True  # 解锁后补跑成功
