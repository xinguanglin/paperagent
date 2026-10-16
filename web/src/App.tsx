import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import Dashboard from './pages/Dashboard'
import SearchPage from './pages/SearchPage'
import DiscoverPage from './pages/DiscoverPage'
import SettingsPage from './pages/SettingsPage'
import LogsPage from './pages/LogsPage'
import PaperDetail from './pages/PaperDetail'

export default function App() {
  return (
    <BrowserRouter>
      <div className="app">
        <Sidebar />
        <main className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/discover" element={<DiscoverPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/paper/:id" element={<PaperDetail />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
