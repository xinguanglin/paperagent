# paper-agent

AI 论文智能体:自动检索最新 arXiv 论文,由 LLM 提炼为结构化中文解读,提供 Web 界面管理与对话。

## 功能

- **双源检索**:arXiv / OpenAlex,支持定时批次与在线检索一键入库
- **深度提炼**:一句话总结、提纲、核心要点、实现步骤、原理拆解、局限与通俗度评分
- **原文图表理解**:公式直提(lateXML alttext)+ 视觉模型识图(公式转 LaTeX、实验图读数),注入解读
- **图示生成**:按论文类型自动生成 Mermaid 流程图 / 系统架构图
- **缓存**:全文与图片本地落库,重新提炼不依赖外网
- **检索与对话**:标题语义搜索、基于解读内容的多轮问答,支持上传 PDF / DOCX
- **模型无关**:任何 OpenAI 兼容服务,仅需配置地址、密钥与模型名

## 部署

### Docker

```bash
cp config.docker.example.yaml config.docker.yaml   # 编辑并填入 api_key
docker compose up -d --build
```

访问 `http://localhost:8787`。数据存储于命名卷,`docker compose down` 不丢失。

### 手动

依赖 Python 3.11+、Node.js + pnpm、MySQL 8。

```bash
pip install -r server/requirements.txt
cp config.example.yaml config.yaml                 # 编辑并填写配置
(cd web && pnpm install && pnpm build)
cd server && python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
```

表结构由应用启动时自动创建与迁移,无需手动建表。

## 配置说明

| 字段 | 说明 |
|---|---|
| `api_key` / `base_url` / `chat_model` | OpenAI 兼容服务的密钥、地址与模型名 |
| `vision_model` | 视觉识图模型,留空关闭识图 |
| `embedding_model` / `embedding_dims` | 语义检索向量模型与维度 |
| `proxy` | HTTP 代理,arXiv 不可达或被限流时使用 |
| `source` | `auto`(arXiv 优先,自动降级 OpenAlex)/ `arxiv` / `openalex` |
| `mysql.*` | MySQL 连接信息 |

## 项目结构

```
paper-agent/
├─ Dockerfile / docker-compose.yaml
├─ config.example.yaml / config.docker.example.yaml
├─ db/schema.sql
├─ server/
│  ├─ app/                    # FastAPI 后端
│  │  ├─ llm_client.py        # OpenAI 兼容 LLM 客户端(对话/视觉/embedding)
│  │  ├─ pipeline.py          # 提炼管线:批次/排队/缓存/识图编排
│  │  ├─ figures.py           # 原文图片下载与校验压缩
│  │  ├─ html_fetcher.py      # arXiv HTML 解析:纯文本 + 公式 + 图片清单
│  │  └─ routers/             # API 路由
│  └─ tests/                  # 离线测试
└─ web/                       # React + Vite 前端
```

## License

[MIT](LICENSE)
