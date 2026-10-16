import { useEffect, useRef, useState } from 'react'

// mermaid 体积 ~1MB：动态 import，只有论文带图（本组件挂载）时才加载，不进主包
let mermaidPromise: Promise<typeof import('mermaid').default> | null = null

async function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((m) => {
      m.default.initialize({
        startOnLoad: false,
        securityLevel: 'strict', // mermaid 内建净化，标签/HTML 不生效，防注入
        theme: 'base',
        themeVariables: {
          primaryColor: '#eef0fe',
          primaryBorderColor: '#4f46e5',
          primaryTextColor: '#1b2333',
          lineColor: '#667085',
          secondaryColor: '#e8f1fd',
          tertiaryColor: '#f5f6fa',
          fontSize: '14px',
          fontFamily: 'PingFang SC, Microsoft YaHei, Segoe UI, sans-serif',
        },
      })
      return m.default
    })
  }
  return mermaidPromise
}

let seq = 0

export default function MermaidDiagram({ code }: { code: string }) {
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
  }, [])

  useEffect(() => {
    const myId = ++seq
    let cancelled = false
    setError('')
    loadMermaid()
      .then((mermaid) => mermaid.render(`paper-mmd-${myId}`, code))
      .then(({ svg }) => {
        if (!cancelled) setSvg(svg)
      })
      .catch(() => {
        // 模型输出偶发语法错误：降级显示源码，不阻塞解读页其他内容
        if (!cancelled) setError('图渲染失败（mermaid 语法错误）')
      })
    return () => {
      cancelled = true
    }
  }, [code])

  useEffect(() => {
    if (boxRef.current && svg) boxRef.current.innerHTML = svg
  }, [svg])

  if (error) {
    return (
      <div>
        <div className="mermaid-fallback-tip">{error}，mermaid 源码如下：</div>
        <pre className="log-box mermaid-src">{code}</pre>
        <button
          className="btn small secondary"
          onClick={() => {
            navigator.clipboard.writeText(code)
            setCopied(true)
            if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
            copyTimerRef.current = setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? '✓ 已复制' : '复制源码'}
        </button>
      </div>
    )
  }

  return <div className="mermaid-box" ref={boxRef} />
}
