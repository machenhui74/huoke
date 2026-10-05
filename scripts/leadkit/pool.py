"""线索池：SQLite 入库、重打分、导出、统计、留存清理。

设计要点：
- 入库和重打分共用同一套更新逻辑，保证「新进来的」和「改规则后重算的」判断一致；
- 已被人点过头的线索（reach_status 在 LOCKED_REACH 里）分数和触达状态保持原样，只补三列判断；
- 导出分两份：exports/ 下的不含昵称，可外传；internal/ 下的含昵称，仅内部对照；另有一份 Excel（internal/，含用户名，高相关行标红、排在最前、笔记链接可点）；
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

from . import xlsx
from .xlsx import SheetSpec
from .geo import GEO_LABEL
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
# 中文表头的「用户名/昵称」同样要拦：可外传的 CSV 现在是中文表头，只查英文名会漏
FORBIDDEN_EXPORT_COLS = ("nickname", "xsec_token", "creator_hash", "user_id", "用户名", "昵称")
# 内部英文字段名（数据库列名）。对外的表头一律是中文，见下方 CSV_EXTRA_COLUMNS 和 XLSX_COLUMNS。
PUBLIC_FIELDS = [
    "platform", "comment_id", "post_id", "comment_text", "post_url", "search_keyword",
    "intent_score", "parent_likely", "problem", "strength", "status",
    "geo_hit", "target_region", "reach_status",
]
# 取值也翻成中文：用户打开表只看中文，不应该再看到 ready / needs_review / pending_review
STATUS_LABEL = {"ready": "高相关", "needs_review": "待复核", "low_archive": "低意向归档", "excluded": "已排除"}
REACH_LABEL = {"pending_review": "待人工审核", "approved": "已批准", "commented": "已评论", "dm_sent": "已私信",
               "wecom_added": "已加企微", "rejected": "已拒绝", "skipped": "已跳过", "none": "无"}


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
    for name, ddl in {"parent_likely": "INTEGER NOT NULL DEFAULT 0", "problem": "TEXT", "strength": "TEXT",
                      "geo_state": "TEXT", "geo_signals": "TEXT", "ip_province": "TEXT", "note_context": "TEXT"}.items():
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
            "UPDATE leads SET parent_likely=?, problem=?, strength=?, geo_state=?, geo_signals=?, updated_at=? "
            "WHERE platform=? AND comment_id=?",
            (scored["parent_likely"], scored["problem"] or None, scored["strength"], scored["geo_state"] or None,
             scored["geo_signals"] or None, now, row["platform"], row["comment_id"]),
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
    if scored["geo_state"] != (row["geo_state"] or ""):
        LOG.info("地域状态 %s %s %s→%s [%s]", row["platform"], row["comment_id"], row["geo_state"] or "-",
                 scored["geo_state"] or "-", scored["geo_signals"])
    if changed:
        LOG.info("改判 %s %s %s→%s 分 %s→%s problem=%s", row["platform"], row["comment_id"], row["status"],
                 status, row["intent_score"], scored["intent_score"], scored["problem"])
    con.execute(
        """UPDATE leads SET intent_score=?, tags=?, status=?, exclude_reason=?, geo_hit=?, target_region=?,
             geo_evidence=?, parent_likely=?, problem=?, strength=?, scene=?, reach_status=?, review_note=?,
             geo_state=?, geo_signals=?, scored_at=?, updated_at=? WHERE platform=? AND comment_id=?""",
        (int(scored["intent_score"]), scored["tags"], status, reason or None, 1 if scored["geo_hit"] else 0,
         scored["target_region"] or None, scored["geo_evidence"] or None, scored["parent_likely"],
         scored["problem"] or None, scored["strength"], _scene(profile, scored["problem"], row["scene"]),
         reach, note, scored["geo_state"] or None, scored["geo_signals"] or None, now, now,
         row["platform"], row["comment_id"]),
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
            note_ctx = r.get("note_text", "") or ""
            ip_prov = r.get("ip_province", "") or ""
            scored = scorer.score(r["text"], r["search_keyword"], {
                "nickname": r["nickname"], "note_text": note_ctx, "ip_province": ip_prov, "platform": r["platform"]})
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
                if note_ctx or ip_prov:  # 重新入库时补上旧数据缺的上下文；空值不覆盖已有的
                    con.execute("UPDATE leads SET note_context=COALESCE(NULLIF(?, ''), note_context), "
                                "ip_province=COALESCE(NULLIF(?, ''), ip_province) WHERE platform=? AND comment_id=?",
                                (note_ctx, ip_prov, r["platform"], r["comment_id"]))
                counts["locked" if outcome == "locked" else "updated"] += 1
            else:
                reach = "pending_review" if status == "ready" else "none"
                con.execute(
                    """INSERT INTO leads (id, platform, comment_id, note_id, comment_text, post_url, nickname,
                         creator_hash, commented_at, search_keyword, intent_score, tags, parent_likely, problem,
                         strength, status, exclude_reason, geo_hit, target_region, geo_evidence, scene,
                         reach_status, scored_at, created_at, updated_at,
                         geo_state, geo_signals, ip_province, note_context)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), r["platform"], r["comment_id"], r["post_id"], r["text"], r["post_url"],
                     r["nickname"], r["user_hash"] or None, r["created_at"], r["search_keyword"],
                     int(scored["intent_score"]), scored["tags"], scored["parent_likely"], scored["problem"] or None,
                     scored["strength"], status, reason or None, 1 if scored["geo_hit"] else 0,
                     scored["target_region"] or None, scored["geo_evidence"] or None,
                     _scene(profile, scored["problem"], None), reach, now, now, now,
                     scored["geo_state"] or None, scored["geo_signals"] or None, ip_prov or None, note_ctx or None),
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
            scored = scorer.score(row["comment_text"] or "", row["search_keyword"] or "", {
                "nickname": row["nickname"] or "", "note_text": row["note_context"] or "",
                "ip_province": row["ip_province"] or "", "platform": row["platform"]})
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


# Excel 表的列：(字段, 表头, 列宽, 数字列?, 链接列?)。顺序和表头是用户指定的最终交付格式，
# 不要随意增删列——要加字段先问用户。用户名是原样输出，不做隐藏。
XLSX_COLUMNS: list[tuple[str, str, float, bool, bool]] = [
    ("intent_score", "意向分", 8, True, False), ("problem", "问题类型", 12, False, False),
    ("post_title", "原帖标题", 34, False, False), ("comment_text", "评论内容", 56, False, False),
    ("commented_at", "评论时间", 19, False, False), ("nickname", "用户名", 16, False, False),
    ("search_keyword", "搜索词", 16, False, False), ("post_url", "笔记链接", 40, False, True),
    ("platform", "平台", 8, False, False),
]
# 可选的第 10 列：地域把握。由 profile 的 [export] xlsx_geo_column 打开，放在最后，前 9 列保持原样
XLSX_GEO_COLUMN = ("geo_label", "地域把握", 12, False, False)
# 「排除原因」列：仅出现在 AI智能排除 工作表
XLSX_EXCLUDE_REASON_COLUMN = ("exclude_reason", "排除原因", 24, False, False)
DEFAULT_HIGHLIGHT = ("ready",)  # 默认只有 ready（高意向）标红；profile 的 [export] highlight_status 可改
# 平台代号 → 表里显示的名字（未知代号原样显示）
PLATFORM_LABEL = {"xhs": "小红书", "dy": "抖音", "douyin": "抖音"}


def _xlsx_value(field: str, value: Any) -> Any:
    """评论时间去掉时区后缀（+0800）更好读；平台代号换成中文名。"""
    if field == "commented_at":
        return (value or "")[:19]
    if field == "platform":
        return PLATFORM_LABEL.get(value, value)
    return value


def _cell(r: sqlite3.Row, field: str) -> Any:
    """Excel 与 CSV 共用的取值：平台、时间、地域状态都显示成人话。"""
    if field == "geo_label":
        return GEO_LABEL.get(r["geo_state"] or "", "")
    return _xlsx_value(field, r[field])


# CSV 的列：前面与 Excel 完全一致（可外传的版本不含用户名），后面是方便筛选和回查的补充列。表头全部中文。
CSV_EXTRA_COLUMNS = [("status", "分级"), ("parent_likely", "像家长"), ("strength", "强度"), ("tags", "命中标签"),
                     ("target_region", "目标地区"), ("reach_status", "触达状态"), ("comment_id", "评论编号"), ("post_id", "笔记编号")]


def csv_columns(with_nickname: bool, with_geo: bool) -> list[tuple[str, str]]:
    """CSV 的 (字段, 中文表头) 列表。with_nickname=False 是可外传的脱敏版。"""
    base = [(f, h) for f, h, *_ in XLSX_COLUMNS if with_nickname or f != "nickname"]
    return base + ([(XLSX_GEO_COLUMN[0], XLSX_GEO_COLUMN[1])] if with_geo else []) + CSV_EXTRA_COLUMNS


def _csv_cell(r: sqlite3.Row, field: str) -> Any:
    v = _cell(r, field)
    if field == "status":
        return STATUS_LABEL.get(v, v)
    if field == "parent_likely":
        return "是" if v else "否"
    if field == "reach_status":
        return REACH_LABEL.get(v, v)
    return "" if v is None else v


def _write_csv(path: Path, rows: list[sqlite3.Row], cols: list[tuple[str, str]]) -> None:
    """写中文表头的 CSV（带 BOM，Excel 直接双击打开不乱码）。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in cols])
        for r in rows:
            w.writerow([_csv_cell(r, fld) for fld, _ in cols])


def _xlsx_cols(with_geo: bool = False, with_nickname: bool = True,
               with_exclude_reason: bool = False) -> list[tuple[str, str, float, bool, bool]]:
    """根据选项构建列定义列表。"""
    cols = [c for c in XLSX_COLUMNS if with_nickname or c[0] != "nickname"]
    if with_geo:
        cols = cols + [XLSX_GEO_COLUMN]
    if with_exclude_reason:
        cols = cols + [XLSX_EXCLUDE_REASON_COLUMN]
    return cols


def _build_sheet_spec(name: str, rows: list[sqlite3.Row], red: frozenset[int],
                      with_geo: bool = False, with_nickname: bool = True,
                      with_exclude_reason: bool = False) -> SheetSpec:
    """构建一个工作表的规格，用于多工作表导出。"""
    cols = _xlsx_cols(with_geo, with_nickname, with_exclude_reason)
    return SheetSpec(
        name=name,
        headers=[h for _, h, *_ in cols],
        rows=[[_cell(r, f) for f, *_ in cols] for r in rows],
        red_rows=red,
        widths=[w for _, _, w, *_ in cols],
        number_cols=frozenset(i for i, c in enumerate(cols) if c[3]),
        link_cols=frozenset(i for i, c in enumerate(cols) if c[4]),
    )


def _write_xlsx(path: Path, rows: list[sqlite3.Row], red: frozenset[int], with_geo: bool = False) -> None:
    """写最终交付的 Excel：含用户名，因此只放 internal/，不进 exports/。"""
    cols = _xlsx_cols(with_geo, with_nickname=True)

    xlsx.write_xlsx(
        path, [h for _, h, *_ in cols],
        [[_cell(r, f) for f, *_ in cols] for r in rows],
        red_rows=red, widths=[w for _, _, w, *_ in cols],
        number_cols={i for i, c in enumerate(cols) if c[3]},
        link_cols={i for i, c in enumerate(cols) if c[4]})


def export(con: sqlite3.Connection, ws: Workspace, profile: Profile,
           statuses: tuple[str, ...] = ("ready", "needs_review")) -> tuple[Path, Path, int]:
    """导出人工池。返回 (脱敏 CSV, 内部对照 CSV, 行数)。

    另有最终交付的 Excel：internal/<profile>_pool.xlsx（含用户名、笔记链接可点击、高相关行标红），
    以及 exports/<profile>_pool.xlsx（脱敏版，无昵称，可外传）。
    两个 Excel 都有两个工作表：
      - Sheet1「线索池」：ready + needs_review
      - Sheet2「AI智能排除」：所有 excluded 线索，按意向分降序，包含排除原因
    三个文件的表头和取值全部是中文；CSV 的前几列与 Excel 一致，后面多几列方便筛选。
    排序：先按状态分层（ready 在前、needs_review 其次），层内按意向分从高到低。
    不能只按分数排——否则某条 needs_review 分数偏高时会插到 ready 前面，「高相关排在最前」就不成立了。
    标红：状态在 highlight_status（默认 ready）里的整行，Excel 里浅红底 + 深红字，CSV 无颜色。
    """
    ws.exports.mkdir(parents=True, exist_ok=True)
    ws.internal.mkdir(parents=True, exist_ok=True)
    marks = ",".join("?" for _ in statuses)

    # 查询人工池线索（ready + needs_review）
    pool_rows = list(con.execute(
        f"""SELECT l.platform, l.comment_id, l.note_id AS post_id, l.comment_text, l.post_url, l.search_keyword,
               l.intent_score, l.parent_likely, l.problem, l.strength, l.tags, l.status, l.geo_hit, l.target_region,
               l.reach_status, l.nickname, l.commented_at, l.geo_state, l.exclude_reason,
               COALESCE(r.note_title, '') AS post_title
            FROM leads l LEFT JOIN raw_comments r ON r.platform = l.platform AND r.comment_id = l.comment_id
            WHERE l.status IN ({marks})
            ORDER BY CASE l.status WHEN 'ready' THEN 0 WHEN 'needs_review' THEN 1 ELSE 2 END,
                     l.intent_score DESC, l.platform, l.comment_id""", statuses))

    # 查询排除线索（excluded），按意向分降序排列
    excluded_rows = list(con.execute(
        """SELECT l.platform, l.comment_id, l.note_id AS post_id, l.comment_text, l.post_url, l.search_keyword,
               l.intent_score, l.parent_likely, l.problem, l.strength, l.tags, l.status, l.geo_hit, l.target_region,
               l.reach_status, l.nickname, l.commented_at, l.geo_state, l.exclude_reason,
               COALESCE(r.note_title, '') AS post_title
            FROM leads l LEFT JOIN raw_comments r ON r.platform = l.platform AND r.comment_id = l.comment_id
            WHERE l.status = 'excluded'
            ORDER BY l.intent_score DESC, l.platform, l.comment_id"""))

    highlight = set(profile.section("export").get("highlight_status", DEFAULT_HIGHLIGHT))
    red = frozenset(i for i, r in enumerate(pool_rows) if r["status"] in highlight)
    with_geo = bool(profile.section("export").get("xlsx_geo_column", False))

    # CSV 只导出人工池（ready + needs_review），不包含 excluded
    public_csv = ws.exports / f"{profile.name}_pool.csv"
    internal_csv = ws.internal / f"{profile.name}_pool_with_nickname.csv"
    _write_csv(public_csv, pool_rows, csv_columns(False, with_geo))
    _check_header(public_csv)
    _write_csv(internal_csv, pool_rows, csv_columns(True, with_geo))

    # 构建双工作表 Excel
    # Sheet1: 线索池（人工池）
    pool_sheet_internal = _build_sheet_spec("线索池", pool_rows, red, with_geo, with_nickname=True)
    pool_sheet_public = _build_sheet_spec("线索池", pool_rows, red, with_geo, with_nickname=False)

    # Sheet2: AI智能排除（包含排除原因列，无标红）
    excluded_sheet_internal = _build_sheet_spec(
        "AI智能排除", excluded_rows, frozenset(), with_geo, with_nickname=True, with_exclude_reason=True)
    excluded_sheet_public = _build_sheet_spec(
        "AI智能排除", excluded_rows, frozenset(), with_geo, with_nickname=False, with_exclude_reason=True)

    # 写入内部版 Excel（含用户名）
    xlsx.write_xlsx_multi(xlsx_path(ws, profile), [pool_sheet_internal, excluded_sheet_internal])

    # 写入公开版 Excel（脱敏，无用户名）
    public_xlsx = xlsx_path_public(ws, profile)
    xlsx.write_xlsx_multi(public_xlsx, [pool_sheet_public, excluded_sheet_public])

    LOG.info("导出人工池 %d 条（标红 %d）+ 排除 %d 条：Excel %s / %s（脱敏）；CSV %s（脱敏）/ %s（含昵称）",
             len(pool_rows), len(red), len(excluded_rows),
             xlsx_path(ws, profile), public_xlsx, public_csv, internal_csv)
    return public_csv, internal_csv, len(pool_rows)


def xlsx_path(ws: Workspace, profile: Profile) -> Path:
    """最终交付 Excel 的位置（含用户名，所以放 internal/）。CLI 和测试都用这个函数找文件。"""
    return ws.internal / f"{profile.name}_pool.xlsx"


def xlsx_path_public(ws: Workspace, profile: Profile) -> Path:
    """脱敏版 Excel 的位置（无用户名，放 exports/ 可外传）。"""
    return ws.exports / f"{profile.name}_pool.xlsx"


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
