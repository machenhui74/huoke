"""线索池：SQLite 入库、重打分、导出、统计、留存清理。

设计要点：
- 入库和重打分共用同一套更新逻辑，保证「新进来的」和「改规则后重算的」判断一致；
- 已被人点过头的线索（reach_status 在 LOCKED_REACH 里）分数和触达状态保持原样，只补三列判断；
- 导出分两份：exports/ 下的不含昵称，可外传；internal/ 下的含昵称，仅内部对照；
- 任何导出都做一遍「禁止列」自检，泄露就中止。
"""
from __future__ import annotations

import csv
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .logger import get_logger
from .paths import Workspace
from .profile import Profile
from .scoring import Scorer

LOG = get_logger("pool")
SCHEMA = Path(__file__).with_name("schema.sql")

# 人已经点过头的状态：分数和触达状态不再被自动流程改动
LOCKED_REACH = ("approved", "commented", "dm_sent", "wecom_added", "rejected", "skipped")
REACH_STATUSES = ("pending_review", "approved", "commented", "dm_sent", "wecom_added", "rejected", "skipped", "none")
# 绝不能出现在可外传导出里的列
FORBIDDEN_EXPORT_COLS = ("nickname", "xsec_token", "creator_hash", "user_id")
PUBLIC_FIELDS = [
    "platform", "comment_id", "post_id", "comment_text", "post_url", "search_keyword",
    "intent_score", "parent_likely", "problem", "strength", "status",
    "geo_hit", "target_region", "reach_status",
]


def stamp(profile: Profile, when: datetime | None = None) -> str:
    """统一时间戳格式：2026-10-01 16:00:00+0800。"""
    return (when or datetime.now(profile.tz)).strftime("%Y-%m-%d %H:%M:%S%z")


def connect(db_path: Path) -> sqlite3.Connection:
    """打开库并保证表结构存在；旧库缺列时自动补齐。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    have = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    for name, ddl in {"parent_likely": "INTEGER NOT NULL DEFAULT 0", "problem": "TEXT", "strength": "TEXT"}.items():
        if name not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {name} {ddl}")
            LOG.info("leads 补列 %s", name)
    con.commit()
    LOG.debug("已连接 %s", db_path)
    return con


def pool_status(scored: dict[str, Any]) -> tuple[str, str]:
    """low_archive 在库里记 excluded，原因里保留原档，方便和硬排除分开。"""
    if scored["status"] == "low_archive":
        return "excluded", scored["exclude_reason"] or f"low_archive score={scored['intent_score']}"
    return scored["status"], scored["exclude_reason"]


def _scene(profile: Profile, problem: str, current: str | None) -> str | None:
    """已有人工场景不覆盖；问题类型能直接对上话术场景的才自动填。"""
    if current:
        return current
    return problem if problem in profile.section("outreach").get("scene_from_problem", []) else None


def _backup(db_path: Path, tag: str) -> Path | None:
    """改库前每天备份一次。库不存在（首次入库）时跳过。"""
    if not db_path.exists():
        return None
    bak = db_path.with_name(f"{db_path.name}.bak-{tag}-{datetime.now().strftime('%Y%m%d')}")
    if not bak.exists():
        shutil.copy2(db_path, bak)
        LOG.info("已备份 %s", bak.name)
    return bak


def _update_scored(con: sqlite3.Connection, profile: Profile, row: sqlite3.Row, scored: dict[str, Any], now: str) -> str:
    """把一次打分结果写回已存在的线索行，返回 changed / locked / same。"""
    status, reason = pool_status(scored)
    if row["reach_status"] in LOCKED_REACH:
        con.execute(
            "UPDATE leads SET parent_likely=?, problem=?, strength=?, updated_at=? WHERE platform=? AND comment_id=?",
            (scored["parent_likely"], scored["problem"] or None, scored["strength"], now, row["platform"], row["comment_id"]),
        )
        LOG.info("已触达冻结 %s %s，只补判断列", row["platform"], row["comment_id"])
        return "locked"

    reach = row["reach_status"]
    if status == "ready" and reach == "none":
        reach = "pending_review"
    if status != "ready" and reach == "pending_review":
        reach = "none"
    note = row["review_note"]
    if status == "needs_review" and row["status"] != "needs_review" and not note:
        note = "进人工池，不自动触达"
    changed = (status != row["status"] or int(scored["intent_score"]) != int(row["intent_score"])
               or (scored["problem"] or "") != (row["problem"] or ""))
    if changed:
        LOG.info("改判 %s %s %s→%s 分 %s→%s problem=%s", row["platform"], row["comment_id"], row["status"],
                 status, row["intent_score"], scored["intent_score"], scored["problem"])
    con.execute(
        """UPDATE leads SET intent_score=?, tags=?, status=?, exclude_reason=?, geo_hit=?, target_region=?,
             geo_evidence=?, parent_likely=?, problem=?, strength=?, scene=?, reach_status=?, review_note=?,
             scored_at=?, updated_at=? WHERE platform=? AND comment_id=?""",
        (int(scored["intent_score"]), scored["tags"], status, reason or None, 1 if scored["geo_hit"] else 0,
         scored["target_region"] or None, scored["geo_evidence"] or None, scored["parent_likely"],
         scored["problem"] or None, scored["strength"], _scene(profile, scored["problem"], row["scene"]),
         reach, note, now, now, row["platform"], row["comment_id"]),
    )
    return "changed" if changed else "same"


def ingest(con: sqlite3.Connection, profile: Profile, scorer: Scorer, records: list[dict[str, str]],
           batch_id: str) -> dict[str, int]:
    """标准化后的评论 → 打分 → 写 raw_comments 与 leads。幂等：同一条评论重复入库只会更新。"""
    now = stamp(profile)
    retention = int(profile.section("collect").get("retention_raw_days", 30))
    expire = stamp(profile, datetime.now(profile.tz) + timedelta(days=retention))
    counts = {"new": 0, "updated": 0, "locked": 0, "ready": 0, "needs_review": 0, "excluded": 0}
    try:
        con.execute("BEGIN")
        for r in records:
            scored = scorer.score(r["text"], r["search_keyword"])
            status, reason = pool_status(scored)
            con.execute(
                """INSERT INTO raw_comments (id, platform, comment_id, note_id, content, nickname, creator_hash,
                     create_time, like_count, source_keyword, note_title, post_url, imported_at, batch_id, expire_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform, comment_id) DO UPDATE SET content=excluded.content,
                     nickname=excluded.nickname, like_count=excluded.like_count, source_keyword=excluded.source_keyword,
                     note_title=excluded.note_title, post_url=excluded.post_url, imported_at=excluded.imported_at,
                     batch_id=excluded.batch_id, expire_at=excluded.expire_at""",
                (str(uuid.uuid4()), r["platform"], r["comment_id"], r["post_id"], r["text"], r["nickname"],
                 r["user_hash"], r["created_at"], r["like_count"], r["search_keyword"], r["post_title"],
                 r["post_url"], now, batch_id, expire),
            )
            existing = con.execute("SELECT * FROM leads WHERE platform=? AND comment_id=?",
                                   (r["platform"], r["comment_id"])).fetchone()
            if existing:
                outcome = _update_scored(con, profile, existing, scored, now)
                counts["locked" if outcome == "locked" else "updated"] += 1
            else:
                reach = "pending_review" if status == "ready" else "none"
                con.execute(
                    """INSERT INTO leads (id, platform, comment_id, note_id, comment_text, post_url, nickname,
                         creator_hash, commented_at, search_keyword, intent_score, tags, parent_likely, problem,
                         strength, status, exclude_reason, geo_hit, target_region, geo_evidence, scene,
                         reach_status, scored_at, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), r["platform"], r["comment_id"], r["post_id"], r["text"], r["post_url"],
                     r["nickname"], r["user_hash"] or None, r["created_at"], r["search_keyword"],
                     int(scored["intent_score"]), scored["tags"], scored["parent_likely"], scored["problem"] or None,
                     scored["strength"], status, reason or None, 1 if scored["geo_hit"] else 0,
                     scored["target_region"] or None, scored["geo_evidence"] or None,
                     _scene(profile, scored["problem"], None), reach, now, now, now),
                )
                counts["new"] += 1
            counts[status] += 1
        con.commit()
    except Exception:
        con.rollback()  # 半批数据比没有数据更糟：整批回滚
        LOG.exception("入库失败，整批已回滚")
        raise
    LOG.info("入库 batch=%s：新增 %d 更新 %d 冻结 %d | ready %d 复核 %d 排除 %d", batch_id, counts["new"],
             counts["updated"], counts["locked"], counts["ready"], counts["needs_review"], counts["excluded"])
    return counts


def rescore(con: sqlite3.Connection, profile: Profile, scorer: Scorer, db_path: Path) -> dict[str, int]:
    """用当前规则重写库里所有线索的判断。只改库，不采集、不触达。"""
    _backup(db_path, "rescore")
    now = stamp(profile)
    counts = {"total": 0, "changed": 0, "locked": 0}
    try:
        con.execute("BEGIN")
        rows = list(con.execute("SELECT * FROM leads"))
        LOG.info("重打分 %d 条线索", len(rows))
        for row in rows:
            scored = scorer.score(row["comment_text"] or "", row["search_keyword"] or "")
            outcome = _update_scored(con, profile, row, scored, now)
            counts["total"] += 1
            if outcome in counts:
                counts[outcome] += 1
        con.commit()
    except Exception:
        con.rollback()
        LOG.exception("重打分失败，已回滚")
        raise
    LOG.info("重打分完成：改判 %d，冻结 %d", counts["changed"], counts["locked"])
    return counts


def _check_header(path: Path) -> None:
    header = path.read_text(encoding="utf-8-sig").splitlines()[0].lower()
    leaked = [c for c in FORBIDDEN_EXPORT_COLS if c in header]
    if leaked:
        path.unlink(missing_ok=True)
        raise SystemExit(f"导出泄露了禁止列 {leaked}，已删除 {path}")


def export(con: sqlite3.Connection, ws: Workspace, profile: Profile,
           statuses: tuple[str, ...] = ("ready", "needs_review")) -> tuple[Path, Path, int]:
    """导出人工池。返回 (脱敏文件, 内部对照文件, 行数)。"""
    ws.exports.mkdir(parents=True, exist_ok=True)
    ws.internal.mkdir(parents=True, exist_ok=True)
    marks = ",".join("?" for _ in statuses)
    rows = list(con.execute(
        f"""SELECT platform, comment_id, note_id AS post_id, comment_text, post_url, search_keyword, intent_score,
               parent_likely, problem, strength, tags, status, geo_hit, target_region, reach_status, nickname
            FROM leads WHERE status IN ({marks}) ORDER BY intent_score DESC, platform""", statuses))
    public = ws.exports / f"{profile.name}_pool.csv"
    internal = ws.internal / f"{profile.name}_pool_with_nickname.csv"
    with public.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PUBLIC_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in PUBLIC_FIELDS})
    _check_header(public)
    with internal.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PUBLIC_FIELDS + ["nickname"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in PUBLIC_FIELDS + ["nickname"]})
    LOG.info("导出 %d 条：%s（脱敏）/ %s（含昵称，仅内部）", len(rows), public, internal)
    return public, internal, len(rows)


def stats(con: sqlite3.Connection) -> dict[str, Any]:
    """池子概况，给人和 agent 一眼看清现状。"""
    q = lambda sql: {str(k): v for k, v in con.execute(sql).fetchall()}
    return {
        "总线索": con.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
        "按状态": q("SELECT status, COUNT(*) FROM leads GROUP BY 1"),
        "按平台": q("SELECT platform, COUNT(*) FROM leads GROUP BY 1"),
        "人工池按问题": q("SELECT COALESCE(problem,'(未归类)'), COUNT(*) FROM leads WHERE status IN ('ready','needs_review') GROUP BY 1"),
        "触达状态": q("SELECT reach_status, COUNT(*) FROM leads GROUP BY 1"),
    }


def mark(con: sqlite3.Connection, profile: Profile, platform: str, comment_id: str, reach: str, note: str = "") -> bool:
    """人工标记触达状态。标过之后自动流程不再改这条线索的分数。"""
    if reach not in REACH_STATUSES:
        raise ValueError(f"reach 只能是 {REACH_STATUSES}")
    cur = con.execute(
        "UPDATE leads SET reach_status=?, reach_note=COALESCE(NULLIF(?, ''), reach_note), reached_at=?, updated_at=? "
        "WHERE platform=? AND comment_id=?",
        (reach, note, stamp(profile) if reach in ("commented", "dm_sent", "wecom_added") else None,
         stamp(profile), platform, comment_id))
    con.commit()
    LOG.info("标记 %s/%s → %s (%d 行)", platform, comment_id, reach, cur.rowcount)
    return cur.rowcount > 0


def purge(con: sqlite3.Connection, profile: Profile, dry_run: bool = True) -> dict[str, int]:
    """按留存期清理：过期原始评论、过期的低分/排除线索。人工池（ready/needs_review）不清。"""
    coll = profile.section("collect")
    now = datetime.now(profile.tz)
    raw_cut = stamp(profile, now)  # raw_comments.expire_at 已经是到期时间，直接和现在比
    ex_cut = stamp(profile, now - timedelta(days=int(coll.get("retention_excluded_days", 14))))
    n_raw = con.execute("SELECT COUNT(*) FROM raw_comments WHERE expire_at < ?", (raw_cut,)).fetchone()[0]
    n_ex = con.execute(
        "SELECT COUNT(*) FROM leads WHERE status='excluded' AND reach_status='none' AND created_at < ?", (ex_cut,)
    ).fetchone()[0]
    if not dry_run:
        con.execute("DELETE FROM raw_comments WHERE expire_at < ?", (raw_cut,))
        con.execute("DELETE FROM leads WHERE status='excluded' AND reach_status='none' AND created_at < ?", (ex_cut,))
        con.commit()
    LOG.info("清理%s：原始评论 %d 条，排除线索 %d 条", "（预演）" if dry_run else "", n_raw, n_ex)
    return {"raw_comments": n_raw, "excluded_leads": n_ex}
