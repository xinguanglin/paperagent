import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // 压缩时保留传统 @media (max-width:Npx) 语法——esbuild 默认会转写成 (width<=Npx)
  // 区间语法，旧浏览器静默忽略导致自适应全部失效
  build: {
    cssTarget: ['chrome99', 'firefox91', 'safari15', 'edge99'],
  },
  server: {
    // 开发模式下把 /api 转发到本地 FastAPI（8787）
    proxy: {
      '/api': 'http://127.0.0.1:8787',
    },
  },
})
