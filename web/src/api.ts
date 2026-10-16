export interface ApiPaper {
  id: number
  arxiv_id: string
  title: string
  one_liner: string | null
  difficulty: number | null
  source_type: 'fulltext' | 'abstract' | null
  model: string | null
  digested_at: string | null
  status: 'fetched' | 'digested' | 'failed'
  fail_reason: string | null
  score: number
  published_at: string | null
  categories: string[]
  origin?: 'fetch' | 'upload' | 'discover'
  local_file?: string | null
}

export interface ApiPapersPage {
  items: ApiPaper[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface ApiDiagram {
  type: 'flow' | 'architecture'
  caption: string
  mermaid: string
}

export interface ApiPaperDetail extends ApiPaper {
  authors: string | null
  abstract: string | null
  pdf_url: string | null
  html_url: string | null
  outline: { section: string; summary: string }[] | null
  key_points: { point: string; detail: string }[] | null
  implementation: string | null
  diagrams: ApiDiagram[] | null
  principle: string | null
  limitations: string | null
  truncated: boolean
  model: string | null
  tokens_used: number
}

export interface ApiRun {
  id: number
  trigger_type: 'scheduled' | 'manual'
  status: 'running' | 'success' | 'partial' | 'failed'
  fetched_count: number
  succeeded_count: number
  failed_count: number
  total_tokens: number
  error_summary: string | null
  started_at: string | null
  finished_at: string | null
}

export interface ApiStats {
  today_count: number
  total_count: number
  month_tokens: number
  next_run_at: string | null
  last_run: ApiRun | null
}

export interface ApiCurrentRun {
  running: boolean
  run_id: number | null
  done: number
  total: number
  note: string
}

export interface ApiHealth {
  status: string
  key_configured: boolean
  chat_model: string | null
  next_run_at: string | null
}

export interface ApiSettings {
  categories: string
  keywords: { word: string; weight: number }[]
  sources: string[]
  top_n: number
  cron_expr: string
  chat_model: string
}

export interface ApiLogEntry {
  time: string
  level: string
  name: string
  message: string
}

export interface ApiLogs {
  file: string
  total_lines: number
  truncated: boolean
  entries: ApiLogEntry[]
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ApiDiscoverItem {
  arxiv_id: string
  title: string
  authors: string | null
  abstract: string | null
  categories: string[]
  published_at: string | null
  abs_url: string
  pdf_url: string | null
  in_library: boolean
  paper_id: number | null
}

export interface ApiDiscoverIntent {
  keywords: string[]
  authors: string[]
  from_date: string | null
  to_date: string | null
  sort_hint: string | null
}

export interface ApiDiscoverResult {
  query: string
  intent: ApiDiscoverIntent
  arxiv_query: string
  source: 'arxiv' | 'openalex'
  sort: string
  results: ApiDiscoverItem[]
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') {
        detail = body.detail
      } else if (Array.isArray(body?.detail)) {
        // FastAPI 422 校验错误是数组：[{loc, msg, ...}]，拼成可读文本
        detail = body.detail
          .map((item: { loc?: string[]; msg?: string }) => {
            const field = item.loc?.slice(1).join('.')
            return field ? `${field}：${item.msg ?? '校验失败'}` : (item.msg ?? '校验失败')
          })
          .join('；')
      }
    } catch {
      /* ignore body parse error */
    }
    throw new ApiError(resp.status, detail)
  }
  return resp.json() as Promise<T>
}

export const api = {
  health: () => request<ApiHealth>('/health'),
  stats: () => request<ApiStats>('/stats'),
  papers: (params: { date?: string; status?: string; page?: number; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.date) q.set('date', params.date)
    if (params.status) q.set('status', params.status)
    q.set('page', String(params.page ?? 1))
    q.set('limit', String(params.limit ?? 20))
    return request<ApiPapersPage>(`/papers?${q.toString()}`)
  },
  paper: (id: number) => request<ApiPaperDetail>(`/papers/${id}`),
  redigest: (id: number) => request<{ started: boolean }>(`/papers/${id}/redigest`, { method: 'POST' }),
  uploadPaper: async (file: File): Promise<{ id: number; title: string; started: boolean }> => {
    // multipart 上传不能用统一的 JSON request()（Content-Type 由浏览器带 boundary 生成）
    const form = new FormData()
    form.append('file', file)
    const resp = await fetch('/api/papers/upload', { method: 'POST', body: form })
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try {
        const body = await resp.json()
        if (typeof body?.detail === 'string') detail = body.detail
      } catch {
        /* ignore body parse error */
      }
      throw new ApiError(resp.status, detail)
    }
    return resp.json()
  },
  search: (q: string, topK = 10) =>
    request<(ApiPaper & { similarity: number })[]>(`/search?q=${encodeURIComponent(q)}&top_k=${topK}`),
  runs: (limit = 20) => request<ApiRun[]>(`/runs?limit=${limit}`),
  currentRun: () => request<ApiCurrentRun>('/runs/current'),
  triggerRun: () => request<{ started: boolean }>('/runs/trigger', { method: 'POST' }),
  stopRun: () => request<{ stopped: boolean }>('/runs/stop', { method: 'POST' }),
  getSettings: () => request<ApiSettings>('/settings'),
  saveSettings: (body: ApiSettings) =>
    request<{ saved: boolean }>('/settings', { method: 'PUT', body: JSON.stringify(body) }),
  discover: (params: { q: string; limit?: number; sort?: string }) => {
    const search = new URLSearchParams({ q: params.q })
    search.set('limit', String(params.limit ?? 10))
    search.set('sort', params.sort ?? 'relevance')
    return request<ApiDiscoverResult>(`/discover?${search.toString()}`)
  },
  discoverIngest: (arxivId: string) =>
    request<{ id: number; created: boolean; started: boolean }>('/discover/ingest', {
      method: 'POST',
      body: JSON.stringify({ arxiv_id: arxivId }),
    }),
  logs: (params: { limit?: number; level?: string; keyword?: string } = {}) => {
    const q = new URLSearchParams()
    q.set('limit', String(params.limit ?? 300))
    if (params.level) q.set('level', params.level)
    if (params.keyword) q.set('keyword', params.keyword)
    return request<ApiLogs>(`/logs?${q.toString()}`)
  },
  chatPaper: async (
    id: number,
    messages: ChatMessage[],
    onDelta: (text: string) => void,
    onReasoning?: () => void,
    signal?: AbortSignal,
  ) => {
    // 独立控制器：叠加外部 signal（组件卸载中断）与空闲超时看门狗
    const ctrl = new AbortController()
    const onExternalAbort = () => ctrl.abort()
    signal?.addEventListener('abort', onExternalAbort)
    let timedOut = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const kickWatchdog = () => {
      if (timer !== undefined) clearTimeout(timer)
      timer = setTimeout(() => {
        timedOut = true
        ctrl.abort()
      }, 60_000)
    }
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
    try {
      kickWatchdog() // 覆盖建连阶段
      const resp = await fetch(`/api/papers/${id}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages }),
        signal: ctrl.signal,
      })
      if (!resp.ok || !resp.body) {
        let detail = `HTTP ${resp.status}`
        try {
          const body = await resp.json()
          if (typeof body?.detail === 'string') detail = body.detail
        } catch {
          /* ignore body parse error */
        }
        throw new ApiError(resp.status, detail)
      }
      // 解析 SSE 流：data: {"kind":"content"|"reasoning","delta":"..."} / {"done":true} / {"error":"..."}
      reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        kickWatchdog() // 每次读之前重置：60 秒无新数据视为流卡死
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const line = buf.slice(0, idx).trim()
          buf = buf.slice(idx + 2)
          if (!line.startsWith('data: ')) continue
          let payload: { kind?: string; delta?: string; error?: string }
          try {
            payload = JSON.parse(line.slice(6))
          } catch {
            continue // 畸形帧（网关注入片段/半截数据）：跳过，不中断整次对话
          }
          if (payload.error) throw new ApiError(500, payload.error)
          if (payload.delta && payload.kind === 'content') onDelta(payload.delta)
          if (payload.delta && payload.kind === 'reasoning' && onReasoning) onReasoning()
        }
      }
    } catch (exc) {
      if (timedOut) {
        throw new ApiError(504, '对话超时：60 秒没有新内容，已中断，请重试')
      }
      throw exc
    } finally {
      if (timer !== undefined) clearTimeout(timer)
      signal?.removeEventListener('abort', onExternalAbort)
      reader?.cancel().catch(() => {
        /* 流已结束/中断时 cancel 会抛错，忽略 */
      })
    }
  },
}
