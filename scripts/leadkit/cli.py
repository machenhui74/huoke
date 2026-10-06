"""leadctl 命令行入口。

退出码约定（方便脚本和 agent 判断）：
  0 成功    1 运行失败/自检失败    2 护栏拒绝或参数错误    3+ 安装步骤失败
结果写 stdout，日志写 stderr；加 --json 时 stdout 为单个 JSON，便于程序解析。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, keywords as kw, pool
from .collectors import BACKENDS
from .guard import HARD_CEILING, CollectPlan, effective_limits
from .logger import get_logger, setup_logging
from .normalize import load_generic_csv, load_mediacrawler
from .paths import Workspace
from .profile import Profile, ProfileError, list_profiles, load_platform_map, load_profile
from .queue import MAX_ATTEMPTS, KeywordQueue, parse_proposal, parse_words
from .scoring import Scorer
from .setup import PINNED_REF, doctor, setup

LOG = get_logger("cli")
DEFAULT_PROFILE = os.environ.get("LEADKIT_PROFILE", "education_taizhou")


def _emit(args: argparse.Namespace, payload: Any, human: str = "") -> None:
    """--json 时输出 JSON，否则输出给人看的文本。"""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif human:
        print(human)


def _ctx(args: argparse.Namespace) -> tuple[Workspace, Profile]:
    ws = Workspace.resolve(args.workdir)
    ws.ensure()
    setup_logging(ws.logs, verbose=args.verbose, quiet=args.quiet)
    return ws, load_profile(args.profile, ws)


def _scorer_or_die(profile: Profile) -> Scorer:
    """打分前先跑自检：规则改坏了就不要往库里写。"""
    scorer = Scorer(profile)
    bad = scorer.self_check()
    if bad:
        print(f"profile「{profile.name}」自检失败 {len(bad)} 条，拒绝继续。运行 leadctl check 查看详情。", file=sys.stderr)
        raise SystemExit(1)
    return scorer


def _queue(ws: Workspace, prof: Profile) -> KeywordQueue:
    """每个 profile 一份队列：品类/地区不同，词表就不同。"""
    return KeywordQueue(ws.state / f"queue_{prof.name}.json")


def _mc_dir(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "mc_dir", None) or os.environ.get("LEADKIT_MC_DIR")
    return Path(raw).expanduser().resolve() if raw else None


# ---------------- 子命令 ----------------
def cmd_profiles(args: argparse.Namespace) -> int:
    ws = Workspace.resolve(args.workdir)
    names = list_profiles(ws)
    _emit(args, names, "\n".join(names))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    _, prof = _ctx(args)
    bad = Scorer(prof).self_check()
    total = len(prof.data.get("cases", []))
    lines = [f"profile {prof.name}：{total} 条自检用例，失败 {len(bad)} 条"]
    for b in bad:
        c, g = b["case"], b["got"]
        lines.append(f"  ✘ {c['text']!r}\n    期望 {c['status']}/{c['parent']}/{c['problem']}/{c['strength']}"
                     f"  实际 {g['status']}/{g['parent_likely']}/{g['problem']}/{g['strength']} 分={g['intent_score']}")
    _emit(args, {"profile": prof.name, "total": total, "failed": len(bad)}, "\n".join(lines))
    return 1 if bad else 0


def cmd_setup(args: argparse.Namespace) -> int:
    ws = Workspace.resolve(args.workdir)
    ws.ensure()
    setup_logging(ws.logs, verbose=args.verbose, quiet=args.quiet)
    return setup(ws, args.ref, _mc_dir(args), args.skip_sync, args.with_raw_identity, args.with_ip_province)


def cmd_doctor(args: argparse.Namespace) -> int:
    ws = Workspace.resolve(args.workdir)
    setup_logging(None, verbose=args.verbose, quiet=True)  # doctor 的输出就是报告本身，不夹杂日志
    checks = doctor(ws, args.profile, _mc_dir(args))
    lines = []
    for c in checks:
        mark = "✔" if c.ok else ("✘" if c.required else "!")
        lines.append(f"  {mark} {c.name}" + (f"  {c.detail}" if c.detail else ""))
        if not c.ok and c.fix:
            lines.append(f"      → {c.fix}")
    hard_fail = [c for c in checks if not c.ok and c.required]
    lines.append("\n环境就绪" if not hard_fail else f"\n有 {len(hard_fail)} 项必需检查未通过")
    _emit(args, [c.__dict__ for c in checks], "\n".join(lines))
    return 1 if hard_fail else 0


def cmd_keywords(args: argparse.Namespace) -> int:
    ws, prof = _ctx(args)
    category, place = "".join(args.category.split()), "".join(args.place.split())
    if not category or not place:
        print("--category 和 --place 不能为空", file=sys.stderr)
        return 2
    phrases, nearby = kw.expand(category, place, prof)
    # --add：调用方（agent 或人）自己补的词，比如家长的真实说法、症状词。排在模板词前面，因为通常更贴近真实搜索。
    extra = [w for w in parse_words(args.add or "") if w not in phrases]
    phrases = extra + phrases
    lines = kw.format_lines(phrases, nearby)
    path = kw.write_proposal(category, place, lines, ws.root / "keywords", args.out or "")
    payload: dict[str, Any] = {"keywords": phrases, "nearby": nearby, "file": str(path)}
    tail = f"\n\n已写入 {path}\n加 --enqueue 可存入待采队列，之后用 leadctl collect --next 分批采；或自行挑 ≤{HARD_CEILING['max_keywords']} 条用 --keywords"
    if args.enqueue:
        platform = args.platform or prof.section("collect").get("platform", "xhs")
        # 附近区的词标了「仅备选、不要采」，不入队；agent 补的词来源记为 agent
        usable = [w for w in phrases if not any(w.startswith(n) for n in nearby)]
        q = _queue(ws, prof)
        res = q.add(platform, [w for w in usable if w in extra], "agent")
        res2 = q.add(platform, [w for w in usable if w not in extra], "expand")
        res = {"added": res["added"] + res2["added"], "skipped": {**res["skipped"], **res2["skipped"]}}
        payload["enqueued"] = res
        tail = (f"\n\n已写入 {path}\n已入队 {len(res['added'])} 个词（平台 {platform}），跳过 {len(res['skipped'])} 个。"
                f"附近区的备选词未入队。\n下一步：leadctl collect --platform {platform} --next（先预检，确认后加 --yes）")
    _emit(args, payload, "\n".join(lines) + tail)
    return 0


_parse_keywords = parse_words  # 旧名保留，逻辑已挪到 queue.py 与队列共用


def cmd_collect(args: argparse.Namespace) -> int:
    ws, prof = _ctx(args)
    platform = args.platform or prof.section("collect").get("platform", "xhs")
    try:
        pmap = load_platform_map(platform)
    except ProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    limits = effective_limits(prof.section("limits"), args.allow_exceed_limits)
    collector = BACKENDS[args.backend](ws, prof, _mc_dir(args))
    notes = args.notes if args.notes is not None else limits["max_notes_per_keyword"]

    # 词的来源二选一：--keywords 手动给，或 --next 从队列取
    queue = _queue(ws, prof) if args.next else None
    picked: list[str] = []
    if args.next and args.keywords:
        print("--keywords 和 --next 只能二选一", file=sys.stderr)
        return 2
    if not args.next and not args.keywords:
        print("需要 --keywords \"词1,词2\"，或 --next 从队列取词", file=sys.stderr)
        return 2
    if queue:
        # 按今日剩余配额决定取几个词，而不是固定取满 3 个：配额不够就少取，别让预检拒绝整批
        used = collector.ledger.usage_today(platform)
        room = min((limits["daily_note_details"] - used["notes"]) // max(1, notes),
                   (limits["daily_comments"] - used["comments"]) // max(1, notes))
        # --max-words：新平台首次实机、或想分多次小量采时，主动少取几个（只能更少，不能超过额度允许的数量）
        cap = args.max_words if getattr(args, "max_words", None) else limits["max_keywords"]
        take = max(1, min(limits["max_keywords"], room, cap))
        picked = [i["keyword"] for i in queue.pending(platform)[:take]]
        left = queue.counts(platform)
        if not picked:
            msg = f"平台 {platform} 的待采队列是空的（已完成 {left['done']}，卡住 {left['stuck']}）。用 leadctl keywords --enqueue 或 queue add 补词"
            _emit(args, {"ok": True, "empty": True, "queue": left}, msg)
            return 0
        LOG.info("从队列取词 %s（今日剩余可容纳 %d 个词，队列待采 %d）", picked, room, left["pending"])
    keywords = picked or _parse_keywords(args.keywords)
    if args.comments is not None:
        comments = args.comments
    else:
        # 各项上限同时取满会超日配额（3词×5篇×5评=75 > 50），所以默认值按「今日剩余评论配额」反推，
        # 而不是直接取单帖上限。显式传 --comments 则以显式值为准（超了会在预检被拒）。
        remaining = limits["daily_comments"] - collector.ledger.usage_today(platform)["comments"]
        comments = max(1, min(limits["max_comments_per_note"], remaining // max(1, len(keywords) * notes)))
        LOG.info("单帖评论数未指定，按今日剩余配额 %d 自动取 %d", remaining, comments)
    plan = CollectPlan(
        platform=platform, keywords=keywords, notes_per_keyword=notes, comments_per_note=comments,
        concurrency=limits["max_concurrency"], sleep_sec=args.sleep if args.sleep is not None else limits["min_sleep_sec"],
        login=prof.section("collect").get("login", "qrcode"),
    )
    batch_id = f"{platform}-{datetime.now(prof.tz).strftime('%Y%m%d-%H%M%S')}"
    batch_dir = ws.raw / batch_id

    issues = collector.preflight(plan, pmap, allow_exceed=args.allow_exceed_limits,
                                 allow_unverified=args.allow_unverified_platform)
    print(collector.describe(plan, batch_dir, pmap), file=sys.stderr)
    if queue:
        print(f"来源: 待采队列（本次取 {len(keywords)} 个，之后还剩 {left['pending'] - len(keywords)} 个待采）", file=sys.stderr)
    if issues:
        _emit(args, {"ok": False, "refused": issues},
              "\n拒绝执行，原因：\n" + "\n".join(f"  ✘ {i}" for i in issues))
        return 2
    if not args.yes:
        _emit(args, {"ok": True, "dry_run": True, "batch_id": batch_id},
              "\n预检通过（dry-run，未采集）。确认无误后加 --yes 真正执行。")
        return 0

    res = collector.run(plan, pmap, batch_id, batch_dir)
    if queue:
        if res.ok:
            queue.mark_done(platform, keywords, batch_id)
        else:
            # 被风控信号打断或用户中断不是词的问题，不计失败次数；其余（超量、进程失败）才计
            queue.mark_failed(platform, keywords, batch_id, count=res.status not in ("aborted:block", "interrupted"))
    payload = {"ok": res.ok, "batch_id": res.batch_id, "batch_dir": str(res.batch_dir), "status": res.status,
               "notes": res.notes, "comments": res.comments, "reason": res.reason}
    if queue:
        payload["queue"] = queue.counts(platform)
    _emit(args, payload, f"采集 {res.status}：笔记 {res.notes} 篇 / 评论 {res.comments} 条\n产物：{res.batch_dir}"
          + (f"\n原因：{res.reason}" if res.reason else ""))
    if res.ok and args.ingest:
        ns = argparse.Namespace(**{**vars(args), "input": str(batch_dir), "text_col": None, "batch_id": batch_id})
        return cmd_ingest(ns)
    return 0 if res.ok else 1


def cmd_queue(args: argparse.Namespace) -> int:
    """待采队列管理：add / list / skip / retry。"""
    ws, prof = _ctx(args)
    platform = args.platform or prof.section("collect").get("platform", "xhs")
    q = _queue(ws, prof)
    words = parse_words(args.words or "")
    if args.action == "add":
        if args.from_proposal:
            words += parse_proposal(Path(args.from_proposal).expanduser())
        if not words:
            print("add 需要 --words \"词1,词2\" 或 --from-proposal 清单文件", file=sys.stderr)
            return 2
        res = q.add(platform, words, "manual")
        lines = [f"已入队 {len(res['added'])} 个：{', '.join(res['added']) or '无'}"]
        lines += [f"  跳过「{w}」：{why}" for w, why in res["skipped"].items()]
        _emit(args, res, "\n".join(lines))
    elif args.action == "list":
        items = q.items(None if args.all_platforms else platform)
        icon = {"pending": "待采", "done": "已采", "skipped": "跳过"}
        lines = []
        for i in items:
            tag = icon.get(i["status"], i["status"])
            if i["status"] == "pending" and i.get("attempts", 0) >= MAX_ATTEMPTS:
                tag = "卡住"
            extra = f"  失败{i['attempts']}次" if i.get("attempts") else ""
            lines.append(f"  [{tag}] {i['platform']}  {i['keyword']}{extra}")
        c = q.counts(platform)
        lines.append(f"\n平台 {platform}：待采 {c['pending']} / 已采 {c['done']} / 跳过 {c['skipped']} / 卡住 {c['stuck']}"
                     + (f"\n「卡住」= 连续失败 {MAX_ATTEMPTS} 次，不会再被取用；确认原因后 leadctl queue retry" if c["stuck"] else ""))
        _emit(args, {"items": items, "counts": c}, "\n".join(lines))
    elif args.action == "skip":
        if not words:
            print("skip 需要 --words", file=sys.stderr)
            return 2
        hit = q.skip(platform, words)
        _emit(args, {"skipped": hit}, f"已跳过 {len(hit)} 个：{', '.join(hit)}")
    elif args.action == "retry":
        hit = q.retry(platform, words or None)
        _emit(args, {"retry": hit}, f"已恢复为待采 {len(hit)} 个：{', '.join(hit) or '无'}")
    return 0


def _resolve_input(ws: Workspace, value: str) -> Path:
    """--input 可以是路径、批次 ID，或 latest。"""
    if value == "latest":
        batches = sorted([p for p in ws.raw.iterdir() if p.is_dir()], key=lambda p: p.name) if ws.raw.exists() else []
        if not batches:
            raise FileNotFoundError(f"{ws.raw} 下还没有采集批次")
        return batches[-1]
    p = Path(value).expanduser()
    if p.exists():
        return p
    if (ws.raw / value).exists():
        return ws.raw / value
    raise FileNotFoundError(f"找不到输入 {value}（也不是 {ws.raw} 下的批次 ID）")


def cmd_ingest(args: argparse.Namespace) -> int:
    ws, prof = _ctx(args)
    scorer = _scorer_or_die(prof)
    src = _resolve_input(ws, args.input)
    if args.text_col:  # 通用 CSV 模式
        records = load_generic_csv(src, text_col=args.text_col, id_col=args.id_col or "", keyword_col=args.keyword_col or "",
                                   url_col=args.url_col or "", nickname_col=args.nickname_col or "",
                                   platform=args.platform or "csv")
        batch_id = args.batch_id or f"csv-{datetime.now(prof.tz).strftime('%Y%m%d-%H%M%S')}"
    else:
        manifest = (src / "manifest.json") if src.is_dir() else None
        platform = args.platform
        if not platform and manifest and manifest.exists():
            platform = json.loads(manifest.read_text(encoding="utf-8")).get("platform")
        if not platform:
            print("无法判断平台，请加 --platform（xhs / dy / ks / bili / wb）", file=sys.stderr)
            return 2
        records = load_mediacrawler(src, load_platform_map(platform), prof.tz, platform)
        batch_id = args.batch_id or (src.name if src.is_dir() else f"{platform}-{datetime.now(prof.tz).strftime('%Y%m%d-%H%M%S')}")
    db = ws.db_path(prof.name)
    pool._backup(db, "ingest")
    con = pool.connect(db)
    try:
        counts = pool.ingest(con, prof, scorer, records, batch_id)
        public, internal, n = pool.export(con, ws, prof)
    finally:
        con.close()
    _emit(args, {"batch_id": batch_id, "records": len(records), **counts, "db": str(db), "export": str(public)},
          f"入库 {len(records)} 条（新增 {counts['new']}，更新 {counts['updated']}）\n"
          f"ready {counts['ready']} / 待复核 {counts['needs_review']} / 排除 {counts['excluded']}\n"
          f"数据库：{db}\n人工池 Excel（高相关在前并标红，含用户名，仅内部）：{pool.xlsx_path(ws, prof)}\n"
          f"人工池 CSV（脱敏，可外传）：{public}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """不入库的快速打分：任意 CSV 进，带分数的 CSV 出。"""
    ws, prof = _ctx(args)
    scorer = _scorer_or_die(prof)
    src = Path(args.input).expanduser()
    records = load_generic_csv(src, text_col=args.text_col, keyword_col=args.keyword_col or "")
    # 不指定 --out 时文件名带当天日期，避免第二天打分盖掉前一天的结果
    day = datetime.now(prof.tz).strftime("%Y-%m-%d")
    out = Path(args.out) if args.out else ws.exports / f"{src.stem}.scored.{day}.csv"
    # 表头和取值都用中文（与线索池导出一致）；英文字段名只在内部使用
    fields = [("intent_score", "意向分"), ("problem", "问题类型"), ("comment_text", "评论内容"), ("search_keyword", "搜索词"),
              ("status", "分级"), ("parent_likely", "像家长"), ("strength", "强度"), ("tags", "命中标签"),
              ("geo_hit", "地域命中"), ("target_region", "目标地区"), ("exclude_reason", "排除原因")]
    counts: dict[str, int] = {}
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in fields])
        for r in records:
            kw = r["search_keyword"] or args.keyword or ""
            s = scorer.score(r["text"], kw)
            label = pool.STATUS_LABEL.get(s["status"], s["status"])
            counts[label] = counts.get(label, 0) + 1
            row = {**s, "comment_text": r["text"], "search_keyword": kw}
            w.writerow([pool.STATUS_LABEL.get(row[k], row[k]) if k == "status"
                        else ("是" if row[k] else "否") if k in ("parent_likely", "geo_hit") else row[k] for k, _ in fields])
    _emit(args, {"out": str(out), "counts": counts}, f"已打分 {len(records)} 条 → {out}\n分布：{counts}")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    ws, prof = _ctx(args)
    db = ws.db_path(prof.name)
    if not db.exists() and args.action != "stats":
        print(f"还没有数据库 {db}，先 leadctl ingest", file=sys.stderr)
        return 2
    con = pool.connect(db)
    try:
        if args.action == "rescore":
            counts = pool.rescore(con, prof, _scorer_or_die(prof), db)
            public, _, n = pool.export(con, ws, prof)
            _emit(args, {**counts, "export": str(public)}, f"重打分 {counts['total']} 条，改判 {counts['changed']}，冻结 {counts['locked']}\n人工池 {n} 条 → {pool.xlsx_path(ws, prof)}（Excel，高相关标红）/ {public}（CSV）")
        elif args.action == "export":
            statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
            public, internal, n = pool.export(con, ws, prof, statuses)
            _emit(args, {"rows": n, "public": str(public), "internal": str(internal)},
                  f"导出 {n} 条（高相关在前并标红）\nExcel（含用户名，仅内部）：{pool.xlsx_path(ws, prof)}\n脱敏 CSV（可外传）：{public}\n含昵称 CSV（仅内部）：{internal}")
        elif args.action == "stats":
            st = pool.stats(con)
            human = "\n".join(f"{k}: {v}" for k, v in st.items())
            _emit(args, st, human)
        elif args.action == "mark":
            if not (args.platform_ and args.comment_id and args.reach):
                print("mark 需要 --platform-id / --comment-id / --reach", file=sys.stderr)
                return 2
            ok = pool.mark(con, prof, args.platform_, args.comment_id, args.reach, args.note or "")
            _emit(args, {"updated": ok}, "已标记" if ok else "没找到这条线索")
            return 0 if ok else 1
        elif args.action == "purge":
            res = pool.purge(con, prof, dry_run=not args.yes)
            _emit(args, res, f"{'将清理' if not args.yes else '已清理'}：原始评论 {res['raw_comments']}，排除线索 {res['excluded_leads']}"
                  + ("\n（预演，加 --yes 真正删除）" if not args.yes else ""))
    finally:
        con.close()
    return 0


# ---------------- 参数解析 ----------------
def _common(top: bool) -> argparse.ArgumentParser:
    """全局参数。顶层和每个子命令各挂一份，所以 `leadctl --json pool stats` 与 `leadctl pool stats --json` 都能用。

    坑点：argparse 里子命令的默认值会覆盖顶层已解析的值。所以子命令版本一律用
    SUPPRESS 作默认（没写就不设置该属性），真正的默认值只在顶层设一次。
    """
    sup = argparse.SUPPRESS
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--workdir", default=None if top else sup, help="工作区目录（默认 $LEADKIT_HOME 或 ~/.leadkit）")
    c.add_argument("--profile", default=DEFAULT_PROFILE if top else sup,
                   help=f"行业/地区配置（默认 {DEFAULT_PROFILE}，可用 $LEADKIT_PROFILE 改）")
    c.add_argument("-v", "--verbose", action="store_true", default=False if top else sup, help="显示 DEBUG 日志")
    c.add_argument("-q", "--quiet", action="store_true", default=False if top else sup, help="只显示警告和错误")
    c.add_argument("--json", action="store_true", default=False if top else sup, help="结果以 JSON 输出到 stdout")
    return c


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leadctl",
        parents=[_common(True)],
        description="公开评论获客意向工具：扩词 → 受限采集 → 打分 → 人工池。只做判断，不评论、不私信。",
        epilog="先跑 `leadctl doctor` 看环境；完整流程见 README.md。全局参数写在子命令前后都行。",
    )
    p.add_argument("--version", action="version", version=f"leadctl {__version__}")
    common = _common(False)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<命令>")

    sub.add_parser("profiles", parents=[common], help="列出可用 profile").set_defaults(fn=cmd_profiles)
    sub.add_parser("check", parents=[common], help="跑 profile 自检用例").set_defaults(fn=cmd_check)

    s = sub.add_parser("setup", parents=[common], help="安装上游 MediaCrawler 并打防封号补丁")
    s.add_argument("--ref", default=PINNED_REF, help=f"上游版本（默认 {PINNED_REF}，补丁针对它验证过）")
    s.add_argument("--mc-dir", help="使用已有的 MediaCrawler 目录，只打补丁不克隆")
    s.add_argument("--skip-sync", action="store_true", help="跳过 uv sync")
    s.add_argument("--with-raw-identity", action="store_true", help="可选补丁：采集明文昵称和用户 ID（隐私风险，默认关）")
    s.add_argument("--with-ip-province", action="store_true",
                   help="可选补丁 0005：评论里额外存评论者 IP 的省级属地（如「浙江」），给地域过滤排除外省用；只到省，默认关")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("doctor", parents=[common], help="环境体检")
    s.add_argument("--mc-dir", help="MediaCrawler 目录（默认工作区内）")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("keywords", parents=[common], help="品类+地区 → 搜索词清单")
    s.add_argument("--category", required=True, help="品类，如 感统训练")
    s.add_argument("--place", "--ip", required=True, help="地区，如 台州椒江")
    s.add_argument("--out", help="输出文件（默认工作区 keywords/）")
    s.add_argument("--add", help="自己补充的词（逗号分隔），会排在模板词前面；agent 联想出的长尾词放这里")
    s.add_argument("--enqueue", action="store_true", help="把可采的词存入待采队列（附近区备选词不入队）")
    s.add_argument("--platform", help="入队的平台（默认取 profile）")
    s.set_defaults(fn=cmd_keywords)

    s = sub.add_parser("queue", parents=[common], help="待采关键词队列：add / list / skip / retry")
    s.add_argument("action", choices=["add", "list", "skip", "retry"])
    s.add_argument("--platform", help="平台（默认取 profile）")
    s.add_argument("--words", help="逗号分隔的词")
    s.add_argument("--from-proposal", help="add：读取 leadctl keywords 写出的清单文件")
    s.add_argument("--all-platforms", action="store_true", help="list：显示所有平台")
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("collect", parents=[common], help="受限采集（默认 dry-run，加 --yes 才执行）")
    s.add_argument("--platform", help="xhs / dy / ks / bili / wb（默认取 profile）")
    s.add_argument("--keywords", help=f"逗号分隔，最多 {HARD_CEILING['max_keywords']} 个（与 --next 二选一）")
    s.add_argument("--next", action="store_true", help="从待采队列自动取下一批词（按今日剩余配额决定取几个）")
    s.add_argument("--max-words", type=int, help="配合 --next：本次最多取几个词（默认按额度取满；新平台首次实机建议 3）")
    s.add_argument("--notes", type=int, help="每词笔记数（默认取限额上限）")
    s.add_argument("--comments", type=int, help="单帖评论数（默认取限额上限）")
    s.add_argument("--sleep", type=int, help="请求间隔秒数（默认 3，不能更低）")
    s.add_argument("--backend", default="mediacrawler", choices=sorted(BACKENDS))
    s.add_argument("--mc-dir", help="MediaCrawler 目录（默认工作区内）")
    s.add_argument("--yes", action="store_true", help="确认执行；不加只做预检")
    s.add_argument("--ingest", action="store_true", help="采集成功后自动入库打分")
    s.add_argument("--allow-exceed-limits", action="store_true", help="放行数量类超限（需书面批准，会留痕）")
    s.add_argument("--allow-unverified-platform", action="store_true", help="允许采集未实测/无补丁的平台")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("ingest", parents=[common], help="采集产物 → 打分 → 入库 → 导出")
    s.add_argument("--input", required=True, help="批次目录 / 批次 ID / latest / CSV 路径")
    s.add_argument("--platform", help="平台（批次里有 manifest 时可省略）")
    s.add_argument("--batch-id")
    g = s.add_argument_group("通用 CSV 模式（没用 MediaCrawler 时）：指定 --text-col 即启用")
    g.add_argument("--text-col", help="评论文本所在列")
    g.add_argument("--id-col", help="评论 ID 列（缺省用行号）")
    g.add_argument("--keyword-col", help="搜索词列")
    g.add_argument("--url-col", help="帖子链接列")
    g.add_argument("--nickname-col", help="昵称列")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("score", parents=[common], help="不入库，给任意 CSV 打分")
    s.add_argument("--input", required=True)
    s.add_argument("--text-col", required=True, help="评论文本所在列")
    s.add_argument("--keyword-col", help="搜索词列")
    s.add_argument("--keyword", help="整份文件统一的搜索词（影响特殊需求判断）")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("pool", parents=[common], help="线索池：rescore / export / stats / mark / purge")
    s.add_argument("action", choices=["rescore", "export", "stats", "mark", "purge"])
    s.add_argument("--status", default="ready,needs_review", help="export 的状态过滤")
    s.add_argument("--platform-id", dest="platform_", help="mark：平台")
    s.add_argument("--comment-id", help="mark：评论 ID")
    s.add_argument("--reach", help="mark：新触达状态 " + "/".join(pool.REACH_STATUSES))
    s.add_argument("--note", help="mark：备注")
    s.add_argument("--yes", action="store_true", help="purge：真正删除（默认只预演）")
    s.set_defaults(fn=cmd_pool)
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认编码不是 UTF-8，中文输出会乱码或报错
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ProfileError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"文件不存在：{exc}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception:  # 兜底：把堆栈写进日志文件，终端只给一句话，避免吓到使用者
        LOG.exception("未预期的错误")
        print("发生未预期错误，详情见工作区 logs/leadkit.log（加 -v 可在终端看到堆栈）", file=sys.stderr)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
