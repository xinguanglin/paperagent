import { useState } from 'react'
import { api, ApiError } from '../api'
import type { ApiPaper } from '../api'
import PaperCard from '../components/PaperCard'

const hotQueries = ['多智能体协作', '幻觉检测', '长上下文', 'RLHF 奖励模型', '多模态预训练', '思维链推理']

export default function SearchPage() {
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [hits, setHits] = useState<(ApiPaper & { similarity: number })[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const search = async (q: string) => {
    setQuery(q)
    if (!q.trim()) return
    setSubmitted(q)
    setError('')
    setLoading(true)
    try {
      const results = await api.search(q)
      setHits(results)
    } catch (exc) {
      setHits([])
      if (exc instanceof ApiError) {
        setError(exc.message)
      } else {
        setError('搜索失败：后端未连接。请先运行 start.bat 启动后端（需已配置智谱 API Key）。')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <h1 className="page-title">语义搜索</h1>
      <p className="page-sub">输入一段话，按标题语义相似度排序（智谱 embedding-3 · 余弦相似度）</p>

      <div className="card">
        <div className="search-box">
          <input
            className="input"
            placeholder="例如：怎么让多个 agent 合作完成任务 / 怎样检测大模型一本正经地胡说八道"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') search(query)
            }}
          />
          <button className="btn" onClick={() => search(query)} disabled={loading}>
            {loading ? '搜索中…' : '搜索'}
          </button>
        </div>
        <div className="search-hint">支持中文描述或英文关键词混合输入 · 匹配范围为论文标题向量</div>

        {error && (
          <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>
            {error}
          </div>
        )}

        {!submitted && (
          <div className="hot-queries">
            {hotQueries.map((q) => (
              <button key={q} onClick={() => search(q)}>
                {q}
              </button>
            ))}
          </div>
        )}

        {submitted && !error && !loading && (
          <div>
            {hits.length === 0 ? (
              <div className="empty-state">没有找到语义相近的论文，换个说法试试</div>
            ) : (
              hits.map((hit) => <PaperCard key={hit.id} paper={hit} similarity={hit.similarity} />)
            )}
          </div>
        )}
      </div>
    </div>
  )
}
