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
from .guard import CollectPlan, effective_limits
from .logger import get_logger, setup_logging
from .normalize import load_generic_csv, load_mediacrawler
from .paths import Workspace
from .profile import Profile, ProfileError, list_profiles, load_platform_map, load_profile
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
    return setup(ws, args.ref, _mc_dir(args), args.skip_sync, args.with_raw_identity)


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
    lines = kw.format_lines(phrases, nearby)
    path = kw.write_proposal(category, place, lines, ws.root / "keywords", args.out or "")
    _emit(args, {"keywords": phrases, "nearby": nearby, "file": str(path)},
          "\n".join(lines) + f"\n\n已写入 {path}\n从中人工挑选至多 3 条，用于 leadctl collect --keywords")
    return 0


def _parse_keywords(raw: str) -> list[str]:
    """支持逗号分隔；顺手去掉从清单里整行复制过来的 `# 注释`。"""
    out = []
    for part in raw.replace("，", ",").split(","):
        word = part.split("#")[0].strip()
        if word:
            out.append(word)
    return out


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
    keywords = _parse_keywords(args.keywords)
    notes = args.notes if args.notes is not None else limits["max_notes_per_keyword"]
    if args.comments is not None:
        comments = args.comments
    else:
        # 各项上限同时取满会超日配额（3词×5篇×5评=75 > 50），所以默认值按「今日剩余评论配额」反推，
        # 而不是直接取单帖上限。显式传 --comments 则以显式值为准（超了会在预检被拒）。
        remaining = limits["daily_comments"] - collector.ledger.usage_today()["comments"]
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
    if issues:
        _emit(args, {"ok": False, "refused": issues},
              "\n拒绝执行，原因：\n" + "\n".join(f"  ✘ {i}" for i in issues))
        return 2
    if not args.yes:
        _emit(args, {"ok": True, "dry_run": True, "batch_id": batch_id},
              "\n预检通过（dry-run，未采集）。确认无误后加 --yes 真正执行。")
        return 0

    res = collector.run(plan, pmap, batch_id, batch_dir)
    payload = {"ok": res.ok, "batch_id": res.batch_id, "batch_dir": str(res.batch_dir), "status": res.status,
               "notes": res.notes, "comments": res.comments, "reason": res.reason}
    _emit(args, payload, f"采集 {res.status}：笔记 {res.notes} 篇 / 评论 {res.comments} 条\n产物：{res.batch_dir}"
          + (f"\n原因：{res.reason}" if res.reason else ""))
    if res.ok and args.ingest:
        ns = argparse.Namespace(**{**vars(args), "input": str(batch_dir), "text_col": None, "batch_id": batch_id})
        return cmd_ingest(ns)
    return 0 if res.ok else 1


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
          f"数据库：{db}\n人工池（脱敏，可外传）：{public}\n人工池（含昵称，仅内部）：{internal}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """不入库的快速打分：任意 CSV 进，带分数的 CSV 出。"""
    ws, prof = _ctx(args)
    scorer = _scorer_or_die(prof)
    src = Path(args.input).expanduser()
    records = load_generic_csv(src, text_col=args.text_col, keyword_col=args.keyword_col or "")
    out = Path(args.out) if args.out else ws.exports / (src.stem + ".scored.csv")  # 默认进工作区，skill 目录保持干净
    fields = ["comment_text", "search_keyword", "intent_score", "status", "parent_likely", "problem", "strength", "tags",
              "geo_hit", "target_region", "exclude_reason"]
    counts: dict[str, int] = {}
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            s = scorer.score(r["text"], r["search_keyword"] or args.keyword or "")
            counts[s["status"]] = counts.get(s["status"], 0) + 1
            w.writerow({"comment_text": r["text"], "search_keyword": r["search_keyword"] or args.keyword or "",
                        **{k: s[k] for k in fields[2:]}})
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
            _emit(args, {**counts, "export": str(public)}, f"重打分 {counts['total']} 条，改判 {counts['changed']}，冻结 {counts['locked']}\n人工池 {n} 条 → {public}")
        elif args.action == "export":
            statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
            public, internal, n = pool.export(con, ws, prof, statuses)
            _emit(args, {"rows": n, "public": str(public), "internal": str(internal)},
                  f"导出 {n} 条\n脱敏（可外传）：{public}\n含昵称（仅内部）：{internal}")
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
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("doctor", parents=[common], help="环境体检")
    s.add_argument("--mc-dir", help="MediaCrawler 目录（默认工作区内）")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("keywords", parents=[common], help="品类+地区 → 搜索词清单")
    s.add_argument("--category", required=True, help="品类，如 感统训练")
    s.add_argument("--place", "--ip", required=True, help="地区，如 台州椒江")
    s.add_argument("--out", help="输出文件（默认工作区 keywords/）")
    s.set_defaults(fn=cmd_keywords)

    s = sub.add_parser("collect", parents=[common], help="受限采集（默认 dry-run，加 --yes 才执行）")
    s.add_argument("--platform", help="xhs / dy / ks / bili / wb（默认取 profile）")
    s.add_argument("--keywords", required=True, help="逗号分隔，最多 3 个")
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
