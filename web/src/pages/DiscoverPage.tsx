import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api'
import type { ApiDiscoverItem } from '../api'
import Badge from '../components/Badge'

type RowState =
  | { phase: 'idle' }
  | { phase: 'ingesting'; paperId: number }
  | { phase: 'queued'; paperId: number }
  | { phase: 'done'; paperId: number }
  | { phase: 'failed'; message: string }

// 检索状态持久化：localStorage（整页刷新/重开标签页也能恢复）+ 模块内存（切页恢复）。
// searching 不持久化——刷新会杀掉在飞的请求，恢复 true 会永远等下去。
const STORAGE_KEY = 'discover-cache-v1'
const STORAGE_TTL = 24 * 3600 * 1000

type DiscoverCache = {
  q: string
  limit: number
  sort: string
  results: ApiDiscoverItem[] | null
  intent: { authors: string[]; keywords: string[]; from_date: string | null; to_date: string | null } | null
  arxivQuery: string
  source: 'arxiv' | 'openalex'
  effSort: string
  searching: boolean
  lastError: string
  rowState: Record<string, RowState>
  savedAt: number
}

const defaults: DiscoverCache = {
  q: '',
  limit: 10,
  sort: 'auto',
  results: null,
  intent: null,
  arxivQuery: '',
  source: 'arxiv',
  effSort: 'relevance',
  searching: false,
  lastError: '',
  rowState: {},
  savedAt: 0,
}

function loadCache(): DiscoverCache {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...defaults }
    const d = JSON.parse(raw) as DiscoverCache
    if (!d || Date.now() - (d.savedAt || 0) > STORAGE_TTL) return { ...defaults }
    return { ...defaults, ...d, searching: false } // 在飞检索随刷新死亡，不恢复
  } catch {
    return { ...defaults }
  }
}

const cache: DiscoverCache = loadCache()

function saveCache() {
  cache.savedAt = Date.now()
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cache))
  } catch {
    /* 存储满等异常忽略，内存缓存仍有效 */
  }
}

export default function DiscoverPage() {
  const [q, setQ] = useState(cache.q)
  const [limit, setLimit] = useState(cache.limit)
  const [sort, setSort] = useState(cache.sort)
  const [results, setResults] = useState<ApiDiscoverItem[] | null>(cache.results)
  const [intent, setIntent] = useState(cache.intent)
  const [arxivQuery, setArxivQuery] = useState(cache.arxivQuery)
  const [source, setSource] = useState<'arxiv' | 'openalex'>(cache.source)
  const [effSort, setEffSort] = useState(cache.effSort)
  const [searching, setSearching] = useState(cache.searching)
  const [error, setError] = useState(cache.lastError)
  const [rowState, setRowState] = useState<Record<string, RowState>>(cache.rowState)
  const navigate = useNavigate()
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const rowStateRef = useRef(rowState) // 轮询回调里读最新值，避免闭包过期

  // 状态变化同步进缓存与 ref
  useEffect(() => {
    cache.q = q
    cache.limit = limit
    cache.sort = sort
    cache.results = results
    cache.intent = intent
    cache.arxivQuery = arxivQuery
    cache.source = source
    cache.effSort = effSort
    cache.rowState = rowState
    rowStateRef.current = rowState
    saveCache() // 持久化到 localStorage，整页刷新后可恢复
  }, [q, limit, sort, results, intent, arxivQuery, source, effSort, rowState])

  useEffect(() => {
    // 恢复时若还有提炼中/排队中的行，继续轮询
    if (
      Object.values(rowStateRef.current).some((s) => s.phase === 'ingesting' || s.phase === 'queued')
    )
      startPolling()
    // 恢复时若上一次检索还在进行（切页卸载了组件），接管等待：请求回调写缓存，这里取回
    if (cache.searching) {
      const timer = setInterval(() => {
        if (!cache.searching) {
          clearInterval(timer)
          setResults(cache.results)
          setIntent(cache.intent)
          setArxivQuery(cache.arxivQuery)
          setSource(cache.source)
          setEffSort(cache.effSort)
          setError(cache.lastError)
          setRowState(cache.rowState)
          setSearching(false)
        }
      }, 400)
      return () => clearInterval(timer)
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const doSearch = async () => {
    const query = q.trim()
    if (!query || searching || cache.searching) return
    setError('')
    cache.q = query
    cache.lastError = ''
    saveCache()
    setSearching(true)
    cache.searching = true // 组件可能中途被切页卸载，标志与结果都落在模块缓存
    setResults(null)
    try {
      const r = await api.discover({ q: query, limit, sort })
      cache.results = r.results
      cache.intent = r.intent
      cache.arxivQuery = r.arxiv_query
      cache.source = r.source
      cache.effSort = r.sort
      cache.rowState = {}
      saveCache() // 回调可能在组件卸载后执行（切页/刷新前），同步 effect 不会跑，这里自己落盘
      setResults(r.results)
      setIntent(r.intent)
      setArxivQuery(r.arxiv_query)
      setSource(r.source)
      setEffSort(r.sort)
      setRowState({})
    } catch (exc) {
      const msg = exc instanceof ApiError ? exc.message : '检索失败：后端未连接'
      cache.lastError = msg
      saveCache()
      setError(msg)
    } finally {
      cache.searching = false
      setSearching(false)
    }
  }

  const startPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      // queued 也要盯：排队中的论文被消化（或服务重启后队列失效被重跑）时行状态能自愈
      const watching = Object.entries(rowStateRef.current).filter(
        ([, s]) => s.phase === 'ingesting' || s.phase === 'queued',
      )
      if (watching.length === 0) {
        if (pollRef.current) clearInterval(pollRef.current)
        pollRef.current = null
        return
      }
      for (const [arxivId, s] of watching) {
        if (s.phase !== 'ingesting' && s.phase !== 'queued') continue
        try {
          const p = await api.paper(s.paperId)
          if (p.status === 'digested') setRowState((m) => ({ ...m, [arxivId]: { phase: 'done', paperId: s.paperId } }))
          else if (p.status === 'failed')
            setRowState((m) => ({
              ...m,
              [arxivId]: { phase: 'failed', message: p.fail_reason ?? '提炼失败' },
            }))
        } catch {
          /* 单次轮询失败忽略 */
        }
      }
    }, 3000)
  }

  const refine = async (item: ApiDiscoverItem) => {
    if (rowState[item.arxiv_id]?.phase === 'ingesting') return
    setError('')
    try {
      const r = await api.discoverIngest(item.arxiv_id)
      if (r.started) {
        setRowState((m) => ({ ...m, [item.arxiv_id]: { phase: 'ingesting', paperId: r.id } }))
        startPolling()
      } else {
        // 批次/另一单篇占用互斥锁：论文已入库但提炼被跳过，等空闲后重试
        setRowState((m) => ({ ...m, [item.arxiv_id]: { phase: 'queued', paperId: r.id } }))
        startPolling()
      }
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : '提炼请求失败：后端未连接')
    }
  }

  return (
    <div>
      <h1 className="page-title">在线检索</h1>
      <p className="page-sub">用一句话描述你想了解的方向，去 arXiv 实时检索；看中的论文一键提炼入库</p>

      {error && (
        <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>
          {error}
        </div>
      )}

      <div className="card">
        <div className="discover-bar">
          <input
            className="input main"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') doSearch()
            }}
            placeholder="例如：我想了解知识图谱领域在学术界的最新成果"
            disabled={searching}
          />
          <select className="input" value={limit} onChange={(e) => setLimit(Number(e.target.value))} title="返回结果数量">
            {[5, 10, 20, 50].map((n) => (
              <option key={n} value={n}>
                {n} 条
              </option>
            ))}
          </select>
          <select className="input" value={sort} onChange={(e) => setSort(e.target.value)} title="排序方式">
            <option value="auto">排序：自动识别</option>
            <option value="relevance">相关度优先</option>
            <option value="latest">最新优先</option>
            <option value="citations">最高被引</option>
          </select>
          <button className="btn" onClick={doSearch} disabled={searching || !q.trim()}>
            {searching ? '检索中…' : '🔍 检索'}
          </button>
        </div>
        {intent && results && results.length > 0 && (
          <div className="intent-bar">
            <span className="row-state">识别到：</span>
            {intent.authors.map((a) => (
              <Badge key={`au-${a}`} text={`👤 ${a}`} tone="purple" />
            ))}
            {(intent.from_date || intent.to_date) && (
              <Badge
                text={`📅 ${intent.from_date ?? '…'} ~ ${intent.to_date ?? '今天'}`}
                tone="orange"
              />
            )}
            {intent.keywords.slice(0, 4).map((k) => (
              <Badge key={`kw-${k}`} text={k} tone="gray" />
            ))}
            <span className="row-state" style={{ marginLeft: 6 }}>
              按顺序：{effSort === 'citations' ? '最高被引' : effSort === 'latest' ? '最新' : '相关度'}
              {source && (
                <>
                  {' · '}
                  <Badge text={source === 'arxiv' ? 'arXiv' : 'OpenAlex'} tone="blue" />
                </>
              )}
            </span>
            <div className="tip" style={{ width: '100%' }}>
              arXiv 检索式：{arxivQuery || '（未走 arXiv）'}
            </div>
          </div>
        )}
      </div>

      {results && (
        <div className="card">
          <h3 style={{ margin: '0 0 12px' }}>检索结果（{results.length} 篇）</h3>
          {results.map((item) => {
            const st = rowState[item.arxiv_id] ?? { phase: 'idle' as const }
            return (
              <div className="result-card" key={item.arxiv_id}>
                <a className="title" href={item.abs_url} target="_blank" rel="noreferrer">
                  {item.title} ↗
                </a>
                <div className="meta">
                  {item.authors && <span>{item.authors}</span>}
                  {item.published_at && <span>{item.published_at}</span>}
                  <span>arXiv:{item.arxiv_id}</span>
                  {item.categories.slice(0, 3).map((c) => (
                    <Badge key={c} text={c} tone="gray" />
                  ))}
                </div>
                {item.abstract && <p className="abstract">{item.abstract}</p>}
                <div className="actions">
                  {st.phase === 'idle' && !item.in_library && (
                    <button className="btn small" onClick={() => refine(item)}>
                      ⚡ 提炼
                    </button>
                  )}
                  {st.phase === 'idle' && item.in_library && (
                    <button
                      className="btn small secondary"
                      onClick={() => navigate(item.paper_id ? `/paper/${item.paper_id}` : '/')}
                    >
                      已在库 · 查看解读 →
                    </button>
                  )}
                  {st.phase === 'ingesting' && (
                    <>
                      <button className="btn small" disabled>
                        ⏳ 提炼中…
                      </button>
                      <span className="row-state">抓取全文 → GLM 深度提炼（约 1 分钟）</span>
                    </>
                  )}
                  {st.phase === 'done' && (
                    <button className="btn small secondary" onClick={() => navigate(`/paper/${st.paperId}`)}>
                      ✓ 已提炼 · 查看解读 →
                    </button>
                  )}
                  {st.phase === 'queued' && (
                    <>
                      <button className="btn small" onClick={() => refine(item)}>
                        重试提炼
                      </button>
                      <span className="row-state" style={{ color: 'var(--orange)' }}>
                        已排队，完成后自动更新——长时间未动可点「重试提炼」
                      </span>
                    </>
                  )}
                  {st.phase === 'failed' && (
                    <>
                      <button className="btn small" onClick={() => refine(item)}>
                        重试提炼
                      </button>
                      <span className="row-state" style={{ color: 'var(--red)' }}>
                        {st.message}
                      </span>
                    </>
                  )}
                  {st.phase === 'done' && (
                    <span className="row-state" style={{ marginLeft: 'auto' }}>
                      已在看板论文列表置顶展示
                    </span>
                  )}
                </div>
              </div>
            )
          })}
          {results.length === 0 && <div className="empty-state">没有检索到相关论文，换个说法试试。</div>}
        </div>
      )}
    </div>
  )
}
