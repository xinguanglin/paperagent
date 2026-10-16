-- 论文智能体数据库结构
-- 语义检索基于论文标题的 embedding（存于 papers.embedding），不做全文向量

CREATE DATABASE IF NOT EXISTS paper_agent
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE paper_agent;

-- 论文主表：arXiv 检索结果 + 标题向量
CREATE TABLE IF NOT EXISTS papers (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  arxiv_id     VARCHAR(32)  NOT NULL COMMENT 'arXiv 唯一ID，如 2603.01234',
  version      VARCHAR(8)   NOT NULL DEFAULT 'v1',
  title        VARCHAR(512) NOT NULL,
  authors      VARCHAR(1024),
  abstract     TEXT,
  categories   VARCHAR(255) COMMENT '逗号分隔，如 cs.AI,cs.CL',
  pdf_url      VARCHAR(512),
  html_url     VARCHAR(512),
  score        INT          NOT NULL DEFAULT 0 COMMENT '关键词命中打分',
  status       ENUM('fetched','digested','failed') NOT NULL DEFAULT 'fetched',
  fail_reason  VARCHAR(512),
  embedding    BLOB COMMENT '标题 embedding-3 向量 float32[]，语义检索唯一依据',
  figures      JSON         COMMENT '原文图片清单与识图结果 [{idx,url,path,caption,status,description},...]',
  published_at DATETIME,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_arxiv (arxiv_id),
  KEY idx_status (status),
  KEY idx_published (published_at)
) ENGINE=InnoDB COMMENT='论文主表';

-- 提炼结果表：GLM 生成的结构化解读
CREATE TABLE IF NOT EXISTS digests (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  paper_id     BIGINT       NOT NULL,
  one_liner    VARCHAR(1024) COMMENT '一句话总结',
  outline      JSON         COMMENT '论文提纲 [{section, summary}]',
  key_points   JSON         COMMENT '核心要点 [{point, detail}]',
  diagrams     JSON         COMMENT 'mermaid 图示 [{type, caption, mermaid}]，不适用的论文为 NULL',
  implementation TEXT       COMMENT '实现过程拆解（流程/模块/配置）',
  principle    TEXT         COMMENT '原理拆解（大白话+类比）',
  limitations  TEXT         COMMENT '局限与值得关注的原因',
  difficulty   TINYINT      COMMENT '通俗度 1-5，越大越需要基础',
  truncated    TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '全文是否超限截断',
  source_type  ENUM('fulltext','abstract') NOT NULL COMMENT '全文提炼 / 降级仅摘要',
  model        VARCHAR(64)  COMMENT '使用的 GLM 模型',
  tokens_used  INT          NOT NULL DEFAULT 0,
  digested_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_paper (paper_id),
  CONSTRAINT fk_digests_paper FOREIGN KEY (paper_id) REFERENCES papers (id) ON DELETE CASCADE
) ENGINE=InnoDB COMMENT='论文提炼结果';

-- 批次执行历史
CREATE TABLE IF NOT EXISTS runs (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  trigger_type    ENUM('scheduled','manual') NOT NULL,
  status          ENUM('running','success','partial','failed') NOT NULL,
  fetched_count   INT NOT NULL DEFAULT 0,
  succeeded_count INT NOT NULL DEFAULT 0,
  failed_count    INT NOT NULL DEFAULT 0,
  total_tokens    INT NOT NULL DEFAULT 0,
  error_summary   TEXT,
  started_at      DATETIME NOT NULL,
  finished_at     DATETIME
) ENGINE=InnoDB COMMENT='定时/手动批次执行历史';

-- 订阅配置（单行表）。敏感信息（API Key、MySQL 连接）不入库，放 config.yaml
CREATE TABLE IF NOT EXISTS settings (
  id         TINYINT      PRIMARY KEY DEFAULT 1,
  categories VARCHAR(255) NOT NULL DEFAULT 'cs.AI,cs.CL,cs.LG',
  keywords   JSON         NOT NULL COMMENT '[{"word":"LLM","weight":5},...]',
  sources    JSON         COMMENT '论文来源优先级列表，如 ["arxiv","openalex"]；空则回退 config.yaml 的 source',
  top_n      INT          NOT NULL DEFAULT 20,
  cron_expr  VARCHAR(32)  NOT NULL DEFAULT '0 8 * * *',
  chat_model VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '对话/解读模型；空 = 回退 config.yaml 的 chat_model',
  vision_model VARCHAR(64) COMMENT '视觉识图模型；空 = 回退 config.yaml 的 vision_model，两者皆空 = 关闭识图',
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB COMMENT='订阅设置（单行）';

-- 初始化默认配置行与默认关键词
INSERT INTO settings (id, keywords) VALUES (1, JSON_ARRAY(
  JSON_OBJECT('word', 'LLM',       'weight', 5),
  JSON_OBJECT('word', 'agent',     'weight', 4),
  JSON_OBJECT('word', 'reasoning', 'weight', 4),
  JSON_OBJECT('word', 'RAG',       'weight', 3),
  JSON_OBJECT('word', 'multimodal','weight', 3),
  JSON_OBJECT('word', 'RLHF',      'weight', 3)
)) ON DUPLICATE KEY UPDATE id = id;
