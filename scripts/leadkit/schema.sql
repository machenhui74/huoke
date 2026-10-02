-- 线索池 schema v0｜试跑 SQLite
-- 项目：lead-intake（leadkit.pool 启动时自动执行本文件，全部 IF NOT EXISTS，可重复执行）

CREATE TABLE IF NOT EXISTS raw_comments (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'xhs',
  comment_id TEXT NOT NULL,
  note_id TEXT NOT NULL,
  content TEXT NOT NULL,
  nickname TEXT,
  creator_hash TEXT,
  create_time TEXT,
  like_count TEXT,
  source_keyword TEXT,
  note_title TEXT,
  post_url TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  expire_at TEXT NOT NULL,
  UNIQUE(platform, comment_id)
);

CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'xhs',
  comment_id TEXT NOT NULL,
  note_id TEXT NOT NULL,
  comment_text TEXT NOT NULL,
  post_url TEXT NOT NULL,
  nickname TEXT NOT NULL,
  creator_hash TEXT,
  commented_at TEXT NOT NULL,
  search_keyword TEXT,
  intent_score INTEGER NOT NULL CHECK(intent_score BETWEEN 0 AND 100),
  tags TEXT NOT NULL,
  -- 人工池要看的三件事：是不是家长、问题是什么、强不强。不触发触达。
  parent_likely INTEGER NOT NULL DEFAULT 0,
  problem TEXT,
  strength TEXT,
  status TEXT NOT NULL CHECK(status IN ('ready','needs_review','excluded')),
  exclude_reason TEXT,
  geo_hit INTEGER NOT NULL DEFAULT 0,
  target_region TEXT,
  geo_evidence TEXT,
  -- 分层地域过滤（[geo_filter]）：状态、命中的证据编码、评论者 IP 省级属地、笔记上下文（重打分不再依赖 30 天后会清掉的原始评论）
  geo_state TEXT,
  geo_signals TEXT,
  ip_province TEXT,
  note_context TEXT,
  -- 触达（人工；v0）
  scene TEXT,
  reach_status TEXT NOT NULL DEFAULT 'pending_review'
    CHECK(reach_status IN (
      'pending_review','approved','commented','dm_sent','wecom_added','rejected','skipped','none'
    )),
  reach_channel TEXT,
  wecom_qr_id TEXT,
  reach_note TEXT,
  reached_at TEXT,
  scored_at TEXT NOT NULL,
  review_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_status_score ON leads(status, intent_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_keyword ON leads(search_keyword);
CREATE INDEX IF NOT EXISTS idx_leads_region ON leads(target_region);
CREATE INDEX IF NOT EXISTS idx_leads_reach ON leads(reach_status);
CREATE INDEX IF NOT EXISTS idx_raw_expire ON raw_comments(expire_at);
