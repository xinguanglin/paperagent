from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    arxiv_id: Mapped[str] = mapped_column(String(32), unique=True)
    version: Mapped[str] = mapped_column(String(8), default="v1")
    title: Mapped[str] = mapped_column(String(512))
    authors: Mapped[str | None] = mapped_column(String(1024))
    abstract: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[str | None] = mapped_column(String(255))
    pdf_url: Mapped[str | None] = mapped_column(String(512))
    html_url: Mapped[str | None] = mapped_column(String(512))
    score: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        Enum("fetched", "digested", "failed", name="paper_status"), default="fetched"
    )
    fail_reason: Mapped[str | None] = mapped_column(String(512))
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # ---- 手工上传（origin=upload）与全文缓存 ----
    origin: Mapped[str] = mapped_column(String(16), default="fetch")  # fetch / upload
    local_file: Mapped[str | None] = mapped_column(String(512))  # 相对项目根的存储路径
    # 中文全文远超 MySQL TEXT 的 64KB 字节上限，MySQL 方言用 MEDIUMTEXT。
    # 上传论文存解析后的全文；在线检索/arXiv 论文在首次提炼时缓存抓到的 HTML 全文，重跑不再外抓
    local_fulltext: Mapped[str | None] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"))
    # 原文图片清单与识图结果 [{idx,url,path,caption,mime,bytes,status,description,...}]
    # 挂在论文不挂 digest：识图结果跨重跑复用（视觉调用是真实成本）
    figures: Mapped[list | None] = mapped_column(JSON)

    digest: Mapped[Digest | None] = relationship(
        back_populates="paper", uselist=False, cascade="all, delete-orphan"
    )


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), unique=True
    )
    one_liner: Mapped[str | None] = mapped_column(String(1024))
    outline: Mapped[list | None] = mapped_column(JSON)
    key_points: Mapped[list | None] = mapped_column(JSON)
    # 提炼生成的 mermaid 图 [{type: flow|architecture, caption, mermaid}]，仅摘要无全文时为 NULL
    diagrams: Mapped[list | None] = mapped_column(JSON)
    implementation: Mapped[str | None] = mapped_column(Text, comment="实现过程拆解")
    principle: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[str | None] = mapped_column(Text)
    difficulty: Mapped[int | None] = mapped_column(SmallInteger)
    truncated: Mapped[bool] = mapped_column(default=False, comment="全文是否超限截断")
    source_type: Mapped[str] = mapped_column(
        Enum("fulltext", "abstract", name="digest_source_type"), default="fulltext"
    )
    model: Mapped[str | None] = mapped_column(String(64))
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    digested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    paper: Mapped[Paper] = relationship(back_populates="digest")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    trigger_type: Mapped[str] = mapped_column(Enum("scheduled", "manual", name="run_trigger"))
    status: Mapped[str] = mapped_column(
        Enum("running", "success", "partial", "failed", name="run_status"), default="running"
    )
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    categories: Mapped[str] = mapped_column(String(255), default="cs.AI,cs.CL,cs.LG")
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list | None] = mapped_column(JSON, comment="论文来源优先级列表，如 [arxiv, openalex]")
    top_n: Mapped[int] = mapped_column(Integer, default=20)
    cron_expr: Mapped[str] = mapped_column(String(32), default="0 8 * * *")
    chat_model: Mapped[str] = mapped_column(String(64), default="", comment="对话/解读模型；空 = 用 config.yaml 的 chat_model")
    # 视觉识图模型；空 = 关闭识图（回退 config.yaml 默认）
    vision_model: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
