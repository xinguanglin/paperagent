import { NavLink } from 'react-router-dom'

interface NavItem {
  to: string
  icon: string
  label: string
}

const navItems: NavItem[] = [
  { to: '/', icon: '📊', label: '今日看板' },
  { to: '/search', icon: '🔍', label: '语义搜索' },
  { to: '/discover', icon: '🌐', label: '在线检索' },
  { to: '/logs', icon: '📜', label: '抓取日志' },
  { to: '/settings', icon: '⚙️', label: '订阅设置' },
]

export default function Sidebar() {
  return (
    <aside className="sidebar">
      <div className="brand">📄 论文智能体</div>
      <div className="brand-sub">每日 AI 论文 · 自动提炼</div>
      {navItems.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === '/'}
          className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
        >
          <span className="nav-icon">{item.icon}</span>
          {item.label}
        </NavLink>
      ))}
      <div className="foot">
        FastAPI + MySQL + 智谱 GLM
        <br />
        每天 08:00 自动执行
      </div>
    </aside>
  )
}
