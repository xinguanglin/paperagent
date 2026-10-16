import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { ApiLogEntry } from '../api'

const LEVEL_OPTIONS = [
  { value: 'DEBUG', label: '全部级别' },
  { value: 'INFO', label: 'INFO 及以上' },
  { value: 'WARNING', label: 'WARNING 及以上' },
  { value: 'ERROR', label: '仅 ERROR' },
]

const levelColor = (level: string) => {
  if (level === 'ERROR') return 'var(--red)'
  if (level === 'WARNING') return 'var(--orange)'
  return 'var(--muted)'
}

function LogRow({ entry }: { entry: ApiLogEntry }) {
  return (
    <div className="log-line">
      <span className="log-time">{entry.time.slice(11, 23)}</span>
      <span className="log-level" style={{ color: levelColor(entry.level) }}>
        {entry.level}
      </span>
      <span className="log-msg">{entry.message}</span>
    </div>
  )
}

export default function LogsPage() {
  const [entries, setEntries] = useState<ApiLogEntry[]>([])
  const [level, setLevel] = useState('INFO')
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('') // 防抖 400ms 后的过滤词，避免逐字符发请求
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [truncated, setTruncated] = useState(false)
  const [error, setError] = useState('')
  const boxRef = useRef<HTMLDivElement>(null)
  const stickToBottom = useRef(true) // 用户向上翻阅时暂停自动贴底，回到底部后恢复跟随
  const seqRef = useRef(0) // 请求序号：晚到的旧响应直接丢弃，防止覆盖新过滤结果

  // 关键词防抖：停止输入 400ms 后才触发过滤请求（服务端每次要读日志尾部，不能逐键打）
  useEffect(() => {
    const t = setTimeout(() => setKeyword(keywordInput), 400)
    return () => clearTimeout(t)
  }, [keywordInput])

  const handleScroll = () => {
    const box = boxRef.current
    if (box) stickToBottom.current = box.scrollHeight - box.scrollTop - box.clientHeight < 40
  }

  const load = useCallback(async () => {
    const seq = ++seqRef.current
    try {
      const result = await api.logs({ level, keyword })
      if (seq !== seqRef.current) return // 已有更新的请求，本次结果过期
      setEntries(result.entries)
      setTruncated(result.truncated)
      setError('')
    } catch (exc) {
      if (seq !== seqRef.current) return
      setError(exc instanceof ApiError ? exc.message : '后端未连接')
    }
  }, [level, keyword])

  // 过滤条件变化立即加载
  useEffect(() => {
    load()
  }, [load])

  // 自动刷新（5s），批次运行时实时看到进度
  useEffect(() => {
    if (!autoRefresh) return
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [autoRefresh, load])

  // 新日志到达时贴底显示（仅当用户本就在底部附近，不打断向上翻阅）
  useEffect(() => {
    const box = boxRef.current
    if (box && stickToBottom.current) box.scrollTop = box.scrollHeight
  }, [entries])

  return (
    <div className="log-page">
      <h1 className="page-title">抓取日志</h1>
      <p className="page-sub">检索、打分、提炼全过程的运行日志（按天滚动保留 7 天）</p>

      {error && (
        <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>
          {error}
        </div>
      )}

      <div className="card">
        <div className="log-toolbar">
          <select className="input log-filter" value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVEL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <input
            className="input log-filter"
            placeholder="关键词过滤，如 批次 / 提炼失败 / arxiv"
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
          />
          <label className="log-auto">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            自动刷新
          </label>
          <button className="btn secondary small" onClick={load}>
            立即刷新
          </button>
        </div>

        <div className="log-box" ref={boxRef} onScroll={handleScroll}>
          {entries.length === 0 ? (
            <div className="log-empty">暂无日志</div>
          ) : (
            entries.map((entry, i) => <LogRow key={i} entry={entry} />)
          )}
        </div>

        <div className="tip" style={{ marginTop: 10 }}>
          {entries.length} 条
          {truncated && '（仅显示最近 300 条，更早的请缩小过滤条件）'}
          {autoRefresh && ' · 每 5 秒自动刷新'}
        </div>
      </div>
    </div>
  )
}
