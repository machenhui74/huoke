"""导出硬门禁：评论必须同时满足三项条件才能进入外发交付表。

三项硬门禁：
  1. 地域本人自述：评论者本人在评论正文中明确自述在目标城市（如台州）
     - 仅出现地名不够，必须有「我在/坐标/人在」等自述句式
     - 搜词、帖子标题、作者位置、昵称、浙江IP 都不能证明评论者本地
     - 外省/境外 IP 与本地自述冲突时不通过
     - 「台州有吗」只是在问，不能当作本人在台州
     - 注意台州和泰州的区分
  2. 评论时效：commented_at 在最近 N 天内（Asia/Shanghai 时区）
     - 未来时间、解析失败、未知时间一律不通过
  3. 服务需求：明确主动询问当地课程/机构/报名/试听等服务需求
     - 不推断儿童诊断、健康；不挖联系方式

不满足门禁的线索仍保留在内部库和复核表，只是不进入外发交付表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .logger import get_logger
from .profile import Profile

LOG = get_logger("export_gate")


@dataclass
class GateResult:
    """门禁判断结果。"""
    passed: bool                        # 三项全部通过
    geo_passed: bool = False            # 地域自述
    time_passed: bool = False           # 评论时效
    need_passed: bool = False           # 服务需求
    reason: str = ""                    # 不通过的原因
    evidence: str = ""                  # 通过时的证据


# ========== 地域本人自述门禁 ==========
# 评论者本人自述在某地的句式；{c} 会被目标城市词替换
# 注意：只收明确的第一人称自述，不收「台州的」「台州朋友」等可能在谈论他人的说法
SELF_LOCAL_TEMPLATES = [
    r"(?:我|我们|咱|本人|人|坐标)\s*(?:在|住|住在|定居在?|常住)\s*{c}",
    r"(?:我家|我们家)\s*(?:在|住|住在)\s*{c}",
    r"(?:我|我们|咱|本人)\s*(?:是|就是)\s*{c}(?:的|人|本地)",
    r"(?:我|我们|咱|本人)\s*{c}(?:的|人)",
    r"在\s*{c}\s*(?:住|生活|工作|上班|带娃|带孩子)",
    r"坐标\s*{c}",
    r"{c}\s*(?:这边|这里|本地|当地)\s*(?:有吗|有没有|哪里有|哪家好)",
]

# 评论者本人自述在外地的句式（用于冲突检测）
SELF_OUT_TEMPLATES = [
    r"(?:我|我们|咱|本人|人|坐标)\s*(?:在|住|住在|定居在?|常住)\s*{c}",
    r"(?:我|我们|咱|本人)\s*(?:是|就是)\s*{c}(?:的|人|本地)",
]

# 仅出现地名，不是本人自述（这些句式不能当作确认）
NOT_SELF_LOCAL = [
    r"{c}\s*有吗",                  # "台州有吗" 只是在问
    r"有\s*{c}\s*(?:的|吗)",        # "有台州的吗"
    r"{c}\s*(?:怎么样|好不好|靠谱吗)",  # 在问地方的情况
]


class GeoGate:
    """地域本人自述门禁：评论者必须明确自述在目标城市。"""

    def __init__(self, cfg: dict[str, Any]):
        self.enabled = bool(cfg.get("enabled", True))
        # 目标城市词（含区县），命中其中之一即可
        self.local_words = list(cfg.get("local_words", []))
        # 外地城市词，用于检测冲突
        self.out_words = list(cfg.get("out_words", []))
        # 需要区分的易混淆词（如台州/泰州）
        self.confusable = dict(cfg.get("confusable", {}))
        # IP 冲突词（外省/境外），出现时有本地自述也不通过
        self.ip_conflict_provinces = list(cfg.get("ip_conflict_provinces", []))

        # 编译正则
        local_pat = "|".join(re.escape(w) for w in self.local_words) if self.local_words else "(?!)"
        out_pat = "|".join(re.escape(w) for w in self.out_words) if self.out_words else "(?!)"

        self.self_local_re = [
            re.compile(t.replace("{c}", f"({local_pat})"))
            for t in SELF_LOCAL_TEMPLATES
        ] if self.local_words else []

        self.self_out_re = [
            re.compile(t.replace("{c}", f"({out_pat})"))
            for t in SELF_OUT_TEMPLATES
        ] if self.out_words else []

        self.not_self_local_re = [
            re.compile(t.replace("{c}", f"({local_pat})"))
            for t in NOT_SELF_LOCAL
        ] if self.local_words else []

        LOG.debug("GeoGate: local_words=%d, out_words=%d", len(self.local_words), len(self.out_words))

    def check(self, text: str, ctx: dict[str, Any] | None = None) -> tuple[bool, str, str]:
        """检查地域自述。返回 (通过, 原因, 证据)。"""
        if not self.enabled:
            return True, "", "门禁未启用"

        ctx = ctx or {}
        raw = text or ""

        # 检查是否有混淆词（如泰州被当成台州）
        for wrong, right in self.confusable.items():
            if wrong in raw and right not in raw:
                return False, f"地名混淆：含「{wrong}」但无「{right}」", ""

        # 检查是否有本人自述在外地（冲突）
        out_match = None
        for r in self.self_out_re:
            m = r.search(raw)
            if m:
                out_match = m.group(1)
                break

        # 检查是否只是提问（不是本人自述）
        for r in self.not_self_local_re:
            if r.search(raw):
                # 如果同时有本人自述句式，以自述为准；否则不通过
                has_self = any(r.search(raw) for r in self.self_local_re)
                if not has_self:
                    return False, "仅出现地名（如「台州有吗」），非本人自述", ""

        # 检查本人自述在本地
        local_match = None
        for r in self.self_local_re:
            m = r.search(raw)
            if m:
                local_match = m.group(1)
                break

        if not local_match:
            return False, "评论正文无本人自述在目标城市", ""

        # 检查是否有 IP 冲突
        ip = ctx.get("ip_province", "") or ""
        ip_clean = re.sub(r"^\s*ip\s*属地\s*[:：]?", "", ip, flags=re.I).strip()
        ip_clean = re.sub(r"[省市]$", "", ip_clean)
        if ip_clean and ip_clean in self.ip_conflict_provinces:
            return False, f"IP 属地「{ip_clean}」与自述「{local_match}」冲突", ""

        # 检查是否同时自述在外地（冲突）
        if out_match:
            return False, f"同时自述在外地「{out_match}」，与本地自述冲突", ""

        return True, "", f"本人自述：{local_match}"


# ========== 评论时效门禁 ==========
# 时间戳格式示例：2026-10-15 14:30、2026-10-15 14:30:00、1698912345（秒级）、1698912345000（毫秒级）
TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
]


class TimeGate:
    """评论时效门禁：commented_at 必须在最近 N 天内。"""

    def __init__(self, cfg: dict[str, Any], tz):
        self.enabled = bool(cfg.get("enabled", True))
        self.max_days = int(cfg.get("max_days", 30))
        self.tz = tz

    def check(self, commented_at: str, now: datetime | None = None) -> tuple[bool, str, str]:
        """检查评论时效。返回 (通过, 原因, 证据)。"""
        if not self.enabled:
            return True, "", "门禁未启用"

        now = now or datetime.now(self.tz)
        raw = (commented_at or "").strip()

        if not raw:
            return False, "评论时间为空/未知", ""

        # 尝试解析时间
        dt = None

        # 先尝试时间戳
        if raw.isdigit():
            ts = int(raw)
            if ts > 10_000_000_000:  # 毫秒级
                ts = ts / 1000
            try:
                dt = datetime.fromtimestamp(ts, self.tz)
            except (ValueError, OSError):
                pass

        # 再尝试各种文本格式
        if dt is None:
            for fmt in TIME_FORMATS:
                try:
                    dt = datetime.strptime(raw[:len(raw)], fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=self.tz)
                    break
                except ValueError:
                    continue

        if dt is None:
            return False, f"评论时间「{raw[:20]}」解析失败", ""

        # 检查是否未来时间
        if dt > now + timedelta(hours=1):  # 允许 1 小时时区误差
            return False, f"评论时间「{dt.strftime('%Y-%m-%d')}」是未来时间", ""

        # 检查是否在时效内
        cutoff = now - timedelta(days=self.max_days)
        if dt < cutoff:
            return False, f"评论时间「{dt.strftime('%Y-%m-%d')}」超过 {self.max_days} 天", ""

        return True, "", f"评论时间 {dt.strftime('%Y-%m-%d')} 在 {self.max_days} 天内"


# ========== 服务需求门禁 ==========
# 明确的服务需求询问词/句式
SERVICE_NEED_WORDS = [
    # 报名相关
    "怎么报名", "能报名吗", "报名方式", "如何报名", "想报名", "准备报名",
    "在哪报名", "哪里报名", "报名入口",
    # 试听/体验
    "能试听", "可以试听", "怎么试听", "试听课", "体验课",
    "怎么预约", "如何预约", "预约方式",
    # 收费/价格
    "怎么收费", "收费多少", "多少钱", "什么价格", "什么价位",
    "学费", "费用", "价格",
    # 课程相关
    "有什么课", "上什么课", "课程安排", "上课时间",
    "一周几节", "每周几次", "上多久",
    # 机构相关
    "哪家好", "有推荐", "求推荐", "推荐一下", "靠谱吗",
    "在哪里", "地址", "怎么走", "在哪儿",
    # 名额/活动
    "有名额", "还有位置", "有活动", "有优惠",
    # 直接询问
    "想了解", "想咨询", "咨询一下", "问一下",
]

# 不是服务需求的排除词（这些词虽然像在问，但不是在找服务）
NOT_SERVICE_NEED = [
    "投诉", "避雷", "曝光", "警告", "报警",
    "诊断", "医院", "治疗", "康复",
    "电话多少", "微信多少", "联系方式",  # 挖联系方式
]


class NeedGate:
    """服务需求门禁：必须明确主动询问当地服务需求。"""

    def __init__(self, cfg: dict[str, Any]):
        self.enabled = bool(cfg.get("enabled", True))
        # 服务需求词（可从 profile 覆盖）
        self.need_words = list(cfg.get("need_words", SERVICE_NEED_WORDS))
        # 排除词
        self.exclude_words = list(cfg.get("exclude_words", NOT_SERVICE_NEED))

    def check(self, text: str, problem: str = "") -> tuple[bool, str, str]:
        """检查服务需求。返回 (通过, 原因, 证据)。"""
        if not self.enabled:
            return True, "", "门禁未启用"

        raw = text or ""

        # 检查排除词
        for w in self.exclude_words:
            if w in raw:
                return False, f"含排除词「{w}」，非正常服务需求", ""

        # 检查服务需求词
        for w in self.need_words:
            if w in raw:
                return True, "", f"含服务需求词：{w}"

        # 如果打分时已经归类为明确咨询问题，也算通过
        valid_problems = {"求推荐", "问价", "报名", "问联系方式", "问课程详情", "求位置", "适龄咨询"}
        if problem and problem in valid_problems:
            return True, "", f"问题类型「{problem}」属于服务需求"

        return False, "评论正文无明确服务需求询问", ""


# ========== 组合门禁 ==========
class ExportGate:
    """导出硬门禁：三项全部通过才能进入外发交付表。"""

    def __init__(self, profile: Profile):
        cfg = profile.section("export_gate")
        self.enabled = bool(cfg.get("enabled", False))
        self.geo_gate = GeoGate(cfg.get("geo", {}))
        self.time_gate = TimeGate(cfg.get("time", {}), profile.tz)
        self.need_gate = NeedGate(cfg.get("need", {}))
        LOG.debug("ExportGate enabled=%s", self.enabled)

    def check(self, row: dict[str, Any], ctx: dict[str, Any] | None = None) -> GateResult:
        """检查一条线索是否满足导出门禁。

        row 需要包含：comment_text, commented_at, problem（可选）
        ctx 可选：ip_province
        """
        if not self.enabled:
            return GateResult(passed=True, geo_passed=True, time_passed=True, need_passed=True,
                              evidence="门禁未启用")

        text = row.get("comment_text", "") or ""
        commented_at = row.get("commented_at", "") or ""
        problem = row.get("problem", "") or ""
        ctx = ctx or {}
        ctx.setdefault("ip_province", row.get("ip_province", ""))

        # 地域门禁
        geo_ok, geo_reason, geo_evidence = self.geo_gate.check(text, ctx)

        # 时效门禁
        time_ok, time_reason, time_evidence = self.time_gate.check(commented_at)

        # 服务需求门禁
        need_ok, need_reason, need_evidence = self.need_gate.check(text, problem)

        passed = geo_ok and time_ok and need_ok

        if passed:
            evidence = f"地域：{geo_evidence}；时效：{time_evidence}；需求：{need_evidence}"
            return GateResult(passed=True, geo_passed=True, time_passed=True, need_passed=True, evidence=evidence)

        # 构造不通过原因
        reasons = []
        if not geo_ok:
            reasons.append(f"地域：{geo_reason}")
        if not time_ok:
            reasons.append(f"时效：{time_reason}")
        if not need_ok:
            reasons.append(f"需求：{need_reason}")

        return GateResult(
            passed=False,
            geo_passed=geo_ok,
            time_passed=time_ok,
            need_passed=need_ok,
            reason="；".join(reasons),
        )
