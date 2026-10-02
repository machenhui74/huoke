"""护栏：把「防封号红线」变成代码里的强制检查，而不是只写在文档里。

别的 agent 不一定会读文档，但一定会撞上这里。三层防线：
  1. 开跑前：参数硬上限、登录方式、补丁是否已打、日配额/冷却（validate_plan / check_patch / Ledger.check）
  2. 开跑中：盯日志，实际拉取数超限或出现风控信号立刻终止（RunMonitor）
  3. 开跑后：台账记录实际用量，影响当日后续配额

硬上限写在代码里（HARD）。profile 的 [limits] 只能收紧，要放宽必须显式
--allow-exceed-limits，并会写 WARNING 和台账，事后可查。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from .logger import get_logger

LOG = get_logger("guard")

# 硬上限（对应 references/safety-redlines.md §1）。
# CEILING 类：值越大越危险，只能往小调；FLOOR 类：值越小越危险，只能往大调。
#
# 2026-10-02 按使用者指示放宽（见红线文档变更记录）：
#   词数 3→10、单帖评论 5→10、日笔记 30→50、日评论 50→500。
#   即一次完整采集 = 10 词 × 5 篇 = 50 篇笔记 × 10 条 = 500 条评论。
#   旧档位（3 / 5 / 5 / 30 / 50）是回退基线：一旦出现风控信号就改回去，并按红线 §9 记录。
#   并发、间隔下限、冷却、代理/二级评论/媒体/登录方式等「零例外」项没有动。
HARD_CEILING = {
    "max_keywords": 10,
    "max_notes_per_keyword": 5,
    "max_comments_per_note": 10,
    "max_concurrency": 1,
    "sessions_per_day": 2,
    "daily_note_details": 50,
    "daily_comments": 500,
}
HARD_FLOOR = {
    "min_sleep_sec": 3,
    "cooldown_minutes": 30,
    "block_cooldown_hours": 2,
}
# 允许 profile 覆盖到上游 config 的键白名单。限速、代理、二级评论等不在其中，
# 防止有人（或 agent）通过 profile 偷偷绕过红线。
OVERRIDE_WHITELIST = {
    "CDP_CONNECT_EXISTING", "AUTO_CLOSE_BROWSER", "SAVE_LOGIN_STATE",
    "ENABLE_CDP_MODE", "CUSTOM_BROWSER_PATH", "XHS_INTERNATIONAL",
}
# 只允许扫码登录：phone 登录在抖音会触发上游的「自动过滑块」逻辑，属于绕过风控，红线明令禁止。
ALLOWED_LOGIN = {"qrcode"}

# 风控信号。只在 WARNING 及以上级别的日志行里匹配，避免评论正文里恰好含「验证码」而误停。
BLOCK_PATTERNS = [
    r"CAPTCHA appeared", r"captcha", r"Verifytype", r"验证码中间页", r"滑块",
    r"IPBlockError", r"status_code[=: ]+(?:461|471|429)", r"429 Too Many",
    r"请求过于频繁", r"访问频繁", r"操作频繁", r"账号异常", r"rate.?limit",
    r"login (?:state )?(?:expired|failed)", r"登录(?:失效|过期|失败)",
]
_BLOCK_RE = re.compile("|".join(BLOCK_PATTERNS), re.I)
_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")


class GuardError(Exception):
    """护栏拒绝执行。message 里写清原因和怎么办。"""


# ============================================================
# 1. 开跑前校验
# ============================================================
@dataclass
class CollectPlan:
    """一次采集的完整参数。所有数字最终都会过 validate_plan。"""

    platform: str
    keywords: list[str]
    notes_per_keyword: int
    comments_per_note: int
    concurrency: int = 1
    sleep_sec: int = 3
    login: str = "qrcode"
    sub_comments: bool = False
    proxy: bool = False
    media: bool = False
    headless: bool = False

    @property
    def est_notes(self) -> int:
        return len(self.keywords) * self.notes_per_keyword

    @property
    def est_comments(self) -> int:
        return self.est_notes * self.comments_per_note


def effective_limits(profile_limits: dict[str, Any], allow_exceed: bool) -> dict[str, int]:
    """合并 profile 与硬上限：profile 只能收紧，除非显式放行。"""
    eff: dict[str, int] = {}
    for key, ceiling in HARD_CEILING.items():
        val = int(profile_limits.get(key, ceiling))
        if val > ceiling and not allow_exceed:
            LOG.warning("profile 的 %s=%s 超过硬上限 %s，已压回", key, val, ceiling)
            val = ceiling
        eff[key] = val
    for key, floor in HARD_FLOOR.items():
        val = int(profile_limits.get(key, floor))
        if val < floor and not allow_exceed:
            LOG.warning("profile 的 %s=%s 低于硬下限 %s，已抬回", key, val, floor)
            val = floor
        eff[key] = val
    if allow_exceed:
        LOG.warning("已启用 --allow-exceed-limits：硬上限不再强制，请确认有书面放宽批准")
    return eff


def validate_plan(plan: CollectPlan, limits: dict[str, int], allow_exceed: bool = False) -> list[str]:
    """逐项检查参数，返回违规说明列表（空 = 通过）。"""
    bad: list[str] = []

    def over(name: str, value: int, key: str) -> None:
        if value > limits[key]:
            bad.append(f"{name}={value} 超过上限 {limits[key]}（{key}）")

    if not plan.keywords:
        bad.append("没有关键词")
    over("关键词数", len(plan.keywords), "max_keywords")
    over("每词笔记数", plan.notes_per_keyword, "max_notes_per_keyword")
    over("单帖评论数", plan.comments_per_note, "max_comments_per_note")
    over("并发", plan.concurrency, "max_concurrency")
    if plan.sleep_sec < limits["min_sleep_sec"]:
        bad.append(f"请求间隔={plan.sleep_sec}s 低于下限 {limits['min_sleep_sec']}s")
    if plan.notes_per_keyword < 1 or plan.comments_per_note < 1:
        bad.append("笔记数和评论数必须 >= 1")
    if plan.login not in ALLOWED_LOGIN:
        bad.append(f"登录方式 {plan.login!r} 不允许；只能 {sorted(ALLOWED_LOGIN)}（其它方式会触发自动过滑块）")
    # 下面三项没有「放宽」的口子：红线零例外
    if plan.sub_comments:
        bad.append("二级评论必须关闭")
    if plan.proxy:
        bad.append("禁止使用代理（不得用代理绕过风控）")
    if plan.media:
        bad.append("媒体下载必须关闭")
    # 去重后的词才算数，避免同一个词重复刷
    if len(set(plan.keywords)) != len(plan.keywords):
        bad.append("关键词有重复")
    if bad and allow_exceed:
        # 只有「数量类」违规可以被放行；零例外项仍然拒绝
        hard = [b for b in bad if any(t in b for t in ("二级评论", "代理", "媒体", "登录方式", "重复", "没有关键词", ">= 1"))]
        soft = [b for b in bad if b not in hard]
        for s in soft:
            LOG.warning("放行违规（--allow-exceed-limits）: %s", s)
        bad = hard
    return bad


def validate_overrides(overrides: dict[str, Any]) -> list[str]:
    """profile.collect.overrides 只能用白名单里的键。"""
    return [f"overrides 不允许覆盖 {k}（只允许 {sorted(OVERRIDE_WHITELIST)}）" for k in overrides if k not in OVERRIDE_WHITELIST]


def check_patch(mc_dir: Path, platform_map: dict[str, Any], allow_unverified: bool = False) -> list[str]:
    """确认上游已打「搜索页截断」补丁。

    背景：上游会把「每词笔记数」强行抬到整页大小（xhs=20、dy=10），配置 5 实际拉 20，
    这是 2026-09-29 的真实事故。补丁没打上就开采，等于红线形同虚设。
    """
    bad: list[str] = []
    patch = platform_map.get("patch")
    label = platform_map["platform"]["label"]
    if not mc_dir.exists():
        return [f"找不到 MediaCrawler：{mc_dir}。先运行 leadctl setup"]
    if patch is None:
        if not platform_map["platform"].get("verified", False) and not allow_unverified:
            bad.append(f"{label} 没有搜索截断补丁、也未实测；如确需采集，加 --allow-unverified-platform（运行时监控仍会生效）")
        return bad
    for rel in (patch["file"], patch["core"]):
        f = mc_dir / rel
        if not f.exists():
            bad.append(f"缺少文件 {rel}，MediaCrawler 目录不完整")
        elif patch["marker"] not in f.read_text(encoding="utf-8", errors="ignore"):
            bad.append(f"{label} 搜索截断补丁未生效（{rel} 里没有 {patch['marker']}）。运行 leadctl setup 重新打补丁")
    return bad


# ============================================================
# 2. 台账：日配额与冷却
# ============================================================
class Ledger:
    """采集台账，JSONL 追加写。每次采集写 start / end 两条，遇风控另写 block。"""

    def __init__(self, state_dir: Path, tz: tzinfo):
        self.path = state_dir / "runs.jsonl"
        self.tz = tz
        state_dir.mkdir(parents=True, exist_ok=True)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def append(self, **event: Any) -> None:
        event["ts"] = self.now().isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        LOG.debug("台账 +1: %s", event)

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                LOG.warning("台账有损坏行，已跳过: %s", line[:60])
        return out

    def _today(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        today = self.now().date().isoformat()
        return [e for e in events if e.get("ts", "")[:10] == today]

    def usage_today(self, platform: str | None = None) -> dict[str, int]:
        """今日已用：每个批次取 end 的实际值，没有 end（崩溃/中断）按 start 的估算值算，宁多勿少。

        platform 给定时只统计该平台：平台风控按账号算，小红书和抖音是两个独立账号，额度分开计
        （2026-10-02 用户决定）。老台账里没有 platform 字段的批次无法归属，保守地算进每个平台。
        不传 platform 则是全部平台合计（doctor 展示用）。冷却和封控锁不受影响，仍是全局的。
        """
        by_batch: dict[str, dict[str, Any]] = {}
        for e in self._today(self.events()):
            b = e.get("batch")
            if e["event"] == "start":
                by_batch[b] = {"notes": e.get("est_notes", 0), "comments": e.get("est_comments", 0),
                               "platform": e.get("platform") or ""}
            elif e["event"] == "end" and b in by_batch:
                by_batch[b] = {**by_batch[b],   # 保留 platform，只用实际值覆盖估算值
                               "notes": e.get("actual_notes", by_batch[b]["notes"]),
                               "comments": e.get("actual_comments", by_batch[b]["comments"])}
        mine = [v for v in by_batch.values() if platform is None or v["platform"] in ("", platform)]
        return {
            "sessions": len(mine),
            "notes": sum(v["notes"] for v in mine),
            "comments": sum(v["comments"] for v in mine),
        }

    def check(self, plan: CollectPlan, limits: dict[str, int]) -> list[str]:
        """日配额、冷却、封控锁。返回违规说明列表。"""
        bad: list[str] = []
        now = self.now()
        events = self.events()
        today = self._today(events)
        use = self.usage_today(plan.platform)   # 按本次要采的平台计额度
        if use["sessions"] >= limits["sessions_per_day"]:
            bad.append(f"今日{plan.platform}已采 {use['sessions']} 次，达到上限 {limits['sessions_per_day']}")
        if use["notes"] + plan.est_notes > limits["daily_note_details"]:
            bad.append(f"今日{plan.platform}笔记详情已用 {use['notes']}，再采 {plan.est_notes} 会超过 {limits['daily_note_details']}")
        if use["comments"] + plan.est_comments > limits["daily_comments"]:
            bad.append(f"今日{plan.platform}评论已用 {use['comments']}，再采 {plan.est_comments} 会超过 {limits['daily_comments']}")

        # 两次采集的冷却：以上一次 end（或 start）为准
        finished = [e for e in events if e["event"] in ("end", "block")]
        if finished:
            last = datetime.fromisoformat(finished[-1]["ts"])
            wait = timedelta(minutes=limits["cooldown_minutes"]) - (now - last)
            if wait.total_seconds() > 0:
                bad.append(f"距上次采集不足 {limits['cooldown_minutes']} 分钟，还需等 {int(wait.total_seconds() // 60) + 1} 分钟")

        blocks = [e for e in events if e["event"] == "block"]
        if blocks:
            last_block = datetime.fromisoformat(blocks[-1]["ts"])
            wait = timedelta(hours=limits["block_cooldown_hours"]) - (now - last_block)
            if wait.total_seconds() > 0:
                bad.append(f"上次触发风控（{blocks[-1].get('reason', '?')}），冷却 {limits['block_cooldown_hours']} 小时，还需 {int(wait.total_seconds() // 60) + 1} 分钟")
        if sum(1 for e in today if e["event"] == "block") >= 2:
            bad.append("今日已两次触发风控，当天封存，明天再评估")
        return bad


# ============================================================
# 3. 运行时监控
# ============================================================
@dataclass
class RunMonitor:
    """逐行消费爬虫日志，决定要不要立刻终止。

    只依赖日志文本，不依赖爬虫内部实现；正则由平台映射表的 [monitor] 提供。
    """

    plan: CollectPlan
    keyword_re: re.Pattern | None = None
    detail_re: re.Pattern | None = None
    comment_re: re.Pattern | None = None
    notes_total: int = 0
    comments_total: int = 0
    notes_this_keyword: int = 0
    current_keyword: str = ""
    abort_reason: str = ""
    abort_kind: str = ""  # "block" | "overfetch"
    _seen_lines: int = field(default=0, repr=False)

    @classmethod
    def from_platform(cls, plan: CollectPlan, platform_map: dict[str, Any]) -> "RunMonitor":
        m = platform_map.get("monitor", {})
        comp = lambda k: re.compile(m[k]) if m.get(k) else None
        return cls(plan, comp("keyword_re"), comp("detail_re"), comp("comment_re"))

    def feed(self, line: str) -> str | None:
        """处理一行日志；返回终止原因，None 表示继续。"""
        self._seen_lines += 1
        if self.abort_reason:
            return self.abort_reason
        if self.keyword_re and (m := self.keyword_re.search(line)):
            self.current_keyword, self.notes_this_keyword = m.group(1).strip(), 0
            LOG.info("监控：开始关键词「%s」", self.current_keyword)
        if self.detail_re and self.detail_re.search(line):
            self.notes_total += 1
            self.notes_this_keyword += 1
            if self.notes_this_keyword > self.plan.notes_per_keyword:
                return self._abort("overfetch", f"关键词「{self.current_keyword}」已拉 {self.notes_this_keyword} 篇，超过上限 {self.plan.notes_per_keyword}")
            if self.notes_total > self.plan.est_notes:
                return self._abort("overfetch", f"累计拉取 {self.notes_total} 篇，超过计划 {self.plan.est_notes}")
        if self.comment_re and self.comment_re.search(line):
            self.comments_total += 1
            # 评论按计划的 2 倍给缓冲：不同平台分页行为不一，只拦明显失控
            if self.comments_total > self.plan.est_comments * 2:
                return self._abort("overfetch", f"累计评论 {self.comments_total} 条，远超计划 {self.plan.est_comments}")
        # 风控信号：只看 WARNING+ 且不是存储回显行（回显里会带用户评论原文）
        lv = _LEVEL_RE.search(line)
        level = lv.group(1) if lv else ""
        if level in ("WARNING", "ERROR", "CRITICAL") and "[store." not in line:
            if hit := _BLOCK_RE.search(line):
                return self._abort("block", f"风控信号「{hit.group(0)}」: {line.strip()[:120]}")
        return None

    def _abort(self, kind: str, reason: str) -> str:
        self.abort_kind, self.abort_reason = kind, reason
        LOG.critical("监控触发终止 [%s] %s", kind, reason)
        return reason
