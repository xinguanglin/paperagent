from datetime import datetime, timedelta
import threading
import time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import init_db
from app.models import Digest, Paper, Run, Setting
from app.pipeline import DIGEST_ATTEMPTS, Pipeline, PipelineBusy, RunState

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine


def make_raw(arxiv_id: str, title: str, hours_ago: float = 1.0):
    from app.arxiv_client import RawPaper

    return RawPaper(
        arxiv_id=arxiv_id,
        version="v1",
        title=title,
        authors=["作者A", "作者B"],
        abstract="关于 LLM agent 的研究",
        categories=["cs.AI"],
        pdf_url=f"http://arxiv.org/pdf/{arxiv_id}",
        html_url=f"https://arxiv.org/html/{arxiv_id}",
        published_at=datetime.utcnow() - timedelta(hours=hours_ago),
    )


VALID_DIGEST = {
    "one_liner": "一句话总结",
    "outline": [{"section": "引言", "summary": "背景"}],
    "key_points": [{"point": "要点", "detail": "细节"}],
    "implementation": "实现过程",
    "principle": "原理拆解",
    "limitations": "局限",
    "difficulty": 2,
}


class FakeLlm:
    """可配置失败次数的假客户端，记录 digest 收到的 fulltext。"""

    model = "test-chat-model"

    def __init__(self, fail_calls: int = 0):
        self.fail_calls = fail_calls  # 前 N 次 digest 调用抛异常
        self.digest_calls = 0
        self.embed_calls = 0
        self.fulltexts: list[str | None] = []

    def digest_with_tokens(self, title, abstract, fulltext=None, figure_notes=None):
        self.digest_calls += 1
        self.fulltexts.append(fulltext)
        if self.digest_calls <= self.fail_calls:
            raise RuntimeError("模拟 GLM 故障")
        return dict(VALID_DIGEST), 800

    def embed(self, text):
        self.embed_calls += 1
        return [0.5] * 1024


def build_pipeline(engine, llm, raws, fulltext=("模拟正文" * 200, False)):
    # 兼容旧二元组注入 (text, truncated)，补齐图片清单维
    if isinstance(fulltext, tuple) and len(fulltext) == 2:
        fulltext = (*fulltext, [])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    state = RunState()
    pipeline = Pipeline(
        session_factory=factory,
        llm=llm,
        state=state,
        fetcher=lambda categories, lookback_days, max_results: list(raws),
        fulltext_fetcher=lambda url: fulltext,
        max_workers=1,  # sqlite 测试库串行执行
    )
    return pipeline, state, factory


def test_full_batch_success_and_dedup():
    engine = make_engine()
    raws = [make_raw("2609.00001", "LLM Agent Scaling"), make_raw("2609.00002", "RLHF Reward Hacking")]
    llm = FakeLlm()
    pipeline, state, factory = build_pipeline(engine, llm, raws)

    run_id = pipeline.run_batch("manual")

    with factory() as session:
        papers = session.execute(select(Paper)).scalars().all()
        assert len(papers) == 2
        assert all(p.status == "digested" for p in papers)
        assert all(p.embedding is not None and len(p.embedding) == 1024 * 4 for p in papers)
        digests = session.execute(select(Digest)).scalars().all()
        assert len(digests) == 2
        assert digests[0].source_type == "fulltext"
        assert digests[0].model == "test-chat-model"
        run = session.get(Run, run_id)
        assert run.status == "success"
        assert run.fetched_count == 2
        assert run.succeeded_count == 2
        assert run.failed_count == 0
        assert run.total_tokens == 1600
    assert state.running is False
    assert state.done == 2 and state.total == 2

    # 全文确实传给了 GLM（回归：曾有抓而未传的 bug）
    assert all(ft for ft in llm.fulltexts)

    # 第二次批次：同批论文已存在 → 去重，不再入库也不再提炼
    run_id2 = pipeline.run_batch("scheduled")
    with factory() as session:
        assert len(session.execute(select(Paper)).scalars().all()) == 2
        run2 = session.get(Run, run_id2)
        assert run2.fetched_count == 0
        assert run2.status == "success"


def test_batch_without_api_key_marks_failed():
    engine = make_engine()
    raws = [make_raw("2609.00003", "Multimodal Pretraining")]
    pipeline, state, factory = build_pipeline(engine, None, raws)

    run_id = pipeline.run_batch("manual")

    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        assert paper.status == "failed"
        assert "未配置 LLM API Key" in paper.fail_reason
        run = session.get(Run, run_id)
        assert run.status == "failed"
        assert run.error_summary is not None


def test_digest_retry_count_then_failed_no_auto_requeue():
    """GLM 网络重试 = DIGEST_ATTEMPTS 次；全失败入库 failed；批次隔离语义下
    下一批不再自动补跑（需单篇重新提炼）。"""
    engine = make_engine()
    raws = [make_raw("2609.00004", "LLM Reasoning")]
    llm = FakeLlm(fail_calls=99)  # 永远失败
    pipeline, state, factory = build_pipeline(engine, llm, raws)

    run_id = pipeline.run_batch("manual")

    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        assert paper.status == "failed"
        run = session.get(Run, run_id)
        assert run.status == "failed"
        assert "模拟 GLM 故障" in (run.error_summary or "")
    assert llm.digest_calls == DIGEST_ATTEMPTS  # 首次 + 重试 2 次

    # 下一批：无新增论文 → 队列为空，failed 的旧论文不被带入
    llm.fail_calls = 0
    run_id2 = pipeline.run_batch("scheduled")
    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        assert paper.status == "failed"  # 保持 failed，不再自动补跑
        run2 = session.get(Run, run_id2)
        assert run2.fetched_count == 0  # 无新增（去重）
        assert run2.succeeded_count == 0
        assert run2.status == "success"
    assert llm.digest_calls == DIGEST_ATTEMPTS  # 没有新的 GLM 调用

    # 单篇重新提炼仍可完成它（手动补账路径）
    assert pipeline.redigest(paper.id) is True
    with factory() as session:
        assert session.get(Paper, paper.id).status == "digested"


def test_fulltext_fallback_to_abstract():
    engine = make_engine()
    raws = [make_raw("2609.00006", "Sparse Attention")]
    llm = FakeLlm()
    pipeline, state, factory = build_pipeline(engine, llm, raws, fulltext=None)
    pipeline.run_batch("manual")

    with factory() as session:
        digest = session.execute(select(Digest)).scalars().one()
        assert digest.source_type == "abstract"
    assert llm.fulltexts == [None]


def test_truncated_flag_persisted():
    engine = make_engine()
    raws = [make_raw("2609.00008", "Very Long Paper")]
    pipeline, state, factory = build_pipeline(
        engine, FakeLlm(), raws, fulltext=("x" * 500, True)
    )

    pipeline.run_batch("manual")

    with factory() as session:
        digest = session.execute(select(Digest)).scalars().one()
        assert digest.truncated is True


def test_diagrams_persisted_and_absent_is_none():
    """带 diagrams 的解读入库为数组；缺失 diagrams（老模型输出/纯理论论文）时入库 NULL。"""

    class SometimesDiagrams(FakeLlm):
        def digest_with_tokens(self, title, abstract, fulltext=None, figure_notes=None):
            self.digest_calls += 1
            self.fulltexts.append(fulltext)
            data = dict(VALID_DIGEST)
            if "With Diagram" in title:
                data["diagrams"] = [
                    {
                        "type": "flow",
                        "caption": "整体流程",
                        "mermaid": 'flowchart TD\n  A["输入"] --> B["输出"]',
                    }
                ]
            return data, 800

    engine = make_engine()
    raws = [make_raw("2609.00011", "With Diagram"), make_raw("2609.00012", "No Diagram")]
    pipeline, state, factory = build_pipeline(engine, SometimesDiagrams(), raws)

    pipeline.run_batch("manual")

    with factory() as session:
        digests = {
            d.paper_id: d for d in session.execute(select(Digest)).scalars().all()
        }
        papers = session.execute(select(Paper)).scalars().all()
        by_title = {p.title: digests[p.id] for p in papers}
        assert by_title["With Diagram"].diagrams == [
            {
                "type": "flow",
                "caption": "整体流程",
                "mermaid": 'flowchart TD\n  A["输入"] --> B["输出"]',
            }
        ]
        assert by_title["No Diagram"].diagrams is None


def test_arxiv_fulltext_cached_for_redigest():
    """在线检索/arXiv 论文首次提炼抓到的全文落库，重跑直接复用、不再请求外网。"""
    calls = {"n": 0}

    def counting_fetcher(url):
        calls["n"] += 1
        return ("模拟正文" * 300, False, [])

    engine = make_engine()
    raws = [make_raw("2609.00021", "Cached Fulltext Paper")]
    pipeline, state, factory = build_pipeline(engine, FakeLlm(), raws)
    pipeline._fulltext_fetcher = counting_fetcher

    pipeline.run_batch("manual")
    assert calls["n"] == 1
    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        assert paper.local_fulltext == "模拟正文" * 300

    assert pipeline.redigest(paper.id) is True
    assert calls["n"] == 1  # 重跑复用缓存，没有再抓

    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        assert paper.status == "digested"


def test_version_update_without_duplicate_ingest():
    engine = make_engine()
    raw_v1 = make_raw("2609.00009", "Paper V1")
    llm = FakeLlm()
    pipeline, state, factory = build_pipeline(engine, llm, [raw_v1])
    pipeline.run_batch("manual")

    # 同一论文出现 v2：只更新 version，不重复入库、不重新提炼
    raw_v2 = make_raw("2609.00009", "Paper V1")
    raw_v2.version = "v2"
    pipeline2, _, _ = build_pipeline(engine, llm, [raw_v2])
    pipeline2.run_batch("manual")

    with factory() as session:
        papers = session.execute(select(Paper)).scalars().all()
        assert len(papers) == 1
        assert papers[0].version == "v2"
        assert papers[0].status == "digested"


def test_busy_batch_rejected_by_mutex():
    engine = make_engine()
    raws = [make_raw("2609.00007", "LLM Agents")]
    pipeline, state, factory = build_pipeline(engine, FakeLlm(), raws)
    acquired = pipeline._mutex.acquire(blocking=False)  # 模拟批次占锁
    assert acquired
    with pytest.raises(PipelineBusy):
        pipeline.run_batch("manual")
    pipeline._mutex.release()


def test_redigest_rejected_while_batch_running():
    engine = make_engine()
    raws = [make_raw("2609.00010", "RAG Survey")]
    pipeline, state, factory = build_pipeline(engine, FakeLlm(), raws)
    pipeline.run_batch("manual")  # 先正常跑完入库

    acquired = pipeline._mutex.acquire(blocking=False)
    assert acquired
    assert pipeline.redigest(1) is False  # 互斥占用：入队而非拒绝
    assert len(pipeline._pending_singles) == 1
    pipeline._mutex.release()
    pipeline._drain_pending()  # 模拟持锁者释放后的自动消化

    # 队列消化后论文已重提成功
    with factory() as session:
        assert session.get(Paper, 1).status == "digested"


def test_fetch_failure_fails_run():
    engine = make_engine()

    def broken_fetcher(categories, lookback_days, max_results):
        raise RuntimeError("arXiv 网络不可达")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    state = RunState()
    pipeline = Pipeline(
        session_factory=factory,
        llm=FakeLlm(),
        state=state,
        fetcher=broken_fetcher,
        max_workers=1,
    )
    with pytest.raises(RuntimeError, match="arXiv"):
        pipeline.run_batch("scheduled")
    with factory() as session:
        run = session.execute(select(Run)).scalars().one()
        assert run.status == "failed"
        assert "arXiv 网络不可达" in run.error_summary
    assert state.running is False


class StopDuringSecondLlm(FakeLlm):
    """第二篇开始提炼时请求停止，模拟用户在批次中途点「停止」。

    max_workers=1 时 worker 串行：第一篇已完整保存，第二篇在提炼中收到停止。
    """

    def __init__(self):
        super().__init__()
        self.state = None  # build_pipeline 后注入
        self.armed = True  # 一次性触发，避免影响后续单篇重跑

    def digest_with_tokens(self, title, abstract, fulltext=None, figure_notes=None):
        if self.armed and self.digest_calls >= 1 and self.state is not None:
            self.state.request_stop()
            self.armed = False
        return super().digest_with_tokens(title, abstract, fulltext)


def test_stop_mid_batch_keeps_done_and_discards_inflight():
    """硬停止：已保存的解读保留；提炼中被停止的论文结果丢弃（保持 fetched），批次秒级收尾。"""
    engine = make_engine()
    raws = [make_raw("2609.10001", "Paper One"), make_raw("2609.10002", "Paper Two")]
    llm = StopDuringSecondLlm()
    pipeline, state, factory = build_pipeline(engine, llm, raws)
    llm.state = state

    run_id = pipeline.run_batch("manual")

    with factory() as session:
        papers = session.execute(select(Paper)).scalars().all()
        assert len(papers) == 2  # 检索入库照常
        by_status = sorted(p.status for p in papers)
        assert by_status == ["digested", "fetched"]  # 已保存的保留，进行中的丢弃
        done = next(p for p in papers if p.status == "digested")
        assert done.digest is not None
        run = session.get(Run, run_id)
        assert run.status == "partial"
        assert run.succeeded_count == 1
        assert "用户停止" in run.error_summary
    assert state.running is False


def test_stop_flag_cleared_on_next_batch():
    """停止标志不能泄漏到下一批次；批次隔离下剩余论文由单篇重跑补完。"""
    engine = make_engine()
    raws = [make_raw("2609.20001", "Paper A"), make_raw("2609.20002", "Paper B")]
    llm = StopDuringSecondLlm()
    pipeline, state, factory = build_pipeline(engine, llm, raws)
    llm.state = state

    pipeline.run_batch("manual")  # 第一批：1 篇完成、1 篇被停止跳过
    run_id2 = pipeline.run_batch("manual")  # 第二批：标志已清；无新增 → 队列为空

    with factory() as session:
        papers = {p.title: p for p in session.execute(select(Paper)).scalars().all()}
        done = next(p for p in papers.values() if p.status == "digested")
        pending = next(p for p in papers.values() if p.status == "fetched")
        run2 = session.get(Run, run_id2)
        assert run2.status == "success"
        assert run2.succeeded_count == 0  # 第二批没带旧账
    assert llm.digest_calls == 2  # 停止时在飞那篇的 GLM 调用已发出（token 已花），结果被丢弃

    # 停止标志未泄漏：单篇重跑正常完成剩余论文
    assert pipeline.redigest(pending.id) is True
    with factory() as session:
        assert session.get(Paper, pending.id).status == "digested"
    assert llm.digest_calls == 3


def test_stop_then_immediate_redigest_of_inflight_paper_is_safe(tmp_path):
    """回归：停止批次后互斥锁立即释放、在飞 worker 仍在跑。此时立刻单篇重跑
    同一篇必须被 inflight 守卫拦下；worker 用本批停止事件快照丢弃结果，
    不得出现并发写库撞 uk_paper 唯一键或污染新任务状态。"""
    # 文件库 + 默认连接池：两个 worker 各自独立连接，真实并发（内存 StaticPool 单连接不支持）
    engine = create_engine(
        f"sqlite:///{tmp_path.as_posix()}/race.db",
        connect_args={"check_same_thread": False},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    state = RunState()
    release = threading.Event()

    class SlowInflightLlm(FakeLlm):
        def digest_with_tokens(self, title, abstract, fulltext=None, figure_notes=None):
            state.request_stop()  # 两个 worker 都进入 GLM 调用时停止批次
            release.wait(timeout=10)  # 卡住：批次收尾返回后 worker 仍在飞
            return super().digest_with_tokens(title, abstract, fulltext)

    raws = [make_raw("2609.80001", "Inflight A"), make_raw("2609.80002", "Inflight B")]
    llm = SlowInflightLlm()
    pipeline = Pipeline(
        session_factory=factory,
        llm=llm,
        state=state,
        fetcher=lambda categories, lookback_days, max_results: list(raws),
        fulltext_fetcher=lambda url: ("模拟正文" * 200, False, []),
        max_workers=2,
    )

    run_id = pipeline.run_batch("manual")  # 停止后秒级返回；此刻两个 worker 仍阻塞在 GLM 调用

    with pytest.raises(PipelineBusy, match="收尾"):
        pipeline.redigest(1)  # 立刻重跑在飞论文 → 必须被拦，而不是并发写库
    assert state.single_active is False  # 被拒的请求不得留下登记

    release.set()  # 放行在飞 worker
    deadline = time.time() + 10
    while pipeline._inflight and time.time() < deadline:
        time.sleep(0.1)
    assert not pipeline._inflight

    with factory() as session:
        papers = session.execute(select(Paper)).scalars().all()
        assert all(p.status == "fetched" for p in papers)  # 在飞结果按本批停止事件丢弃
        assert session.execute(select(Digest)).scalars().first() is None
        run = session.get(Run, run_id)
        assert run.status == "failed"  # 0 篇完成的停止批次
        assert "用户停止" in (run.error_summary or "")
    assert llm.digest_calls == 2

    assert pipeline.redigest(1) is True  # 收尾完成后重跑正常
    with factory() as session:
        assert session.get(Paper, 1).status == "digested"
    assert llm.digest_calls == 3


def test_redigest_mutual_exclusion_and_single_flag():
    """单篇重跑期间：状态登记可见、另一单篇重跑入队等待、结束后登记清除并自动消化队列。"""
    engine = make_engine()
    raws = [make_raw("2609.81001", "Solo A"), make_raw("2609.81002", "Solo B")]
    llm = FakeLlm()
    pipeline, state, factory = build_pipeline(engine, llm, raws)
    pipeline.run_batch("manual")
    with factory() as session:
        ids = sorted(p.id for p in session.execute(select(Paper)).scalars().all())

    entered = threading.Event()
    release = threading.Event()

    class BlockingLlm(FakeLlm):
        def digest_with_tokens(self, title, abstract, fulltext=None, figure_notes=None):
            entered.set()
            release.wait(timeout=10)
            return super().digest_with_tokens(title, abstract, fulltext)

    pipeline.update_llm(BlockingLlm())
    result: dict = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", pipeline.redigest(ids[0])))
    t.start()
    assert entered.wait(3)
    assert state.single_active is True
    assert state.single_paper == ids[0]
    assert pipeline.redigest(ids[1]) is False  # 与进行中的单篇重跑互斥：入队而非拒绝
    assert len(pipeline._pending_singles) == 1
    release.set()
    t.join(10)
    assert result["ok"] is True
    assert state.single_active is False
    # 第一篇结束后自动消化队列：第二篇无需再手动触发
    with factory() as session:
        assert session.get(Paper, ids[1]).status == "digested"
    assert pipeline._pending_singles == []


def _make_source_pipeline(engine, arxiv_raws, openalex_raws, sources, top_n=50, llm=None):
    """注入两个假源构造管线：settings.sources 控制优先级顺序。"""
    calls = {"arxiv": [], "openalex": []}

    def fake_arxiv(categories, lookback_days=2, max_results=100):
        calls["arxiv"].append(max_results)
        return list(arxiv_raws)

    def fake_openalex(categories, lookback_days=2, max_results=100):
        calls["openalex"].append(max_results)
        return list(openalex_raws)

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    state = RunState()
    pipeline = Pipeline(
        session_factory=factory,
        llm=llm if llm is not None else FakeLlm(),
        state=state,
        source_fetchers={"arxiv": fake_arxiv, "openalex": fake_openalex},
        max_workers=1,
    )
    with factory() as session:
        s = session.get(Setting, 1)
        s.sources = sources
        s.top_n = top_n
        session.commit()
    return pipeline, calls, factory


def test_multi_source_backfill_and_dedup():
    """arXiv 只有 15 篇时自动从 OpenAlex 补齐缺口；跨源同 ID 去重；合并后统一打分取 Top N。"""
    engine = make_engine()
    arxiv = [make_raw(f"2609.3{i:03d}", f"LLM Paper {i}") for i in range(15)]
    overlap = [make_raw("2609.3000", "重复论文", hours_ago=2)]  # 与 arXiv 重复
    extra = [make_raw(f"2609.4{i:03d}", f"Agent Paper {i}") for i in range(10)]
    pipeline, calls, factory = _make_source_pipeline(
        engine, arxiv_raws=arxiv + overlap, openalex_raws=overlap + extra,
        sources=["arxiv", "openalex"],
    )

    run_id = pipeline.run_batch("manual")

    # 补齐：arxiv 请求 100 拿到 16 条（批内 1 条重复去重后 15），openalex 按缺口补 85
    assert calls["arxiv"] == [100]
    assert calls["openalex"] == [85]
    with factory() as session:
        papers = session.execute(select(Paper)).scalars().all()
        ids = {p.arxiv_id for p in papers}
        assert len(papers) == 25  # 16 + 10（重复的 2609.3000 去重）
        assert "2609.3000" in ids
        run = session.get(Run, run_id)
        assert run.fetched_count == 25
        assert run.status == "success"


def test_multi_source_single_selection_skips_others():
    """只选 arXiv 一个源：OpenAlex 完全不被调用。"""
    engine = make_engine()
    arxiv = [make_raw("2609.5001", "Solo Source")]
    pipeline, calls, factory = _make_source_pipeline(
        engine, arxiv_raws=arxiv, openalex_raws=[make_raw("2609.5002", "不应出现")],
        sources=["arxiv"],
    )
    pipeline.run_batch("manual")
    assert calls["openalex"] == []
    with factory() as session:
        papers = session.execute(select(Paper)).scalars().all()
        assert [p.arxiv_id for p in papers] == ["2609.5001"]


def test_multi_source_priority_order_and_full_first_source():
    """高优先级源已抓满时不再调用低优先级源。"""
    engine = make_engine()
    arxiv = [make_raw(f"2609.6{i:03d}", f"Paper {i}") for i in range(100)]
    pipeline, calls, factory = _make_source_pipeline(
        engine, arxiv_raws=arxiv, openalex_raws=[make_raw("2609.6999", "不应出现")],
        sources=["arxiv", "openalex"],
    )
    pipeline.run_batch("manual")
    assert calls["openalex"] == []  # arXiv 已满 100，无需补齐


def test_multi_source_failure_falls_back():
    """高优先级源抛错时用低优先级源兜底，不中断批次。"""
    engine = make_engine()

    def broken_arxiv(categories, lookback_days=2, max_results=100):
        raise RuntimeError("模拟 arXiv 429")

    def fake_openalex(categories, lookback_days=2, max_results=100):
        return [make_raw("2609.7001", "Fallback Paper")]

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    state = RunState()
    pipeline = Pipeline(
        session_factory=factory,
        llm=FakeLlm(),
        state=state,
        source_fetchers={"arxiv": broken_arxiv, "openalex": fake_openalex},
        max_workers=1,
    )
    with factory() as session:
        s = session.get(Setting, 1)
        s.sources = ["arxiv", "openalex"]
        session.commit()

    run_id = pipeline.run_batch("manual")
    with factory() as session:
        papers = session.execute(select(Paper)).scalars().all()
        assert [p.arxiv_id for p in papers] == ["2609.7001"]
        run = session.get(Run, run_id)
        assert run.status == "success"


def test_single_digest_queue_drains_after_mutex_release():
    """单篇提炼遇互斥占用时入队而非丢弃；锁释放后自动依次消化队列。"""
    engine = make_engine()
    raws = [make_raw("2609.90001", "Queued Paper A"), make_raw("2609.90002", "Queued Paper B")]
    llm = FakeLlm()
    pipeline, state, factory = build_pipeline(engine, llm, raws)
    pipeline.run_batch("manual")  # 两篇先正常入库并提炼
    with factory() as session:
        for p in session.execute(select(Paper)).scalars().all():
            p.status = "failed"  # 模拟待重提状态
        session.commit()
        ids = session.execute(select(Paper.id).order_by(Paper.id)).scalars().all()

    # 模拟锁被批次占用：手动持有互斥锁
    pipeline._mutex.acquire()
    try:
        assert pipeline.redigest(ids[0]) is False  # 入队，返回 False
        assert pipeline.redigest(ids[1]) is False
        assert len(pipeline._pending_singles) == 2
        with factory() as session:
            assert session.get(Paper, ids[0]).status == "failed"  # 尚未执行
    finally:
        pipeline._mutex.release()
    pipeline._drain_pending()  # 模拟持锁者释放后的自动消化（生产中 run_batch/_run_single 释放时必触发）

    with factory() as session:
        assert session.get(Paper, ids[0]).status == "digested"
        assert session.get(Paper, ids[1]).status == "digested"
    assert pipeline._pending_singles == []
    assert llm.digest_calls == 4  # 批次 2 次 + 队列补跑 2 次


def test_single_digest_queue_dedup():
    """同篇重复排队只保留一份。"""
    engine = make_engine()
    raws = [make_raw("2609.91001", "Dedup Paper")]
    llm = FakeLlm()
    pipeline, state, factory = build_pipeline(engine, llm, raws)
    pipeline.run_batch("manual")
    with factory() as session:
        pid = session.execute(select(Paper.id)).scalar_one()

    pipeline._mutex.acquire()
    try:
        pipeline.redigest(pid)
        pipeline.redigest(pid)  # 重复入队
        assert len(pipeline._pending_singles) == 1
    finally:
        pipeline._mutex.release()
    pipeline._drain_pending()
    assert llm.digest_calls == 2  # 只补跑一次


# ---------- 多模态识图编排 ----------

def _tiny_png_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(buf, format="PNG")
    return buf.getvalue()


class VisionFake(FakeLlm):
    """带视觉档的假客户端：记录识图调用与 digest 收到的注入段。"""

    vision_model = "test-vision-model"

    def __init__(self):
        super().__init__()
        self.described: list[str] = []
        self.notes_seen: list[str | None] = []

    def describe_image(self, path, title, caption):
        self.described.append(path)
        return "架构图：组件A→组件B"

    def digest_with_tokens(self, title, abstract, fulltext=None, figure_notes=None):
        self.digest_calls += 1
        self.fulltexts.append(fulltext)
        self.notes_seen.append(figure_notes)
        return dict(VALID_DIGEST), 800


def test_vision_describe_flow_and_cache(tmp_path, monkeypatch):
    """有图论文：下载→识图→注入解读；重跑复用缓存转述，不再识图、不再下载。"""
    from types import SimpleNamespace

    png = _tiny_png_bytes()
    dl_calls = {"n": 0}

    def counting_get(url):
        dl_calls["n"] += 1
        return SimpleNamespace(status_code=200, content=png, url=url)

    monkeypatch.setattr("app.net.http_get", counting_get)
    figs = [
        {"idx": 1, "url": "https://arxiv.org/html/2609.00031/x1.png", "caption": "Figure 1: architecture", "status": "pending"},
    ]
    engine = make_engine()
    raws = [make_raw("2609.00031", "Vision Paper")]
    llm = VisionFake()
    pipeline, state, factory = build_pipeline(engine, llm, raws, fulltext=("模拟正文" * 200, False, figs))
    pipeline._figures_dir = str(tmp_path)

    pipeline.run_batch("manual")

    assert llm.described and llm.notes_seen[0] and "图1" in llm.notes_seen[0]
    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        assert paper.figures[0]["status"] == "described"
        assert paper.figures[0]["description"] == "架构图：组件A→组件B"
        paper_id = paper.id

    described_before = len(llm.described)
    pipeline.redigest(paper_id)
    assert len(llm.described) == described_before  # 缓存复用，不再识图
    assert dl_calls["n"] == 1  # 文件在，不再下载
    assert llm.notes_seen[-1] and "图1" in llm.notes_seen[-1]


def test_vision_disabled_keeps_plain_pipeline(tmp_path):
    """vision_model 未配置：有图论文也不识图、注入段为 None（行为与纯文本管线一致）。"""
    figs = [{"idx": 1, "url": "https://arxiv.org/html/2609.00032/x1.png", "caption": "Figure 1", "status": "pending"}]
    engine = make_engine()
    raws = [make_raw("2609.00032", "No Vision Paper")]
    llm = VisionFake()
    llm.vision_model = None
    pipeline, state, factory = build_pipeline(engine, llm, raws, fulltext=("模拟正文" * 200, False, figs))
    pipeline._figures_dir = str(tmp_path)

    pipeline.run_batch("manual")

    assert llm.described == []
    assert llm.notes_seen == [None]
    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        assert paper.figures and paper.figures[0]["status"] == "pending"  # 清单已存，等识图开启


def test_figure_budget_stops_describe(tmp_path, monkeypatch):
    """识图总预算耗尽：剩余图记 failed，解读照常完成（宁可少转述不拖死锁）。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.net.http_get",
        lambda url: SimpleNamespace(status_code=200, content=_tiny_png_bytes(), url=url),
    )
    figs = [
        {"idx": i, "url": f"https://arxiv.org/html/2609.00033/x{i}.png", "caption": f"Figure {i}", "status": "pending"}
        for i in range(1, 4)
    ]

    class SlowVision(VisionFake):
        def describe_image(self, path, title, caption):
            time.sleep(0.25)
            return super().describe_image(path, title, caption)

    engine = make_engine()
    raws = [make_raw("2609.00033", "Budget Paper")]
    llm = SlowVision()
    pipeline, state, factory = build_pipeline(engine, llm, raws, fulltext=("模拟正文" * 200, False, figs))
    pipeline._figures_dir = str(tmp_path)
    pipeline._figure_budget = 0.2  # 只够第一张

    pipeline.run_batch("manual")

    assert len(llm.described) == 1  # 预算只允许 1 张
    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        statuses = [f["status"] for f in paper.figures]
        assert statuses.count("described") == 1
        assert statuses.count("failed") >= 1  # 预算外图标记失败
        assert paper.status == "digested"  # 解读不受影响
    assert llm.notes_seen[0] is not None  # 已转述的照常注入


def test_vision_max_figures_quota(tmp_path, monkeypatch):
    """vision_max_figures 截断：12 张图配额 3 → 只识 3 张，其余保持 pending 等下轮。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.net.http_get",
        lambda url: SimpleNamespace(status_code=200, content=_tiny_png_bytes(), url=url),
    )
    figs = [
        {"idx": i, "url": f"https://arxiv.org/html/2609.00034/x{i}.png", "caption": f"Figure {i}", "status": "pending"}
        for i in range(1, 13)
    ]
    engine = make_engine()
    raws = [make_raw("2609.00034", "Quota Paper")]
    llm = VisionFake()
    pipeline, state, factory = build_pipeline(engine, llm, raws, fulltext=("模拟正文" * 200, False, figs))
    pipeline._figures_dir = str(tmp_path)
    pipeline._vision_max_figures = 3

    pipeline.run_batch("manual")

    assert len(llm.described) == 3
    with factory() as session:
        paper = session.execute(select(Paper)).scalars().one()
        statuses = [f["status"] for f in paper.figures]
        assert statuses.count("described") == 3
        assert statuses.count("pending") == 9  # 未及配额的图留给下轮
        assert paper.status == "digested"
