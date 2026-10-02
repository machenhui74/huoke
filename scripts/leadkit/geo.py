"""分层地域判断（与行业、城市无关，词表全部来自 profile 的 [geo] 与 [geo_filter]）。

为什么要有这一层：
  平台评论的 IP 属地最细只到省，台州用户显示的只是「浙江」，所以没有任何一个信号能单独确定「是台州人」。
  这里把多个弱信号分层叠加，给每条评论一个地域状态，再由 profile 决定每种状态怎么分流。

证据分三层（编码会写进 geo_signals，方便人工追溯）：
  硬证据  H1 评论正文有本地地名（过歧义校验） / H2 评论者 IP 属地就是目标城市
  软证据  S1 IP 属地=目标省 / S2 笔记（视频）标题、话题标签、描述含本地地名 /
          S3 昵称含本地地名（偏弱：本地商家号也会命中） / S4 评论含镇街、商圈等弱地名
  负证据  N1 IP 属地是外省或海外 / N2 评论自述在外地（「我在深圳」）/ N3 笔记明确是外地内容（含外市词、不含本地词）

状态：
  local_confirmed 有硬证据 | local_likely 软证据互相印证 | note_local 只有笔记本地 | weak 只有零星弱线索
  unknown 有上下文但没有任何地域线索 | no_context 没拿到笔记上下文（旧数据），无法判断，一律放行
  out_of_region 有负证据且无硬证据 | conflict 硬证据与负证据并存，交给人工

不做的事：不请求作者主页、不伪造定位、不把笔记发布者的属地当成评论者属地。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .logger import get_logger
from .profile import Profile

LOG = get_logger("geo")

# 状态 → 表里显示的中文
GEO_LABEL = {
    "local_confirmed": "确认本地", "local_likely": "可能本地", "note_local": "仅笔记本地", "weak": "弱线索",
    "unknown": "无地域线索", "no_context": "缺上下文", "out_of_region": "外地", "conflict": "地名冲突",
}
# 每个状态的默认处理。keep=不改判；review=最高只进待复核；exclude=排除。
DEFAULT_ACTIONS = {
    "local_confirmed": "keep", "local_likely": "keep", "note_local": "keep", "weak": "keep",
    "unknown": "keep", "no_context": "keep", "out_of_region": "exclude", "conflict": "review",
}
ACTIONS = ("keep", "review", "exclude")
# IP 属地里「没有信息」的取值，不当成外省处理
IP_IGNORE = {"", "未知", "未知地区", "无", "null", "none"}
# 评论者自述在外地的句式；{c} 会被外市词替换。故意只收明确自述，不收「杭州的」这类可能只是在谈论杭州的说法
SELF_OUT_TEMPLATES = [
    r"(?:我|我们|咱们|本人|人|现)(?:在|住在|住|搬到|来自|定居在?|常住)\s*{c}",
    r"在\s*{c}\s*(?:上|读|工作|生活|买房|定居)",
    r"{c}\s*(?:这边|这里|本地|当地)",
]


@dataclass
class Verdict:
    """一次地域判断的结果。"""

    state: str = ""                 # 空串 = 未启用
    signals: list[str] = field(default_factory=list)
    action: str = "keep"

    @property
    def label(self) -> str:
        return GEO_LABEL.get(self.state, "")


def province_of(label: str) -> str:
    """IP 属地原文 → 规整后的省/市名：去掉「IP属地：」前缀和「省/市」后缀。"""
    text = re.sub(r"^\s*ip\s*属地\s*[:：]?", "", label or "", flags=re.I).strip()
    return re.sub(r"[省市]$", "", text)


def _present(text: str, words: list[str], false_positive: list[tuple[str, str]],
             need_context: list[tuple[str, list[str]]]) -> list[str]:
    """文本里命中的本地地名。先扣掉「黄岩岛」这类误命中，再处理「天台」这类必须有语境词才算的。"""
    low = text.lower()
    out = []
    for w in words:
        if w.lower() not in low:
            continue
        if any(w == fw and ctx in text for fw, ctx in false_positive):
            continue
        out.append(w)
    for w, ctx_words in need_context:
        if w in text and any(c in text for c in ctx_words) and w not in out:
            out.append(w)
    return out


class GeoJudge:
    """绑定一个 Profile 的地域判断器。未启用时 judge() 返回空状态，调用方行为与旧版完全一致。"""

    def __init__(self, profile: Profile):
        cfg = profile.section("geo_filter")
        geo = profile.section("geo")
        self.enabled = bool(cfg.get("enabled", False))
        self.use_ip = bool(cfg.get("use_ip", True))
        self.note_out_is_out = bool(cfg.get("note_out_is_out", True))
        self.local_words = list(dict.fromkeys(
            list(geo.get("core", [])) + list(geo.get("main", [])) + list(geo.get("city", []))
            + list(cfg.get("local_words", []))))
        self.weak_words = list(dict.fromkeys(list(geo.get("street", [])) + list(cfg.get("weak_words", []))))
        # 弱地名自己的误命中（如「三甲医院」）沿用 [geo] 里已有的配置，再加 [geo_filter] 里的
        self.false_positive = [tuple(x) for x in list(geo.get("street_false_positive", [])) + list(cfg.get("false_positive", []))]
        self.need_context = [(w, list(ctx)) for w, ctx in cfg.get("need_context", [])]
        self.out_cities = list(dict.fromkeys(list(geo.get("outcity", [])) + list(cfg.get("out_cities", []))))
        self.ip_local = [province_of(x) for x in cfg.get("ip_local", [])]
        self.ip_province = [province_of(x) for x in cfg.get("ip_province", [])]
        templates = cfg.get("self_out_templates", SELF_OUT_TEMPLATES)
        cities = "|".join(re.escape(c) for c in self.out_cities)
        self.self_out = [re.compile(t.replace("{c}", f"({cities})")) for t in templates] if cities else []
        self.actions = {**DEFAULT_ACTIONS, **cfg.get("actions", {})}
        self.actions_by_platform = {p: dict(a) for p, a in cfg.get("actions_by_platform", {}).items()}
        for table in [self.actions, *self.actions_by_platform.values()]:
            for state, act in table.items():
                if state not in GEO_LABEL or act not in ACTIONS:
                    raise ValueError(f"[geo_filter] 动作配置不合法: {state}={act}，状态须在 {list(GEO_LABEL)}，动作须在 {ACTIONS}")
        LOG.debug("GeoJudge enabled=%s 本地词=%d 弱地名=%d 外市词=%d", self.enabled, len(self.local_words),
                  len(self.weak_words), len(self.out_cities))

    def action_for(self, state: str, platform: str) -> str:
        """平台专属配置优先于全局配置（抖音评论几乎不带地名，常要比小红书更严）。"""
        return self.actions_by_platform.get(platform, {}).get(state, self.actions.get(state, "keep"))

    def judge(self, text: str, ctx: dict[str, Any] | None = None) -> Verdict:
        """综合评论正文与上下文（昵称、笔记文本、IP 属地、平台）给出地域状态。"""
        if not self.enabled:
            return Verdict()
        ctx = ctx or {}
        raw = text or ""
        note = ctx.get("note_text") or ""
        platform = ctx.get("platform") or ""
        sig: list[str] = []

        h1 = _present(raw, self.local_words, self.false_positive, self.need_context)
        if h1:
            sig.append("H1:" + "/".join(h1[:3]))

        ip = province_of(ctx.get("ip_province") or "") if self.use_ip else ""
        ip = "" if ip.lower() in IP_IGNORE else ip
        h2 = bool(ip) and ip in self.ip_local
        s1 = bool(ip) and not h2 and ip in self.ip_province
        n1 = bool(ip) and not h2 and not s1
        if h2:
            sig.append(f"H2:{ip}")
        if s1:
            sig.append(f"S1:{ip}")
        if n1:
            sig.append(f"N1:{ip}")

        note_hits = _present(note, self.local_words, self.false_positive, self.need_context)
        s2 = bool(note_hits)
        if s2:
            sig.append("S2")
        s3 = bool(_present(ctx.get("nickname") or "", self.local_words, self.false_positive, self.need_context))
        if s3:
            sig.append("S3")
        s4 = bool(_present(raw, self.weak_words, self.false_positive, []))
        if s4:
            sig.append("S4")

        n2 = next((m.group(1) for r in self.self_out if (m := r.search(raw))), "")
        if n2:
            sig.append(f"N2:{n2}")
        note_out = [c for c in self.out_cities if c in note]
        n3 = bool(self.note_out_is_out and note_out and not s2)
        if n3:
            sig.append("N3:" + "/".join(note_out[:2]))

        hard, neg = bool(h1 or h2), bool(n1 or n2)
        soft = sum((s1, s3, s4))
        if hard and neg:
            state = "conflict"
        elif hard:
            state = "local_confirmed"
        elif neg or n3:
            state = "out_of_region"
        elif (s2 and soft) or (s1 and (s3 or s4)):
            state = "local_likely"
        elif s2:
            state = "note_local"
        elif soft:
            state = "weak"
        elif note or ip:
            state = "unknown"
        else:
            state = "no_context"   # 没有笔记文本也没有 IP：旧数据，不能因为「没线索」就误杀
        return Verdict(state, sig, self.action_for(state, platform))
