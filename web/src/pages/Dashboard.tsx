import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { ApiHealth, ApiPaper, ApiStats } from '../api'
import Badge from '../components/Badge'
import PaperCard from '../components/PaperCard'
import UploadModal from '../components/UploadModal'

const statusTone = { running: 'blue', success: 'green', partial: 'orange', failed: 'red' } as const
const statusLabel = { running: '进行中', success: '成功', partial: '部分失败', failed: '失败' } as const

function fmtTokens(n: number): string {
  return n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n)
}

export default function Dashboard() {
  const [health, setHealth] = useState<ApiHealth | null>(null)
  const [stats, setStats] = useState<ApiStats | null>(null)
  const [papers, setPapers] = useState<ApiPaper[]>([])
  const [pager, setPager] = useState({ total: 0, page: 1, pages: 1 })
  const [paperPage, setPaperPage] = useState(1)
  const [current, setCurrent] = useState<{ running: boolean; done: number; total: number; note: string } | null>(null)
  const [stopping, setStopping] = useState(false)
  const [offline, setOffline] = useState(false)
  const [error, setError] = useState('')
  const [redigesting, setRedigesting] = useState<Set<number>>(new Set())
  const [uploadOpen, setUploadOpen] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const redigestPollsRef = useRef<Map<number, ReturnType<typeof setInterval>>>(new Map())

  // 卸载时清掉所有单篇重跑轮询
  useEffect(() => {
    const polls = redigestPollsRef.current
    return () => {
      for (const t of polls.values()) clearInterval(t)
    }
  }, [])

  const stopRedigestPoll = (id: number) => {
    const t = redigestPollsRef.current.get(id)
    if (t) {
      clearInterval(t)
      redigestPollsRef.current.delete(id)
    }
  }

  const clearRedigesting = (id: number) => {
    setRedigesting((s) => {
      if (!s.has(id)) return s
      const next = new Set(s)
      next.delete(id)
      return next
    })
  }

  // 单篇重跑是 2-4 分钟的 LLM 任务：固定间隔轮询该篇状态直到完成（最长 5 分钟）
  const pollRedigest = (id: number) => {
    const startedAt = Date.now()
    const timer = setInterval(async () => {
      const elapsed = Date.now() - startedAt
      if (elapsed > 5 * 60_000) {
        stopRedigestPoll(id)
        clearRedigesting(id)
        loadAll()
        return
      }
      try {
        const p = await api.paper(id)
        // 前 8 秒跳过判定：后端把状态复位为 fetched 有瞬间延迟，别把旧状态误判为完成
        if (elapsed > 8_000 && (p.status === 'digested' || p.status === 'failed')) {
          stopRedigestPoll(id)
          clearRedigesting(id)
          loadAll()
        }
      } catch {
        /* 后端暂不可达：下轮再查 */
      }
    }, 4000)
    redigestPollsRef.current.set(id, timer)
  }

  const redigest = async (id: number) => {
    if (redigesting.has(id)) return
    try {
      await api.redigest(id)
      setError('')
      setRedigesting((s) => new Set(s).add(id))
      pollRedigest(id)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : '重跑失败：后端未连接')
    }
  }

  const loadAll = useCallback(async () => {
    try {
      const [h, s, p] = await Promise.all([
        api.health(),
        api.stats(),
        api.papers({ page: paperPage, limit: 20 }),
      ])
      setHealth(h)
      setStats(s)
      setPapers(p.items)
      setPager({ total: p.total, page: p.page, pages: Math.max(1, p.pages) })
      setOffline(false)
    } catch (exc) {
      // 区分两种失败：接口报错（后端在，如数据库断连）≠ 后端不可达
      if (exc instanceof ApiError) {
        setOffline(false)
        setError(`接口错误：${exc.message}（后端已在运行，请检查数据库连接）`)
      } else {
        setOffline(true)
      }
    }
  }, [paperPage])

  useEffect(() => {
    loadAll()
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [loadAll])

  const startPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const c = await api.currentRun()
        setCurrent(c)
        if (!c.running) {
          if (pollRef.current) clearInterval(pollRef.current)
          pollRef.current = null
          setStopping(false)
          loadAll()
        }
      } catch {
        /* 轮询失败忽略，下次重试 */
      }
    }, 2000)
  }, [loadAll])

  // 页面加载/刷新时若批次已在跑（如定时任务），恢复运行状态显示与轮询
  useEffect(() => {
    api
      .currentRun()
      .then((c) => {
        setCurrent(c)
        if (c.running) startPolling()
      })
      .catch(() => {})
  }, [startPolling])

  const trigger = async () => {
    setError('')
    try {
      await api.triggerRun()
      setCurrent({ running: true, done: 0, total: 0, note: '已触发，正在检索…' })
      startPolling()
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : '触发失败：后端未连接')
    }
  }

  const stop = async () => {
    setError('')
    try {
      await api.stopRun()
      setStopping(true)
      setCurrent((c) => (c ? { ...c, note: '已请求停止，正在中断…' } : c))
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : '停止失败：后端未连接')
    }
  }

  const running = current?.running ?? false
  const latest = stats?.last_run ?? null

  return (
    <div>
      <h1 className="page-title">今日看板</h1>
      <p className="page-sub">
        {new Date().toLocaleDateString('zh-CN')} · 每日 08:00 自动执行 · 也可手动触发
      </p>

      {offline && (
        <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>
          后端未连接（http://127.0.0.1:8787）。请双击 start.bat 启动后端后刷新页面。
        </div>
      )}
      {!offline && health && !health.key_configured && (
        <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>
          未配置智谱 API Key：论文会正常检索入库，但不会提炼，语义搜索不可用。请在 config.yaml 填写
          api_key 后重启服务。
        </div>
      )}
      {error && (
        <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>{error}</div>
      )}

      <div className="stats">
        <div className="stat-card">
          <div className="num">{stats?.today_count ?? '—'}</div>
          <div className="label">今日新增论文</div>
        </div>
        <div className="stat-card">
          <div className="num">{stats?.total_count ?? '—'}</div>
          <div className="label">库内论文总数</div>
        </div>
        <div className="stat-card">
          <div className="num">{stats ? fmtTokens(stats.month_tokens) : '—'}</div>
          <div className="label">本月 token 消耗</div>
        </div>
        <div className="stat-card">
          <div className="num">08:00</div>
          <div className="label">下次定时执行</div>
        </div>
      </div>

      <div className="card">
        <div className="run-row">
          <h3 style={{ margin: 0 }}>
            {running ? '批次进行中' : latest ? `最近批次 #${latest.id}` : '最近批次'}
          </h3>
          {running ? (
            <Badge text="进行中" tone="blue" />
          ) : latest ? (
            <Badge text={statusLabel[latest.status]} tone={statusTone[latest.status]} />
          ) : null}
          {latest && !running && (
            <span style={{ color: 'var(--muted)', fontSize: 13 }}>
              {latest.fetched_count} 篇抓取 · {latest.succeeded_count} 成功 · {latest.failed_count} 失败 ·{' '}
              {fmtTokens(latest.total_tokens)} token
            </span>
          )}
          <div className="grow" />
          {running && (
            <button
              className="btn secondary"
              onClick={stop}
              disabled={stopping || offline}
              title="停止本批次：已完成并入库的解读保留，未开始的论文可在看板单篇重新提炼"
            >
              {stopping ? '正在停止…' : '■ 停止'}
            </button>
          )}
          <button className="btn secondary" onClick={() => setUploadOpen(true)}>
            ⬆ 上传论文
          </button>
          <button className="btn" onClick={trigger} disabled={running || offline}>
            {running ? '任务进行中…' : '▶ 立即抓取'}
          </button>
        </div>
        {running && current && (
          <div>
            <div className="progress-track">
              <div
                className="progress-fill"
                style={{ width: `${current.total > 0 ? (current.done / current.total) * 100 : 6}%` }}
              />
            </div>
            <div className="progress-label">
              {current.note}
              {current.total > 0 ? `（${current.done}/${current.total}）` : '…'}
            </div>
          </div>
        )}
        {latest?.error_summary && !running && (
          <div className="progress-label" style={{ color: 'var(--orange)' }}>
            上次批次问题：{latest.error_summary}
          </div>
        )}
      </div>

      <div className="card">
        <h3>论文列表（共 {pager.total} 篇）</h3>
        {papers.map((p) => (
          <PaperCard
            key={p.id}
            paper={p}
            onRedigest={redigest}
            redigesting={redigesting.has(p.id)}
          />
        ))}
        {papers.length === 0 && !offline && (
          <div className="empty-state">库里还没有论文——点上方「立即抓取」跑第一批。</div>
        )}
        {pager.pages > 1 && (
          <div className="pager">
            <button
              className="btn secondary small"
              disabled={pager.page <= 1}
              onClick={() => setPaperPage(paperPage - 1)}
            >
              ‹ 上一页
            </button>
            {Array.from({ length: pager.pages }, (_, i) => i + 1)
              .filter((n) => n === 1 || n === pager.pages || Math.abs(n - pager.page) <= 1)
              .map((n, idx, arr) => (
                <span key={n} className="pager-group">
                  {idx > 0 && arr[idx - 1] !== n - 1 && <span className="pager-dots">…</span>}
                  <button
                    className={`btn small${n === pager.page ? '' : ' secondary'}`}
                    onClick={() => setPaperPage(n)}
                  >
                    {n}
                  </button>
                </span>
              ))}
            <button
              className="btn secondary small"
              disabled={pager.page >= pager.pages}
              onClick={() => setPaperPage(paperPage + 1)}
            >
              下一页 ›
            </button>
          </div>
        )}
      </div>

      {uploadOpen && <UploadModal onClose={() => setUploadOpen(false)} onUploaded={loadAll} />}
    </div>
  )
}
