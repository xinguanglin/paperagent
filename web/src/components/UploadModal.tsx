import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api'

const MAX_MB = 30
const ALLOWED = ['pdf', 'doc', 'docx']

interface UploadModalProps {
  readonly onClose: () => void
  readonly onUploaded: () => void
}

export default function UploadModal({ onClose, onUploaded }: UploadModalProps) {
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [result, setResult] = useState<{ id: number; title: string; started: boolean } | null>(null)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const pick = (f: File | null | undefined) => {
    setError('')
    if (!f) return
    const ext = f.name.split('.').pop()?.toLowerCase() ?? ''
    if (!ALLOWED.includes(ext)) {
      setError(`不支持的格式 .${ext || '?'}——仅支持 PDF / DOC / DOCX`)
      return
    }
    if (f.size > MAX_MB * 1024 * 1024) {
      setError(`文件超过 ${MAX_MB}MB 上限`)
      return
    }
    setFile(f)
  }

  const submit = async () => {
    if (!file || uploading) return
    setUploading(true)
    setError('')
    try {
      const r = await api.uploadPaper(file)
      setResult(r)
      onUploaded()
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : '上传失败：后端未连接')
    } finally {
      setUploading(false)
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>⬆ 上传论文</h3>
        {result ? (
          <div>
            <div className="saved-banner">
              已入库{result.started ? '，正在提炼（约 2-4 分钟，完成后自动刷新）' : '。当前有任务在跑，稍后在看板点「重新提炼」即可'}
            </div>
            <p className="page-sub" style={{ marginBottom: 14 }}>{result.title}</p>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button className="btn secondary" onClick={onClose}>
                关闭
              </button>
              <Link className="btn" to={`/paper/${result.id}`}>
                查看论文
              </Link>
            </div>
          </div>
        ) : (
          <div>
            <div
              className="upload-zone"
              onClick={() => inputRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault()
                pick(e.dataTransfer.files?.[0])
              }}
            >
              {file ? file.name : '点击或拖拽文件到这里'}
              <div className="tip">支持 PDF / DOC / DOCX，单文件，不超过 {MAX_MB}MB</div>
            </div>
            <input
              ref={inputRef}
              type="file"
              accept=".pdf,.doc,.docx"
              style={{ display: 'none' }}
              onChange={(e) => pick(e.target.files?.[0])}
            />
            {error && (
              <div
                className="saved-banner"
                style={{ background: 'var(--red-soft)', color: 'var(--red)' }}
              >
                {error}
              </div>
            )}
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 14 }}>
              <button className="btn secondary" onClick={onClose} disabled={uploading}>
                取消
              </button>
              <button className="btn" onClick={submit} disabled={!file || uploading}>
                {uploading ? '解析上传中…' : '上传并提炼'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
