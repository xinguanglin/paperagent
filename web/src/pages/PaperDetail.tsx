import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../api'
import type { ApiPaperDetail, ChatMessage } from '../api'
import Badge, { difficultyTone } from '../components/Badge'
import MermaidDiagram from '../components/MermaidDiagram'

// 把"1. xxx\n2. xxx"编号步骤解析成列表;开头就不是编号的按普通段落处理
function parseSteps(text: string): string[] | null {
  const steps: string[] = []
  let started = false
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue
    const m = line.match(/^\s*(\d{1,2})[.、．]\s*(.*)$/)
    if (m) {
      steps.push(m[2].trim())
      started = true
    } else if (started) {
      steps[steps.length - 1] += line.trim() // 步骤的折行续文
    } else {
      return null // 首个非空行不是编号 → 非步骤格式
    }
  }
  return steps.length >= 3 ? steps : null
}

// 解读正文:按换行拆段渲染,配合 .digest-text 的行距与两端对齐
function DigestProse({ text }: { text: string | null }) {
  if (!text) return <p>（待提炼）</p>
  const paras = text.split(/\r?\n+/).filter((p) => p.trim())
  return (
    <div className="digest-text">
      {paras.map((p, i) => (
        <p key={i}>{p}</p>
      ))}
    </div>
  )
}

function ImplementationView({ text }: { text: string | null }) {
  if (!text) return <p>（待提炼）</p>
  const steps = parseSteps(text)
  if (steps) {
    return (
      <ol className="impl-list">
        {steps.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ol>
    )
  }
  return <DigestProse text={text} />
}

export default function PaperDetail() {
  const { id } = useParams()
  const [paper, setPaper] = useState<ApiPaperDetail | null>(null)
  const [mode, setMode] = useState<'loading' | 'real' | 'missing' | 'offline'>('loading')
  const [chat, setChat] = useState<ChatMessage[]>([])
  const [chatInput, setChatInput] = useState('')
  const [sending, setSending] = useState(false)
  const [thinking, setThinking] = useState(false)
  const chatListRef = useRef<HTMLDivElement>(null)
  const stickBottomRef = useRef(true) // 用户向上翻阅时暂停自动贴底，回到底部后恢复跟随
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .paper(Number(id))
      .then((p) => {
        if (!cancelled) {
          setPaper(p)
          setMode('real')
        }
      })
      .catch(() => {
        if (!cancelled) setMode('missing')
      })
    return () => {
      cancelled = true
    }
  }, [id])

  // 对话消息更新时贴底显示（仅当用户本就在底部附近，不打断向上翻阅）
  useEffect(() => {
    const el = chatListRef.current
    if (el && stickBottomRef.current) el.scrollTop = el.scrollHeight
  }, [chat, thinking])

  // 组件卸载时中断进行中的对话流，避免请求悬挂与闭包持有整页状态
  useEffect(() => () => abortRef.current?.abort(), [])

  if (mode === 'loading') return <div className="empty-state">加载中…</div>
  if (mode !== 'real' || !paper)
    return (
      <div className="empty-state">
        {mode === 'offline' ? '后端未连接，无法加载论文。' : '论文不存在。'}
        <br />
        <Link to="/">返回看板</Link>
      </div>
    )

  const sendChat = async () => {
    const question = chatInput.trim()
    if (!question || sending || !paper) return
    const history = [...chat, { role: 'user' as const, content: question }].slice(-12)
    setChat([...history, { role: 'assistant', content: '' }])
    setChatInput('')
    setSending(true)
    setThinking(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      await api.chatPaper(
        paper.id,
        history,
        (text) => {
          setThinking(false)
          setChat((ms) =>
            ms.map((m, i) => (i === ms.length - 1 ? { ...m, content: m.content + text } : m)),
          )
        },
        () => setThinking(true),
        ctrl.signal,
      )
    } catch (exc) {
      if ((exc as { name?: string } | null)?.name === 'AbortError') return // 卸载中断，无需反馈
      const msg = exc instanceof ApiError ? exc.message : '后端未连接'
      setChat((ms) =>
        ms.map((m, i) => {
          if (i !== ms.length - 1) return m
          // 已流出部分内容的是"中断"，否则是整体失败——两种都要让用户看见
          return m.content
            ? { ...m, content: `${m.content}\n\n（连接中断：${msg}）` }
            : { role: 'assistant' as const, content: `（对话失败：${msg}）` }
        }),
      )
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null
      setSending(false)
      setThinking(false)
    }
  }

  return (
    <div className="detail-layout">
      <div className="detail-main">
        <div className="detail-header">
          <Link to="/" className="back-link">
            ← 返回看板
          </Link>
          <h2>{paper.title}</h2>
          <div className="detail-meta">
            <span>{paper.authors || '未知作者'}</span>
            <span>{paper.published_at ?? ''}</span>
            {paper.origin === 'upload' ? (
              <span>上传文件：{paper.local_file ? paper.local_file.split('/').pop() : '—'}</span>
            ) : (
              <>
                <span>arXiv:{paper.arxiv_id}</span>
                <a
                  href={`https://arxiv.org/abs/${paper.arxiv_id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看原文 ↗
                </a>
              </>
            )}
          </div>
          <div className="detail-meta" style={{ marginTop: 8 }}>
            {paper.difficulty != null && (
              <Badge text={`通俗度 ${paper.difficulty}/5`} tone={difficultyTone(paper.difficulty)} />
            )}
            <Badge
              text={
                paper.source_type === 'fulltext'
                  ? '全文提炼'
                  : paper.source_type === 'abstract'
                    ? '仅摘要（无 HTML 版降级）'
                    : '尚未提炼'
              }
              tone={paper.source_type === 'fulltext' ? 'green' : paper.source_type === 'abstract' ? 'orange' : 'gray'}
            />
            {paper.truncated && <Badge text="长文已截断" tone="orange" />}
            {paper.categories.map((c) => (
              <Badge key={c} text={c} tone="gray" />
            ))}
            {paper.tokens_used > 0 && (
              <span style={{ fontSize: 12 }}>提炼消耗 {(paper.tokens_used / 1000).toFixed(1)}k token</span>
            )}
          </div>
        </div>

        {paper.status !== 'digested' && (
          <div className="saved-banner" style={{ background: 'var(--red-soft)', color: 'var(--red)' }}>
            {paper.status === 'failed'
              ? `提炼失败：${paper.fail_reason ?? '未知原因'}——可在看板点「重新提炼」`
              : '尚未提炼——等待下一批次或在看板手动重跑'}
          </div>
        )}

        <div className="card">
          <div className="section">
            <h4>💡 一句话总结</h4>
            <p>{paper.one_liner ?? '（待提炼）'}</p>
          </div>

          <div className="section">
            <h4>📑 论文提纲</h4>
            {paper.outline ? (
              <ol className="outline-list">
                {paper.outline.map((item) => (
                  <li key={item.section}>
                    <span className="sec-name">{item.section}</span>
                    <span className="sec-desc">{item.summary}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p>（待提炼）</p>
            )}
          </div>

          <div className="section">
            <h4>🎯 核心要点</h4>
            {paper.key_points ? (
              <ul className="kp-list">
                {paper.key_points.map((kp) => (
                  <li key={kp.point}>
                    <span className="kp-title">{kp.point}</span>
                    <span className="kp-detail">{kp.detail}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p>（待提炼）</p>
            )}
          </div>

          {paper.diagrams?.map((dg, i) => (
            <div className="section" key={`${dg.type}-${i}`}>
              <h4>{dg.type === 'architecture' ? '🏗 系统架构图' : '🔄 流程 / 框架图'}</h4>
              {dg.caption && <div className="mermaid-caption">{dg.caption}</div>}
              <MermaidDiagram code={dg.mermaid} />
            </div>
          ))}

          <div className="section">
            <h4>🛠 实现过程</h4>
            <ImplementationView text={paper.implementation} />
          </div>

          <div className="section">
            <h4>🔬 原理拆解</h4>
            <DigestProse text={paper.principle} />
          </div>

          <div className="section">
            <h4>⚠️ 局限与价值</h4>
            <DigestProse text={paper.limitations} />
          </div>
        </div>
      </div>

      <div className="detail-chat">
        <div className="card chat-panel">
          <h3>💬 向智能体提问</h3>
          <div className="tip" style={{ marginBottom: 10 }}>
            基于这篇论文的摘要与解读内容回答；没看懂的地方直接问。
          </div>
          <div
            className="chat-list"
            ref={chatListRef}
            onScroll={() => {
              const el = chatListRef.current
              if (el) stickBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
            }}
          >
            {chat.length === 0 && (
              <div className="chat-empty">例如：核心要点的第 3 点我没看懂，能展开讲讲吗？</div>
            )}
            {chat.map((m, i) => (
              <div key={i} className={`chat-msg ${m.role}`}>
                <div className="chat-bubble">
                  {m.content ||
                    (sending && i === chat.length - 1 ? (thinking ? '🤔 思考中…' : '…') : '')}
                </div>
              </div>
            ))}
          </div>
          <div className="chat-input-row">
            <input
              className="input"
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={(e) => {
                // 排除中文输入法组合态：选字/确认拼音的 Enter 不发送
                if (e.key === 'Enter' && !e.nativeEvent.isComposing) sendChat()
              }}
              placeholder="输入你的问题，回车发送"
              disabled={sending}
            />
            <button className="btn" onClick={sendChat} disabled={sending || !chatInput.trim()}>
              发送
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
