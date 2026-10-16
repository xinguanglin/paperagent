import { Link } from 'react-router-dom'
import type { ApiPaper } from '../api'
import Badge, { difficultyTone } from './Badge'

interface PaperCardProps {
  readonly paper: ApiPaper
  readonly similarity?: number
  readonly onRedigest?: (id: number) => void
  readonly redigesting?: boolean
}

export default function PaperCard({ paper, similarity, onRedigest, redigesting }: PaperCardProps) {
  return (
    <div className="paper-card">
      {similarity !== undefined && (
        <div className="similarity-wrap">
          <span className="similarity-text">语义相似度 {(similarity * 100).toFixed(0)}%</span>
          <div className="similarity-bar">
            <div className="similarity-fill" style={{ width: `${similarity * 100}%` }} />
          </div>
        </div>
      )}
      <Link to={`/paper/${paper.id}`} className="title" style={{ display: 'block' }}>
        {paper.title}
      </Link>
      <p className="oneliner">
        {paper.one_liner ??
          (paper.status === 'failed'
            ? `提炼失败：${paper.fail_reason ?? '未知原因'}`
            : '尚未提炼（等待下一批次或手动触发）')}
      </p>
      <div className="meta">
        {paper.origin === 'upload' && <Badge text="手工上传" tone="gray" />}
        {paper.origin === 'discover' && <Badge text="🌐 在线检索" tone="blue" />}
        {paper.difficulty != null && (
          <Badge text={`通俗度 ${paper.difficulty}/5`} tone={difficultyTone(paper.difficulty)} />
        )}
        {paper.source_type != null && (
          <Badge
            text={paper.source_type === 'fulltext' ? '全文提炼' : '仅摘要'}
            tone={paper.source_type === 'fulltext' ? 'green' : 'orange'}
          />
        )}
        {paper.model != null && paper.status === 'digested' && (
          <Badge text={paper.model} tone="blue" />
        )}
        {paper.digested_at != null && paper.status === 'digested' && (
          <Badge text={`提炼于 ${paper.digested_at}`} tone="gray" />
        )}
        <Badge
          text={
            paper.status === 'digested' ? '已提炼' : paper.status === 'failed' ? '提炼失败' : '待提炼'
          }
          tone={
            paper.status === 'digested' ? 'green' : paper.status === 'failed' ? 'red' : 'gray'
          }
        />
        {paper.origin !== 'upload' && <Badge text={`关键词分 ${paper.score}`} tone="purple" />}
        <span>{paper.published_at ?? ''}</span>
        {paper.origin !== 'upload' && <span>arXiv:{paper.arxiv_id}</span>}
        {onRedigest && (
          <button
            className="btn secondary small"
            onClick={() => onRedigest(paper.id)}
            disabled={redigesting}
          >
            {redigesting ? '提炼中…' : '重新提炼'}
          </button>
        )}
      </div>
    </div>
  )
}
