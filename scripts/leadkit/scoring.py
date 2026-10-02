"""公开评论意向打分引擎（与行业无关）。

回答三件事：评论者像不像目标客户（家长）、问题是什么、强不强。
所有词表、阈值、地名都来自 Profile，本模块不含任何行业词。
只做判断：不发评论、不关注、不私信。

流程（与原「识需 v0」一致）：
  1. 空/无效评论、广告引流 → excluded
  2. 词表分档：高 / 中 / 低 / 弱信号，叠加 家长、地点、预算、紧迫 加分
  3. 地理分（外市与本地混杂时放弃，交给人工）
  4. 无家长身份词则总分打折
  5. 「在问」的句子抬进复核（但不抬到 ready）
  6. 特殊需求封顶，只进复核
  7.（可选）明确咨询直接抬到 ready 线：问价 / 问怎么报怎么约 / 问几岁能上 / 问联系方式 /
     问课程时间活动 / 问有没有这种课 / 带本地地名问位置，由 profile 的 inquiry_ready_score 开启，默认关闭；
     带手机号的评论是商家引流，不抬
  8. 分流：ready / needs_review / low_archive
  9.（可选）分层地域过滤：笔记上下文 / 昵称 / IP 省级属地 / 自述外地 → 地域状态 → 保持 / 封顶复核 / 排除，
     由 profile 的 [geo_filter] 开启，默认关闭（见 geo.py）
"""
from __future__ import annotations

import math
import re
from typing import Any

from .geo import GeoJudge
from .logger import get_logger
from .profile import Profile

LOG = get_logger("scoring")

AGE_RE = re.compile(r"(?<!\d)(?:1[0-8]|[0-9])\s*(?:周岁|岁)|(?:几|多)(?:岁|大)|[0-9]{1,2}\s*个月|几个月")
# 评论里直接贴了手机号/座机：这是商家在引流，不是客户在问，永远不能被「明确咨询」抬分
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)|\d{3,4}-\d{7,8}|\d{8,}")
# 疑问形态。故意不含「嘛 / 吧 / 呢」：它们大多是语气词，不代表在提问
QUESTION_RE = re.compile(r"[?？]|吗|多少|怎么|哪|几|什么|有没有|能不能|可以不")
EMOJI_RE = re.compile(r"\[[^\[\]]{1,12}\]")
VISIBLE_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _hits(text: str, words: list[str]) -> list[str]:
    """按词表顺序返回命中的词（大小写不敏感）。调用方把长短语放前面。"""
    low = text.lower()
    return [w for w in words if w.lower() in low]


def _visible(text: str) -> str:
    """去掉表情占位后再量长度，单字和纯表情视为无效。"""
    return "".join(VISIBLE_RE.findall(EMOJI_RE.sub("", text or "")))


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


class Scorer:
    """绑定一个 Profile 的打分器。构造时预取词表，打分时不再碰 TOML。"""

    def __init__(self, profile: Profile):
        self.profile = profile
        sc = profile.section("scoring")
        self.ready_t = sc["ready_threshold"]
        self.review_t = sc["review_threshold"]
        self.high_base, self.mid_base = sc["high_base"], sc["mid_base"]
        self.low_base, self.weak_base = sc["low_base"], sc["weak_base"]
        self.non_parent_factor = sc.get("non_parent_factor", 0.6)
        self.bonus = {k: sc.get(f"{k}_bonus", 0) for k in ("parent", "loc", "budget", "urgent")}
        self.consult_floor = sc.get("consult_floor", 48)
        self.buyer_floor = sc.get("buyer_floor", 42)
        self.special_cap = sc.get("special_cap", 65)
        # 兜底：短评论带疑问形态、又沾上品类词/本地地名/孩子年龄，至少进复核池，不被直接丢掉。0 = 关闭。
        self.question_floor = sc.get("question_floor", 0)
        # 明确咨询（问价/问报名约课/问几岁能上）直接抬到这个分；0 = 关闭。
        # 默认关闭是因为「怎么收费」这类话在不同品类里的含金量不同，由各 profile 自己决定。
        self.inquiry_ready = sc.get("inquiry_ready_score", 0)

        wl = profile.wordlist
        self.high, self.mid, self.low = wl("high"), wl("mid"), wl("low")
        self.parent_words = wl("parent")
        self.budget, self.urgent, self.loc = wl("budget"), wl("urgent"), wl("loc")
        self.price_ask_words = wl("price_ask")
        self.price_ask_exempt = wl("price_ask_exempt")
        self.enroll_ask_words = wl("enroll_ask")
        self.age_ask_words = wl("age_ask")
        # 其余几类明确咨询（都写成提问的完整说法，避免把商家的陈述句误判成提问）
        self.contact_ask_words = wl("contact_ask")      # 有联系电话吗 / 怎么联系
        self.detail_ask_words = wl("detail_ask")        # 上多久 / 营业了吗 / 有活动吗 / 怎么拼
        self.loc_ask_words = wl("loc_ask")              # 在哪里 / 怎么走
        self.avail_ask_words = wl("avail_ask")          # 有吗 / 还有吗：须同时有地名或品类词才算
        self.avail_patterns = [re.compile(p) for p in profile.words.get("avail_patterns", [])]  # 有…课吗
        self.complaint_words = wl("complaint")
        self.complaint_exempt = wl("complaint_exempt")
        self.institution = wl("institution")
        self.special = wl("special")
        self.consult_q = wl("consult_q")
        self.ask_extra = wl("ask_extra")
        self.exclude = [tuple(x) for x in profile.words.get("exclude", [])]
        self.self_child = [re.compile(p) for p in profile.words.get("self_child_patterns", [])]

        self.age_cfg = profile.section("age")
        self.geo = profile.section("geo")
        self.geo_judge = GeoJudge(profile)   # 默认未启用，启用后才会改判
        self.rules = profile.data.get("problem_rules", [])
        self.problem_fallback = profile.section("problem_fallback").get("lifted", "")
        LOG.debug("Scorer 就绪: profile=%s high=%d mid=%d exclude=%d rules=%d",
                  profile.name, len(self.high), len(self.mid), len(self.exclude), len(self.rules))

    # ---------------- 谓词 ----------------
    def young_child_age(self, text: str) -> int | None:
        """评论里最小的儿童年龄；几个月记 0；超过 16 岁不当成在问孩子。"""
        if not self.age_cfg.get("enabled", False):
            return None
        ages: list[int] = []
        if any(w in text for w in self.age_cfg.get("unknown_age_words", [])):
            ages.append(0)
        for _ in re.finditer(r"(?<!\d)(\d{1,2})\s*个月", text):
            ages.append(0)
        for m in re.finditer(r"([零一二两三四五六七八九十])\s*(周岁|岁|个月)", text):
            ages.append(0 if m.group(2) == "个月" else _CN_NUM[m.group(1)])
        for m in re.finditer(r"(?<!\d)(\d{1,2})\s*(?:周岁|岁)", text):
            years = int(m.group(1))
            if years <= 16:
                ages.append(years)
        return min(ages) if ages else None

    def price_ask(self, text: str) -> bool:
        """是在问费用；「怎么也得多少钱」这类吐槽不算。"""
        if any(w in text for w in self.price_ask_exempt):
            return False
        return any(w in text for w in self.price_ask_words)

    def enroll_ask(self, text: str) -> bool:
        """是在问怎么报；「就没报名了」是事后抱怨，不在词表里。"""
        return any(w in text for w in self.enroll_ask_words)

    def age_ask(self, text: str) -> bool:
        """是在问「几岁能上 / 多大孩子能上」这类入门资格，说明在认真考虑让孩子去。"""
        return any(w in text for w in self.age_ask_words)

    def _any(self, words: list[str], text: str) -> bool:
        return any(w in text for w in words)

    def is_complaint(self, text: str) -> bool:
        """曝光、避雷、投诉；「求避雷」之后继续求推荐的不算。"""
        if any(w in text for w in self.complaint_exempt):
            return False
        return any(w in text for w in self.complaint_words)

    def parent_likely(self, text: str) -> bool:
        """评论者像在说自己的孩子；机构口吻、孩子自称不算。"""
        if any(p in text for p in self.institution):
            return False
        if any(r.search(text) for r in self.self_child):
            return False
        return bool(_hits(text, self.parent_words))

    def _predicate(self, name: str, text: str, is_parent: bool) -> bool:
        """problem_rules 里可引用的内置谓词。"""
        if name == "complaint":
            return self.is_complaint(text)
        if name == "price_ask":
            return self.price_ask(text)
        if name == "enroll_ask":
            return self.enroll_ask(text)
        if name == "age_known":
            return self.young_child_age(text) is not None
        if name == "parent":
            return is_parent
        raise ValueError(f"profile 的 problem_rules 引用了未知谓词: {name}")

    def problem_of(self, text: str, *, is_parent: bool) -> str:
        """按 profile 里的规则顺序归类问题；都不命中则留空。"""
        for rule in self.rules:
            words: list[str] = []
            for w in rule.get("words", []):
                words.extend(self.profile.wordlist(w[1:]) if w.startswith("@") else [w])
            hit = any(w in text for w in words) or any(
                self._predicate(p, text, is_parent) for p in rule.get("any_of", [])
            )
            if hit and all(self._predicate(p, text, is_parent) for p in rule.get("all_of", [])):
                return rule["name"]
        return ""

    def _is_special(self, text: str, search_keyword: str) -> bool:
        return any(w in f"{text}\n{search_keyword}" for w in self.special)

    # ---------------- 主入口 ----------------
    def _empty(self, reason: str = "") -> dict[str, Any]:
        return {
            "intent_score": 0, "tags": "排除", "status": "excluded", "exclude_reason": reason,
            "geo_hit": False, "target_region": "", "geo_evidence": "", "tier": "排除",
            "parent_likely": 0, "problem": "", "strength": "无", "geo_state": "", "geo_signals": "",
        }

    def _geo(self, raw: str, edu: bool) -> tuple[bool, int, list[str], str, bool, list[str]]:
        """地理分。返回 (命中, 地理分, 证据词, 地区, 是否地名混杂, 街道词)。"""
        g = self.geo
        core, main = _hits(raw, g.get("core", [])), _hits(raw, g.get("main", []))
        city, street = _hits(raw, g.get("city", [])), _hits(raw, g.get("street", []))
        for word, ctx in g.get("street_false_positive", []):
            if ctx in raw:
                street = [s for s in street if s != word]
        out = _hits(raw, g.get("outcity", []))
        local_hits = core + main + city + street
        mixed = bool(out and local_hits)
        if out:  # 外市词存在：混杂交人工，纯外市不给分
            return False, 0, [], "", mixed, street
        bonus, evidence, region = 0, [], ""
        if core:
            bonus += g.get("bonus_core", 20)
            evidence += core
            region = g.get("region_core", "")
        elif main:
            bonus += g.get("bonus_main", 12)
            evidence += main
            region = main[0]
        elif city:
            bonus += g.get("bonus_city", 8)
            evidence += city
            region = g.get("region_city", "")
        if street:
            evidence += street
            region = region or g.get("region_core", "")
            if edu:  # 街道名单独出现不加满分，必须同时有教育意向词
                bonus += g.get("bonus_street", 15)
        bonus = min(g.get("bonus_cap", 20), bonus)
        return bool(evidence), bonus, evidence, region, False, street

    def score(self, text: str, search_keyword: str = "", ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """给一条公开评论打分并写出人工池字段。

        status 为 ready / needs_review / low_archive / excluded；入库时 low_archive
        记成 excluded 并在 exclude_reason 里保留原档。
        ctx（可选）= {nickname, note_text, ip_province, platform}，只给地域过滤用；不传则与旧版结果完全一致。
        """
        raw = text or ""
        if len(_visible(raw)) <= 1:
            LOG.debug("空/无效评论，排除")
            return self._empty("空/无效评论")
        low = raw.lower()
        for w, kind in self.exclude:
            if w.lower() in low:
                LOG.debug("硬排除 %s:%s", kind, w)
                return self._empty(f"{kind}:{w}")

        high_h, mid_h, low_h = _hits(raw, self.high), _hits(raw, self.mid), _hits(raw, self.low)
        is_parent = self.parent_likely(raw)
        age_years = self.young_child_age(raw)
        institution = any(p in raw for p in self.institution)
        consult_q = _hits(raw, self.consult_q)
        complaint = self.is_complaint(raw)
        price_ask, enroll_ask = self.price_ask(raw), self.enroll_ask(raw)

        # 低龄孩子还在问能不能上、多少钱：写评论的一定是家长
        young_max = self.age_cfg.get("young_max", 6)
        if (
            not institution and not complaint
            and age_years is not None and age_years <= young_max
            and (bool(consult_q) or price_ask or enroll_ask
                 or any(w in raw for w in self.age_cfg.get("young_ask_words", [])))
        ):
            is_parent = True

        budget_h, urgent_h, loc_h = _hits(raw, self.budget), _hits(raw, self.urgent), _hits(raw, self.loc)
        age_hit = bool(AGE_RE.search(raw)) if self.age_cfg.get("enabled", False) else False

        if high_h:
            base, tier = self.high_base, "高意向"
        elif mid_h:
            base, tier = self.mid_base, "中意向"
        elif low_h:
            base, tier = self.low_base, "低意向"
        else:
            base, tier = self.weak_base, "弱信号"

        b = self.bonus
        bonus = (b["parent"] if is_parent else 0) + (b["loc"] if loc_h else 0) \
            + (b["budget"] if budget_h else 0) + (b["urgent"] if urgent_h else 0)

        edu = bool(high_h or mid_h or low_h)
        geo_hit, geo_bonus, evidence, region, mixed, street = self._geo(raw, edu)

        total = base + bonus + geo_bonus
        if not is_parent:
            total = int(math.floor(total * self.non_parent_factor))

        # 「在问」的句子抬进复核池，但不抬到 ready
        lo, hi = self.age_cfg.get("older_range", [7, 16])
        older_learn = (
            age_years is not None and lo <= age_years <= hi
            and (bool(consult_q) or any(w in raw for w in self.age_cfg.get("older_learn_words", [])))
        )
        has_phone = bool(PHONE_RE.search(raw))          # 贴了手机号 = 商家引流，新增的几条抬分路径一律不认
        loc_hit = self._any(self.loc_ask_words, raw) and not has_phone   # 单独问位置：无地名只进复核，有本地地名算明确咨询
        buyer_ask = not institution and not complaint and (price_ask or enroll_ask or older_learn or loc_hit)
        asking = bool(consult_q) or price_ask or enroll_ask or any(w in raw for w in self.ask_extra)
        consult = is_parent and not institution and not complaint and asking and (
            age_hit or bool(consult_q) or age_years is not None or price_ask or enroll_ask
        )
        lifted = False
        if consult and total < self.review_t:
            total, tier, lifted = self.consult_floor, "咨询", True
        elif buyer_ask and total < self.review_t:
            total, tier, lifted = self.buyer_floor, "咨询", True
        elif (self.question_floor and total < self.review_t and not institution and not complaint and not has_phone
              and len(_visible(raw)) <= 40 and QUESTION_RE.search(raw)
              and (edu or geo_hit or age_years is not None)):
            total, tier, lifted = self.question_floor, "疑问", True

        special = self._is_special(raw, search_keyword)
        if special and total > self.special_cap:
            total = self.special_cap

        # 明确咨询：问价 / 问报名约课 / 问几岁能上。机构口吻、投诉、特殊需求不在此列（特殊需求仍只进复核）。
        avail = ((self._any(self.avail_ask_words, raw) and (geo_hit or edu))
                 or any(r.search(raw) for r in self.avail_patterns))
        inquiry = (not institution and not complaint and not special and not has_phone
                   and (price_ask or enroll_ask or self.age_ask(raw)
                        or self._any(self.contact_ask_words, raw) or self._any(self.detail_ask_words, raw)
                        or avail or (loc_hit and geo_hit)))
        promoted = False
        if self.inquiry_ready and inquiry and total < self.inquiry_ready:
            total, tier, promoted = self.inquiry_ready, "明确咨询", True
        total = max(0, min(100, total))

        if total >= self.ready_t and not special:
            status = "ready"
        elif total >= self.review_t:
            status = "needs_review"
        else:
            status = "low_archive"

        # 分层地域过滤：只能把 ready 压到复核、或把进池的线索排除，从不抬分，也不碰已是低档的线索
        verdict = self.geo_judge.judge(raw, ctx)
        exclude_reason = ""
        if verdict.state and status in ("ready", "needs_review"):
            if verdict.action == "exclude":
                LOG.info("地域排除 %s state=%s signals=%s", raw[:20].replace("\n", " "), verdict.state, verdict.signals)
                status, exclude_reason = "excluded", f"geo:{verdict.state}"
            elif verdict.action == "review" and status == "ready":
                LOG.info("地域封顶复核 %s state=%s signals=%s", raw[:20].replace("\n", " "), verdict.state, verdict.signals)
                status = "needs_review"

        problem = self.problem_of(raw, is_parent=is_parent)
        if (lifted or promoted) and not problem:
            problem = self.problem_fallback
        strength = {"ready": "高", "needs_review": "中"}.get(status, "低")

        tags = [tier]
        shown: list[str] = []
        for group in (high_h, mid_h, low_h, budget_h, urgent_h, consult_q):
            for w in group:
                if w not in shown and not any(w != s and w in s for s in shown):
                    shown.append(w)
        tags.extend(shown[:6])
        tags.append("家长" if is_parent else f"无家长词×{self.non_parent_factor}")
        if problem:
            tags.append(problem)
        if lifted:
            tags.append("咨询抬入复核")
        if promoted:
            tags.append("明确咨询抬入高相关")
        if special:
            tags.append("特殊需求封顶")
        if budget_h and "预算" not in tags:
            tags.append("预算")
        if urgent_h:
            tags.append("紧迫")
        if loc_h:
            tags.append("地点")
        if verdict.state and verdict.state != "no_context":
            tags.append(f"地域:{verdict.label}")
        if mixed:
            tags.append("地名混杂需人工")
        elif geo_hit:
            tags.append(f"地理+{geo_bonus}")
            if region:
                tags.append(region)
            if street and not edu:
                tags.append("街道仅标记")

        LOG.debug("打分 %s → %s %s problem=%s", raw[:20].replace("\n", " "), total, status, problem)
        return {
            "intent_score": total,
            "tags": ",".join(_dedupe(tags)),
            "status": status,
            "exclude_reason": exclude_reason,
            "geo_hit": geo_hit,
            "geo_state": verdict.state,
            "geo_signals": ",".join(verdict.signals),
            "target_region": region if geo_hit else "",
            "geo_evidence": ",".join(_dedupe(evidence)) if geo_hit else "",
            "tier": tier,
            "parent_likely": 1 if is_parent else 0,
            "problem": problem,
            "strength": strength,
        }

    # ---------------- 自检 ----------------
    def self_check(self) -> list[dict[str, Any]]:
        """跑 profile 里的 [[cases]]，返回失败列表（空 = 全部通过）。"""
        bad: list[dict[str, Any]] = []
        cases = self.profile.data.get("cases", [])
        for c in cases:
            got = self.score(c["text"], c.get("keyword", ""))
            wrong = (
                got["status"] != c["status"]
                or got["parent_likely"] != c["parent"]
                or got["problem"] != c["problem"]
                or got["strength"] != c["strength"]
                or got["intent_score"] < c.get("min_score", 0)
                or got["intent_score"] > c.get("max_score", 100)
            )
            if wrong:
                bad.append({"case": c, "got": got})
        LOG.info("自检 %d 条用例，失败 %d 条", len(cases), len(bad))
        for b in bad:
            LOG.error("自检失败: %s 期望 %s 实际 %s/%s/%s/%s", b["case"]["text"], b["case"]["status"],
                      b["got"]["status"], b["got"]["parent_likely"], b["got"]["problem"], b["got"]["strength"])
        return bad
