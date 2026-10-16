from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.arxiv_client import RawPaper, fetch_recent
from app.html_fetcher import fetch_fulltext
from app.models import Digest, Paper, Run, Setting
from app.scorer import score_paper
from app.upload_parser import sanitize_filename
from app.llm_client import MAX_FULLTEXT_CHARS, LlmClient

logger = logging.getLogger(__name__)

DIGEST_ATTEMPTS = 3  # 首次 + 重试 2 次
FIGURE_PHASE_BUDGET = 180.0  # 识图阶段总预算（秒）：下载+识图，超时停剩余（设计 §6.1）
# caption 加权排序的关键词（评审 P1-7）：架构/实验图优先于装饰图
_FIGURE_PRIORITY = ("figure", "architecture", "framework", "plot", "curve", "chart", "diagram", "table")


class PipelineBusy(RuntimeError):
    pass


class RunState:
    """进程内批次状态，供 /api/runs/current 轮询。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.run_id: int | None = None
        self.done = 0
        self.total = 0
        self.note = ""
        self._stop_event = threading.Event()
        self.single_active = False  # 单篇重跑进行中（供入口 409 判断，避免"假启动"）
        self.single_paper: int | None = None

    def request_stop(self) -> None:
        """请求停止当前批次：已完成并提交的解读保留，未开始的论文跳过（保持原状态）。"""
        with self._lock:
            self._stop_event.set()

    def clear_stop(self) -> None:
        with self._lock:
            self._stop_event = threading.Event()

    @property
    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_event.is_set()

    @property
    def stop_event(self) -> threading.Event:
        """当前批次的停止事件快照：worker 在提交前捕获，之后新批次复位标志不影响它。"""
        with self._lock:
            return self._stop_event

    def try_begin(self, run_id: int, total: int = 0, note: str = "") -> bool:
        """原子地检查-置位：成功拿到批次状态返回 True，已有批次在跑返回 False。"""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.run_id = run_id
            self.done = 0
            self.total = total
            self.note = note
            self._stop_event = threading.Event()  # 新批次开始时清掉上一批的停止标志
            return True

    def try_begin_single(self, paper_id: int) -> bool:
        """登记单篇重跑：已在重跑中返回 False（调用方应转为 PipelineBusy/409）。"""
        with self._lock:
            if self.single_active:
                return False
            self.single_active = True
            self.single_paper = paper_id
            return True

    def end_single(self) -> None:
        with self._lock:
            self.single_active = False
            self.single_paper = None

    def begin(self, run_id: int, total: int, note: str = "") -> None:
        """批次内刷新进度（不改变 running）。"""
        with self._lock:
            self.run_id = run_id
            self.done = 0
            self.total = total
            self.note = note

    def finish(self, note: str = "") -> None:
        with self._lock:
            self.running = False
            self.note = note

    def step(self) -> None:
        with self._lock:
            self.done += 1

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "run_id": self.run_id,
                "done": self.done,
                "total": self.total,
                "note": self.note,
                "single_active": self.single_active,
            }


class Pipeline:
    def __init__(
        self,
        session_factory: sessionmaker,
        llm: LlmClient | None,
        state: RunState,
        fetcher=fetch_recent,
        fulltext_fetcher=fetch_fulltext,
        lookback_days: int = 2,
        max_workers: int = 3,
        source: str = "auto",
        source_fetchers: dict | None = None,
        vision_max_figures: int = 10,
        figures_dir: str = "uploads/figures",
        figure_budget_seconds: float = FIGURE_PHASE_BUDGET,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._state = state
        self._fetcher = fetcher
        self._fulltext_fetcher = fulltext_fetcher
        self._lookback_days = lookback_days
        self._max_workers = max_workers
        self._source = source
        self._source_fetchers = source_fetchers  # 测试注入 {源名: fetcher}，完整接管多源行为
        self._vision_max_figures = vision_max_figures
        self._figures_dir = figures_dir
        self._figure_budget = figure_budget_seconds
        self._mutex = threading.Lock()  # 批次之间、批次与单篇重跑之间的真实互斥
        # 停止批次后锁立即释放，但在飞的 LLM worker 还会跑完收尾；
        # 记录它们正占用的论文 ID，单篇重跑据此拒绝"撞车"（见 redigest）
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()
        # 单篇提炼等待队列：互斥占用时入队，持锁者释放后自动消化（见 _drain_pending）
        self._pending_singles: list[tuple[int, bool]] = []
        self._queue_lock = threading.Lock()
        self._draining = False

    def update_llm(self, client: LlmClient | None) -> None:
        """设置页切换模型档位后热更新，无需重启。"""
        self._llm = client

    # ---------- 批次入口 ----------

    def run_batch(self, trigger: str) -> int:
        if not self._mutex.acquire(blocking=False):
            raise PipelineBusy("已有批次在运行，请等待完成后再触发")
        try:
            now = datetime.now()
            with self._sessions() as session:
                run = Run(trigger_type=trigger, status="running", started_at=now)
                session.add(run)
                session.commit()
                run_id = run.id
            logger.info("批次 #%s 开始（%s 触发）", run_id, trigger)
            if not self._state.try_begin(run_id, 0, "正在检索 arXiv…"):
                raise PipelineBusy("已有批次在运行")
            try:
                summary = self._execute(run_id)
            except Exception as exc:  # 检索阶段等致命错误
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    if run is not None:
                        run.status = "failed"
                        run.error_summary = str(exc)[:1000]
                        run.finished_at = datetime.now()
                        session.commit()
                self._state.finish(f"批次失败：{exc}")
                raise
            if summary.get("stopped"):
                self._state.finish(f"已停止：本批完成 {summary['succeeded']} 篇，其余可单篇重新提炼")
            else:
                self._state.finish(
                    f"完成：新增 {summary['fetched']} 篇，提炼成功 {summary['succeeded']}，失败 {summary['failed']}"
                )
            return run_id
        finally:
            self._mutex.release()
            self._drain_pending()  # 批次结束（含被停止）后自动消化排队的单篇提炼

    def redigest(self, paper_id: int) -> bool:
        """单篇重跑：互斥锁被占时不丢弃而是排队，锁释放后自动执行。

        返回 True=已立即执行；False=已入队等待。论文不存在等硬错误仍直接抛出。
        """
        return self._queue_single(paper_id, reset=True)

    def _queue_single(self, paper_id: int, *, reset: bool) -> bool:
        try:
            return self._run_single(paper_id, reset=reset)
        except PipelineBusy:
            with self._inflight_lock:
                if paper_id in self._inflight:
                    # 该论文仍在批次停止后的收尾任务中：排队会与收尾写入撞车，保持拒绝
                    raise
            with self._queue_lock:
                # 重复入队去重：同篇已在队列里就不重复排
                if (paper_id, reset) not in self._pending_singles:
                    self._pending_singles.append((paper_id, reset))
            logger.info("单篇提炼 #%s 已排队（互斥占用中），锁释放后自动执行", paper_id)
            return False

    def _drain_pending(self) -> None:
        """消化排队的单篇提炼：持锁者释放后调用；已在消化中则直接返回防递归。"""
        if self._draining:
            return
        self._draining = True
        try:
            while True:
                with self._queue_lock:
                    if not self._pending_singles:
                        return
                    paper_id, reset = self._pending_singles[0]
                try:
                    ok = self._run_single(paper_id, reset=reset)
                except PipelineBusy:
                    return  # 锁又被别人拿了；那位持锁者结束时同样会 drain
                except Exception:
                    logger.exception("排队单篇提炼 #%s 失败", paper_id)
                    continue
                finally:
                    with self._queue_lock:
                        if self._pending_singles and self._pending_singles[0] == (paper_id, reset):
                            self._pending_singles.pop(0)
                logger.info("排队单篇提炼 #%s 完成：%s", paper_id, "成功" if ok else "失败")
        finally:
            self._draining = False

    def _run_single(self, paper_id: int, *, reset: bool) -> bool:
        """共享的单篇提炼执行体：互斥锁 + 单篇登记 + inflight 守卫 + 状态机。

        供 redigest 与上传论文的首轮提炼复用，保证两条路径 409/丢弃语义完全一致。
        """
        if not self._mutex.acquire(blocking=False):
            raise PipelineBusy("批次或另一单篇重跑进行中，请稍后再试")
        try:
            with self._inflight_lock:
                if paper_id in self._inflight:
                    raise PipelineBusy("该论文仍在上一批次停止后的收尾任务中，请稍后再试")
            if not self._state.try_begin_single(paper_id):
                raise PipelineBusy("已有单篇重跑进行中，请稍后再试")
            try:
                if reset:
                    with self._sessions() as session:
                        paper = session.get(Paper, paper_id)
                        if paper is None:
                            raise ValueError(f"论文 {paper_id} 不存在")
                        paper.status = "fetched"
                        paper.fail_reason = None
                        session.commit()
                logger.info("单篇提炼 #%s 开始", paper_id)
                ok, _ = self._process_paper(paper_id)  # stop=None：独立事件，不受批次停止标志影响
                return bool(ok)
            finally:
                self._state.end_single()
        finally:
            self._mutex.release()
            self._drain_pending()

    def ingest_discover(self, raw) -> tuple[int, bool]:
        """在线检索结果入库（origin=discover）：已存在则直接返回其 ID。

        不在此方法内触发提炼——由路由经后台单篇任务执行，互斥语义与上传/重跑一致。
        """
        with self._sessions() as session:
            existing_id = session.execute(
                select(Paper.id).where(Paper.arxiv_id == raw.arxiv_id)
            ).scalar_one_or_none()
            if existing_id is not None:
                return existing_id, False
            setting = session.get(Setting, 1)
            keywords = list(setting.keywords or []) if setting else []
            score = score_paper(f"{raw.title} {raw.abstract}", keywords)
            paper = Paper(
                arxiv_id=raw.arxiv_id,
                version=raw.version,
                title=raw.title[:500],
                authors=", ".join(raw.authors)[:1000] if raw.authors else None,
                abstract=raw.abstract,
                categories=",".join(raw.categories)[:250] if raw.categories else None,
                pdf_url=raw.pdf_url,
                html_url=raw.html_url,
                score=score,
                status="fetched",
                published_at=raw.published_at,
                created_at=datetime.now(),  # 应用侧本地时间；DB 默认值是容器 UTC，会差 8 小时
                origin="discover",
            )
            session.add(paper)
            session.flush()
            paper_id = paper.id
            session.commit()
        logger.info("在线检索论文入库 [%s] %s（关键词分 %s）", raw.arxiv_id, raw.title[:60], score)
        return paper_id, True

    def ingest_upload(
        self,
        filename: str,
        text: str,
        local_file: str,
        upload_dir: str = "uploads",
    ) -> tuple[int, str]:
        """上传论文入库（不提炼）：建 Paper(origin=upload, status=fetched)。

        返回 (论文 ID, 标题)。提炼由调用方经 _run_single 触发；忙时保持 fetched，
        可在看板点「重新提炼」补跑。
        """
        from uuid import uuid4

        arxiv_id = f"up-{datetime.now().strftime('%y%m%d%H%M%S')}-{uuid4().hex[:4]}"
        safe_name = sanitize_filename(filename)
        title = self._guess_title(text, safe_name)
        with self._sessions() as session:
            paper = Paper(
                arxiv_id=arxiv_id,
                title=title,
                authors=None,
                abstract=None,
                categories="手工上传",
                score=0,
                status="fetched",
                published_at=datetime.now(),  # 以入库时间代替发表时间
                created_at=datetime.now(),  # 应用侧本地时间；DB 默认值是容器 UTC，会差 8 小时
                origin="upload",
                local_file=local_file,
                local_fulltext=text,
            )
            session.add(paper)
            session.commit()
            logger.info("上传论文入库 [%s] %s（%s 字符）", arxiv_id, title[:60], len(text))
            return paper.id, title

    @staticmethod
    def _guess_title(text: str, fallback: str) -> str:
        """正文首行像标题的当作标题（4-200 字符），否则用文件名。"""
        for line in text.splitlines():
            s = line.strip()
            if 4 <= len(s) <= 200:
                return s[:500]
        return fallback[:500]

    # ---------- 内部流程 ----------

    def _fetch_raws(
        self,
        categories: list[str],
        lookback_days: int,
        max_results: int = 100,
        sources: list[str] | None = None,
    ) -> list[RawPaper]:
        """多源抓取：按 sources 顺序（优先级）逐源取，不足额自动从下一源补齐，跨源去重。

        sources 来自设置页（settings.sources）；为空时回退 config.yaml 的 source。
        部分源失败不中断：只要还有源可用就继续；全部失败才抛错。
        """
        if self._source_fetchers is None and self._fetcher is not fetch_recent:
            # 未注入多源 fetchers 且注入了单个 fetcher（旧测试路径）：直接使用
            return self._fetcher(categories, lookback_days=lookback_days, max_results=max_results)
        from app import openalex_client

        fetchers = self._source_fetchers or {
            "arxiv": fetch_recent,
            "openalex": openalex_client.fetch_recent,
        }
        if not sources:
            sources = {"arxiv": ["arxiv"], "openalex": ["openalex"]}.get(
                self._source, ["arxiv", "openalex"]
            )

        raws: list[RawPaper] = []
        seen: set[str] = set()
        errors: list[str] = []
        for name in sources:
            need = max_results - len(raws)
            if need <= 0:
                break
            fetcher = fetchers.get(name)
            if fetcher is None:
                errors.append(f"{name}: 未知来源，已跳过")
                continue
            try:
                batch = fetcher(categories, lookback_days=lookback_days, max_results=need)
                fresh = []
                for r in batch:
                    if r.arxiv_id in seen:
                        continue
                    seen.add(r.arxiv_id)  # 批内重复也要去掉
                    fresh.append(r)
                raws.extend(fresh)
                logger.info(
                    "论文源 %s 提供候选 %s 篇（去重后累计 %s/%s）",
                    name, len(fresh), len(raws), max_results,
                )
            except Exception as exc:
                logger.warning("论文源 %s 检索失败：%s", name, exc)
                errors.append(f"{name}: {exc}")
        if not raws and errors:
            raise RuntimeError("论文源均不可用——" + "；".join(errors))
        if errors:
            logger.warning("部分论文源失败，已用其余源继续：%s", "；".join(errors))
        return raws

    def _execute(self, run_id: int) -> dict:
        with self._sessions() as session:
            setting = session.get(Setting, 1)
            if setting is None:
                raise RuntimeError("settings 表未初始化，请先运行 init_db")
            categories = [c.strip() for c in setting.categories.split(",") if c.strip()]
            keywords = list(setting.keywords or [])
            top_n = int(setting.top_n or 20)
            sources = list(setting.sources or [])

        # 批次隔离：只提炼本批新入库的论文；历史批次取消/中断留下的 fetched/failed
        # 不自动带入（避免老批次欠账越滚越大），需要时在看板单篇「重新提炼」
        queue_ids = self._ingest(categories, keywords, top_n, sources)
        new_count = len(queue_ids)
        self._state.begin(run_id, len(queue_ids), f"检索完成，待提炼 {len(queue_ids)} 篇")
        logger.info("批次 #%s 待提炼 %s 篇（本批新入库）", run_id, len(queue_ids))

        results: list[tuple[bool | None, int]] = []
        if queue_ids:
            # 硬停止：每秒轮询停止标志，一旦置位立即收尾——取消排队任务、
            # 放弃等待在飞任务（其结果在落库前被丢弃），批次秒级关闭
            pool = ThreadPoolExecutor(max_workers=self._max_workers)
            # 提交前捕获本批停止事件快照：批次收尾后新批次/单篇重跑复位全局标志，
            # 不影响仍在飞的本批 worker 的丢弃判定
            stop_event = self._state.stop_event
            pending = {pool.submit(self._process_paper, pid, stop_event) for pid in queue_ids}
            try:
                while pending:
                    done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            results.append(fut.result())
                        except Exception:
                            logger.exception("提炼任务异常退出")
                        # 进度只由批次主循环推进：旧批次的在飞 worker 不会
                        # 把 step 记到新批次的计数上
                        self._state.step()
                    if pending and self._state.stop_requested:
                        break
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        succeeded = sum(1 for ok, _ in results if ok)
        abandoned = len(queue_ids) - len(results)
        # 因停止跳过的论文返回 (None, 0)，不算失败（其状态是 fetched 待提炼）
        failed = sum(1 for ok, _ in results if ok is False)
        total_tokens = sum(tokens for _, tokens in results)
        stopped = self._state.stop_requested or abandoned > 0
        if stopped:
            status = "partial" if succeeded > 0 else "failed"
        else:
            status = "success" if failed == 0 else ("partial" if succeeded > 0 else "failed")

        with self._sessions() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.fetched_count = new_count
            run.succeeded_count = succeeded
            run.failed_count = failed
            run.total_tokens = total_tokens
            run.status = status
            run.finished_at = datetime.now()
            run.error_summary = (
                f"用户停止：本批完成 {succeeded} 篇，未完成 {len(queue_ids) - succeeded} 篇"
                "（保持待提炼，可在看板单篇重新提炼）"
                if stopped
                else self._collect_errors(session, queue_ids)
            )
            session.commit()
        if stopped:
            logger.info(
                "批次 #%s 被用户立即停止：完成 %s 篇，未完成 %s 篇（在飞结果已丢弃）",
                run_id, succeeded, len(queue_ids) - succeeded,
            )
        else:
            logger.info(
                "批次 #%s 完成：%s（新增 %s 篇，提炼成功 %s，失败 %s，token %s）",
                run_id, status, new_count, succeeded, failed, total_tokens,
            )

        return {
            "fetched": new_count,
            "succeeded": succeeded,
            "failed": failed,
            "stopped": stopped,
        }

    def _ingest(
        self,
        categories: list[str],
        keywords: list[dict],
        top_n: int,
        sources: list[str] | None = None,
    ) -> list[int]:
        """检索、打分、入库，返回本批新入库的论文 ID 列表。"""
        raws: list[RawPaper] = self._fetch_raws(
            categories, self._lookback_days, sources=sources
        )
        with self._sessions() as session:
            existing = {
                arxiv_id: version
                for arxiv_id, version in session.execute(
                    select(Paper.arxiv_id, Paper.version)
                ).fetchall()
            }
            # 版本更新：同论文出现新版本只刷新 version 字段，不重复提炼
            for r in raws:
                if r.arxiv_id in existing and existing[r.arxiv_id] != r.version:
                    session.execute(
                        Paper.__table__.update()
                        .where(Paper.arxiv_id == r.arxiv_id)
                        .values(version=r.version)
                    )
            fresh = [r for r in raws if r.arxiv_id not in existing]
            scored = []
            for r in fresh:
                score = score_paper(f"{r.title} {r.abstract}", keywords)
                scored.append((score, r))
            scored.sort(key=lambda pair: (-pair[0], pair[1].published_at or datetime.min))
            inserted_ids: list[int] = []
            for score, r in scored[:top_n]:
                paper = Paper(
                    arxiv_id=r.arxiv_id,
                    version=r.version,
                    title=r.title[:500],
                    authors=", ".join(r.authors)[:1000],
                    abstract=r.abstract,
                    categories=",".join(r.categories)[:250],
                    pdf_url=r.pdf_url,
                    html_url=r.html_url,
                    score=score,
                    status="fetched",
                    published_at=r.published_at,
                    created_at=datetime.now(),  # 应用侧本地时间；DB 默认值是容器 UTC，会差 8 小时
                )
                session.add(paper)
                session.flush()  # 先拿到自增 ID，供本批提炼队列使用
                inserted_ids.append(paper.id)
            session.commit()
            logger.info("打分筛选完成：候选 %s 篇，新入库 Top %s 篇", len(fresh), len(inserted_ids))
            return inserted_ids

    def _save_figures(self, session, paper) -> None:
        """JSON 列的原地修改不会标脏，必须显式 flag_modified 后提交。"""
        flag_modified(paper, "figures")
        session.commit()

    def _prepare_figures(self, session, paper) -> str | None:
        """识图编排：下载缺失图片 → 视觉转述（预算内）→ 组装注入段。

        返回 None = 无图/识图关闭/全部失败，prompt 与纯文本管线完全一致。
        任何识图侧故障不外抛——增强路径全部可降级（设计 §9）。
        """
        llm = self._llm
        if llm is None or not getattr(llm, "vision_model", None):
            return None
        figures = paper.figures or []
        if not figures:
            return None
        from pathlib import Path

        from app import figures as figures_mod

        described = [f for f in figures if f.get("status") == "described" and f.get("description")]
        quota = max(0, self._vision_max_figures - len(described))
        if quota == 0:
            return self._figure_notes(described)

        def _priority(fig):
            cap = (fig.get("caption") or "").lower()
            return -sum(1 for k in _FIGURE_PRIORITY if k in cap)

        todo = sorted([f for f in figures if f.get("status") != "described"], key=_priority)[:quota]
        started = time.monotonic()
        for i, fig in enumerate(todo):
            if time.monotonic() - started > self._figure_budget:
                fig["status"] = "failed"  # 预算耗尽：宁可少转述，不拖死锁内提炼
                self._save_figures(session, paper)
                break
            if i > 0:
                time.sleep(1.0)  # 对 arxiv.org 保持礼貌间隔（首次下载前已随全文抓取等待过）
            figures_mod.download_one(fig, paper.arxiv_id, Path(self._figures_dir))
            self._save_figures(session, paper)
            if fig.get("status") != "pending" or not fig.get("path"):
                continue  # skipped/failed/下载失败：跳过识图，不阻塞后续
            if time.monotonic() - started > self._figure_budget:
                fig["status"] = "failed"
                self._save_figures(session, paper)
                break
            description = None
            for attempt in range(2):  # 识图失败重试 1 次（设计 §6.1）
                if time.monotonic() - started > self._figure_budget and attempt > 0:
                    break
                try:
                    description = llm.describe_image(fig["path"], paper.title, fig.get("caption", ""))
                    break
                except Exception:
                    logger.warning("识图失败(第 %s 次) %s", attempt + 1, fig.get("url"), exc_info=True)
            if not description:
                fig["status"] = "failed"
                self._save_figures(session, paper)
                continue
            fig.update(
                description=description,
                described_by=llm.vision_model,
                described_at=datetime.now().isoformat(timespec="seconds"),
                status="described",
            )
            self._save_figures(session, paper)
        done = [f for f in figures if f.get("status") == "described" and f.get("description")]
        return self._figure_notes(done)

    @staticmethod
    def _figure_notes(described: list[dict]) -> str | None:
        if not described:
            return None
        return "\n".join(
            f"图{f['idx']}({(f.get('caption') or '无图注')[:80]}):{f['description']}" for f in described
        )

    def _process_paper(
        self, paper_id: int, stop: threading.Event | None = None
    ) -> tuple[bool | None, int]:
        """单篇状态机：fetched → digested / failed。独立 session，失败不拖垮批次。

        stop 是所属批次提交前捕获的停止事件快照；None 表示单篇重跑——
        用独立事件，不受批次停止标志影响。
        返回 (None, 0) 表示因用户停止而跳过：论文保持原状态，可在看板单篇重新提炼。
        """
        if stop is None:
            stop = threading.Event()
        with self._inflight_lock:
            self._inflight.add(paper_id)
        session = self._sessions()
        try:
            if stop.is_set():
                return None, 0
            paper = session.get(Paper, paper_id)
            if paper is None:
                return False, 0

            if self._llm is None:
                paper.status = "failed"
                paper.fail_reason = "未配置 LLM API Key（请在 config.yaml 填写后重启）"
                session.commit()
                return False, 0

            fulltext = None
            truncated = False
            if paper.local_fulltext:
                # 已有全文（上传时解析 / 首次提炼时缓存的 arXiv HTML）：直接用，不再外抓
                fulltext = paper.local_fulltext
                truncated = len(fulltext) > MAX_FULLTEXT_CHARS  # 超长标记；实际截断在 digest 调用内做
                # 全文缓存早于识图功能的存量论文：figures 还没提取过，补抓一次清单（仅此一次）
                if (
                    paper.figures is None
                    and paper.html_url
                    and self._llm is not None
                    and getattr(self._llm, "vision_model", None)
                ):
                    result = self._fulltext_fetcher(paper.html_url)
                    if result is not None:
                        paper.figures = result[2] or []
                        session.commit()
                    time.sleep(1.0)
            elif paper.html_url:
                result = self._fulltext_fetcher(paper.html_url)
                if result is not None:
                    fulltext, truncated, figures = result
                    # 缓存抓到的全文与图片清单：重新提炼不再请求 arXiv（也躲开它的限流）
                    paper.local_fulltext = fulltext
                    paper.figures = figures or []
                    session.commit()
                time.sleep(1.0)  # 对 arxiv.org 保持礼貌间隔

            figure_notes = self._prepare_figures(session, paper)
            data, tokens = self._digest_with_retry(paper, fulltext, figure_notes)
            if stop.is_set():
                # 硬停止：批次已收尾，本篇结果丢弃、不落库，论文保持待提炼
                return None, 0
            embedding = self._llm.embed(paper.title)

            if paper.digest is not None:
                session.delete(paper.digest)
                session.flush()  # 先发出 DELETE，避免同事务的新 INSERT 撞 uk_paper 唯一键
            paper.digest = Digest(
                one_liner=str(data["one_liner"])[:1000],
                outline=data["outline"],
                key_points=data["key_points"],
                implementation=data.get("implementation"),
                diagrams=data.get("diagrams") or None,
                principle=data["principle"],
                limitations=data["limitations"],
                difficulty=int(data["difficulty"]),
                truncated=truncated,
                source_type="fulltext" if fulltext else "abstract",
                model=self._llm.model,
                tokens_used=tokens,
                digested_at=datetime.now(),  # 应用侧本地时间；DB 默认值是容器 UTC，会差 8 小时
            )
            paper.embedding = np.asarray(embedding, dtype=np.float32).tobytes()
            paper.status = "digested"
            paper.fail_reason = None
            session.commit()
            logger.info(
                "提炼成功 [%s] %s（%s token，%s）",
                paper.arxiv_id, paper.title[:60], tokens, "全文" if fulltext else "仅摘要",
            )
            return True, tokens
        except Exception as exc:
            session.rollback()
            if stop.is_set():
                # 硬停止导致的调用中断不算失败：论文保持待提炼
                return None, 0
            try:
                paper = session.get(Paper, paper_id)
                if paper is not None:
                    paper.status = "failed"
                    paper.fail_reason = str(exc)[:500]
                    session.commit()
                    logger.warning(
                        "提炼失败 [%s] %s：%s", paper.arxiv_id, paper.title[:60], str(exc)[:150]
                    )
            except Exception:
                pass
            return False, 0
        finally:
            with self._inflight_lock:
                self._inflight.discard(paper_id)
            session.close()

    def _digest_with_retry(
        self, paper: Paper, fulltext: str | None, figure_notes: str | None = None
    ) -> tuple[dict, int]:
        """网络/限流指数退避重试：共 DIGEST_ATTEMPTS 次尝试（首次 + 重试 2 次）。"""
        assert self._llm is not None
        last_exc: Exception | None = None
        for attempt in range(DIGEST_ATTEMPTS):
            try:
                return self._llm.digest_with_tokens(
                    paper.title, paper.abstract or "", fulltext, figure_notes
                )
            except Exception as exc:
                last_exc = exc
                if attempt < DIGEST_ATTEMPTS - 1:
                    time.sleep(2**attempt)
        raise last_exc if last_exc else RuntimeError("digest 失败")

    def _collect_errors(self, session, paper_ids: list[int]) -> str | None:
        """只汇总本批次处理过的论文的失败原因，避免历史失败污染。"""
        if not paper_ids:
            return None
        reasons: dict[str, int] = {}
        rows = session.execute(
            select(Paper.fail_reason).where(
                Paper.status == "failed", Paper.id.in_(paper_ids)
            )
        ).scalars().fetchall()
        for reason in rows:
            if reason:
                key = reason[:120]
                reasons[key] = reasons.get(key, 0) + 1
        if not reasons:
            return None
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        return "；".join(f"{r}（×{c}）" for r, c in top)
