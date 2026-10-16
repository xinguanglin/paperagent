import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { ApiSettings } from '../api'

const initialSettings: ApiSettings = {
  categories: 'cs.AI, cs.CL, cs.LG',
  keywords: [
    { word: 'LLM', weight: 5 },
    { word: 'agent', weight: 4 },
    { word: 'reasoning', weight: 4 },
    { word: 'RAG', weight: 3 },
    { word: 'multimodal', weight: 3 },
    { word: 'RLHF', weight: 3 },
  ],
  sources: ['arxiv', 'openalex'],
  top_n: 20,
  cron_expr: '0 8 * * *',
  chat_model: '',
}

const SOURCE_OPTIONS = [
  { id: 'arxiv', label: 'arXiv', desc: '官方 API，最新最快，高峰期限流（429）' },
  { id: 'openalex', label: 'OpenAlex', desc: '学术索引，收录 arXiv 仓库作品，索引滞后数小时' },
]

export default function SettingsPage() {
  const [categories, setCategories] = useState(initialSettings.categories)
  const [keywords, setKeywords] = useState<ApiSettings['keywords']>(initialSettings.keywords)
  const [sources, setSources] = useState<string[]>(initialSettings.sources)
  const [topN, setTopN] = useState(initialSettings.top_n)
  const [cronExpr, setCronExpr] = useState(initialSettings.cron_expr)
  const [chatModel, setChatModel] = useState(initialSettings.chat_model)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  const [offline, setOffline] = useState(false)

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        setCategories(s.categories)
        setKeywords(s.keywords)
        setSources(s.sources?.length ? s.sources : ['arxiv'])
        setTopN(s.top_n)
        setCronExpr(s.cron_expr)
        setChatModel(s.chat_model)
        setOffline(false)
      })
      .catch(() => setOffline(true))
  }, [])

  const toggleSource = (id: string) => {
    setSources((list) => (list.includes(id) ? list.filter((s) => s !== id) : [...list, id]))
  }

  const moveSource = (id: string, dir: -1 | 1) => {
    setSources((list) => {
      const i = list.indexOf(id)
      const j = i + dir
      if (i < 0 || j < 0 || j >= list.length) return list
      const next = [...list]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }

  const updateKeyword = (index: number, patch: Partial<ApiSettings['keywords'][number]>) => {
    setKeywords((list) => list.map((kw, i) => (i === index ? { ...kw, ...patch } : kw)))
  }

  const save = async () => {
    setError('')
    if (sources.length === 0) {
      setError('请至少选择一个论文来源')
      return
    }
    try {
      await api.saveSettings({
        categories,
        keywords,
        sources,
        top_n: topN,
        cron_expr: cronExpr,
        chat_model: chatModel,
      })
      setSaved(true)
      setOffline(false)
      window.setTimeout(() => setSaved(false), 2500)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : '保存失败：后端未连接')
    }
  }

  return (
    <div>
      <h1 className="page-title">订阅设置</h1>
      <p className="page-sub">修改后立即生效，无需重启</p>

      {offline && (
        <div className="saved-banner" style={{ background: 'var(--orange-soft)', color: 'var(--orange)' }}>
          后端未连接——当前显示默认值，保存不会生效
        </div>
      )}
      {saved && <div className="saved-banner">✓ 已保存并生效（定时任务已按新 cron 重排）</div>}
      {error && (
        <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>{error}</div>
      )}

      <div className="card">
        <div className="form-row">
          <label>arXiv 分类</label>
          <input className="input" value={categories} onChange={(e) => setCategories(e.target.value)} />
          <div className="tip">逗号分隔。常用：cs.AI 人工智能 / cs.CL 计算语言学 / cs.LG 机器学习 / cs.CV 视觉</div>
        </div>

        <div className="form-row">
          <label>关键词与权重（打分 = 命中词权重之和，取每日 Top N）</label>
          <table className="kw-table">
            <tbody>
              {keywords.map((kw, i) => (
                <tr key={i}>
                  <td style={{ width: '45%' }}>
                    <input className="input" value={kw.word} onChange={(e) => updateKeyword(i, { word: e.target.value })} />
                  </td>
                  <td style={{ width: '25%' }}>
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={10}
                      value={kw.weight}
                      onChange={(e) => updateKeyword(i, { weight: Number(e.target.value) })}
                    />
                  </td>
                  <td>
                    <button className="kw-del" onClick={() => setKeywords((list) => list.filter((_, j) => j !== i))}>
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            className="btn secondary small"
            style={{ marginTop: 10 }}
            onClick={() => setKeywords((list) => [...list, { word: '', weight: 1 }])}
          >
            + 添加关键词
          </button>
        </div>

        <div className="form-row">
          <label>论文来源（勾选启用；列表顺序 = 抓取优先级，排前面的源抓不满时自动用后面的源补齐）</label>
          <table className="kw-table">
            <tbody>
              {SOURCE_OPTIONS.map((opt) => {
                const order = sources.indexOf(opt.id)
                const checked = order >= 0
                return (
                  <tr key={opt.id}>
                    <td style={{ width: '30%' }}>
                      <label style={{ display: 'inline-flex', gap: 8, alignItems: 'center', fontWeight: 400, marginBottom: 0 }}>
                        <input type="checkbox" checked={checked} onChange={() => toggleSource(opt.id)} />
                        {opt.label}
                        {checked && <span className="badge-blue source-order">优先级 {order + 1}</span>}
                      </label>
                    </td>
                    <td>
                      <span className="tip">{opt.desc}</span>
                    </td>
                    <td style={{ width: 90 }}>
                      {checked && (
                        <span style={{ display: 'inline-flex', gap: 4 }}>
                          <button
                            className="kw-del"
                            title="提高优先级"
                            disabled={order === 0}
                            onClick={() => moveSource(opt.id, -1)}
                          >
                            ↑
                          </button>
                          <button
                            className="kw-del"
                            title="降低优先级"
                            disabled={order === sources.length - 1}
                            onClick={() => moveSource(opt.id, 1)}
                          >
                            ↓
                          </button>
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="tip">单选一个源 = 只用它；多选 = 按优先级依次抓取、不足补齐、跨源去重后统一打分取 Top N</div>
        </div>

        <div className="form-row">
          <label>每日 Top N（进入提炼的论文数）</label>
          <input
            className="input"
            type="number"
            min={1}
            max={50}
            value={topN}
            onChange={(e) => setTopN(Number(e.target.value))}
          />
          <div className="tip">1-50；越大越全，token 消耗越多（设 1 可低成本试跑）</div>
        </div>

        <div className="form-row">
          <label>定时执行（cron 表达式）</label>
          <input className="input" value={cronExpr} onChange={(e) => setCronExpr(e.target.value)} />
          <div className="tip">默认 0 8 * * * = 每天 08:00；五字段：分 时 日 月 周</div>
        </div>

        <div className="form-row">
          <label>解读模型</label>
          <input
            className="input"
            value={chatModel}
            onChange={(e) => setChatModel(e.target.value)}
            placeholder="留空 = 使用 config.yaml 的 chat_model；填各服务自己的模型名"
          />
          <div className="tip">
            任何 OpenAI 兼容服务（智谱/DeepSeek/Qwen/Ollama/vLLM 等）的模型名均可，
            如 glm-5.3-flash、deepseek-chat、qwen-max
          </div>
        </div>

        <button className="btn" onClick={save}>
          保存设置
        </button>
      </div>

      <div className="card">
        <h3>安全说明</h3>
        <p style={{ margin: 0, color: 'var(--muted)' }}>
          智谱 API Key、MySQL 连接与代理地址存放在程序目录的 config.yaml（不进 git、不进数据库）。本页设置存于
          MySQL 的 settings 表。
        </p>
      </div>
    </div>
  )
}
