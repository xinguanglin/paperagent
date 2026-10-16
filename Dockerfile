# ---- 前端构建阶段：pnpm 打包 web/dist ----
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---- 后端运行阶段：Python + FastAPI + 托管前端产物 ----
FROM python:3.11-slim
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
COPY server/ ./server/
COPY --from=web /web/dist ./web/dist
# main.py 按 server/app/main.py 上三级定位 web/dist 与 config.yaml，均落在 /app 下
WORKDIR /app/server
EXPOSE 8787
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
