"""护栏：限额、台账、运行时监控。"""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import _helpers
from leadkit.guard import (
    CollectPlan, Ledger, RunMonitor, check_patch, effective_limits, validate_overrides, validate_plan,
)
from leadkit.profile import load_platform_map

OK_PLAN = dict(platform="xhs", keywords=["a", "b", "c"], notes_per_keyword=5, comments_per_note=3)


def limits(**over):
    return effective_limits({**_helpers.profile().section("limits"), **over}, False)


class PlanTest(unittest.TestCase):
    def test_default_plan_passes(self):
        self.assertEqual(validate_plan(CollectPlan(**OK_PLAN), limits()), [])

    def test_each_hard_limit(self):
        cases = [
            dict(keywords=list("abcdefghijk")), dict(notes_per_keyword=6), dict(comments_per_note=11),
            dict(concurrency=2), dict(sleep_sec=2), dict(sub_comments=True), dict(proxy=True),
            dict(media=True), dict(login="phone"), dict(keywords=["a", "a"]),
        ]
        for c in cases:
            with self.subTest(c):
                self.assertTrue(validate_plan(CollectPlan(**{**OK_PLAN, **c}), limits()), c)

    def test_profile_cannot_raise_limits(self):
        """profile 写大了也会被压回硬上限。"""
        self.assertEqual(limits(max_keywords=12)["max_keywords"], 10)
        self.assertEqual(limits(min_sleep_sec=0)["min_sleep_sec"], 3)

    def test_exceed_flag_only_relaxes_numeric_limits(self):
        lim = effective_limits({"max_keywords": 12}, True)
        plan = CollectPlan(**{**OK_PLAN, "keywords": list("abcdefghijklm"), "proxy": True})
        bad = validate_plan(plan, lim, allow_exceed=True)
        self.assertEqual(len(bad), 1)  # 关键词数被放行，代理仍然拒绝
        self.assertIn("代理", bad[0])

    def test_override_whitelist(self):
        self.assertEqual(validate_overrides({"CDP_CONNECT_EXISTING": False}), [])
        self.assertTrue(validate_overrides({"CRAWLER_MAX_SLEEP_SEC": 0}))
        self.assertTrue(validate_overrides({"ENABLE_IP_PROXY": True}))


class PatchTest(unittest.TestCase):
    def test_patch_detection(self):
        pmap = load_platform_map("xhs")
        with tempfile.TemporaryDirectory() as d:
            mc = Path(d)
            for rel in (pmap["patch"]["file"], pmap["patch"]["core"]):
                (mc / rel).parent.mkdir(parents=True, exist_ok=True)
                (mc / rel).write_text("# 没有补丁", encoding="utf-8")
            self.assertTrue(check_patch(mc, pmap))
            for rel in (pmap["patch"]["file"], pmap["patch"]["core"]):
                (mc / rel).write_text(pmap["patch"]["marker"], encoding="utf-8")
            self.assertEqual(check_patch(mc, pmap), [])

    def test_unverified_platform_needs_flag(self):
        with tempfile.TemporaryDirectory() as d:
            ks = load_platform_map("ks")
            self.assertTrue(check_patch(Path(d), ks))             # 目录空：先报缺失
            (Path(d) / "x").write_text("", encoding="utf-8")
            self.assertTrue(check_patch(Path(d), ks, False))
            self.assertEqual(check_patch(Path(d), ks, True), [])


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tz = _helpers.profile().tz
        self.ledger = Ledger(Path(self.tmp.name), self.tz)
        self.plan = CollectPlan(**OK_PLAN)
        self.lim = limits()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, batch, notes=15, comments=45):
        self.ledger.append(event="start", batch=batch, est_notes=notes, est_comments=comments)
        self.ledger.append(event="end", batch=batch, status="ok", actual_notes=notes, actual_comments=comments)

    def test_empty_ledger_allows(self):
        self.assertEqual(self.ledger.check(self.plan, self.lim), [])

    def test_cooldown_after_run(self):
        self._run("b1", 5, 15)
        self.assertTrue(any("30 分钟" in b for b in self.ledger.check(self.plan, self.lim)))

    def test_daily_quota_uses_actuals(self):
        self._run("b1", 50, 495)  # 当天已用满
        small = CollectPlan(**{**OK_PLAN, "keywords": ["a"], "comments_per_note": 3})
        self.assertTrue(any("评论" in b for b in self.ledger.check(small, effective_limits({"cooldown_minutes": 0}, True))))

    def _run_on(self, platform, batch, notes, comments):
        self.ledger.append(event="start", batch=batch, platform=platform, est_notes=notes, est_comments=comments)
        self.ledger.append(event="end", batch=batch, status="ok", actual_notes=notes, actual_comments=comments)

    def test_quota_is_counted_per_platform(self):
        """小红书和抖音是两个账号：小红书用满，不应挡住抖音；但挡住小红书自己。"""
        self._run_on("xhs", "b1", 50, 180)
        no_cd = effective_limits({"cooldown_minutes": 0}, True)
        dy = CollectPlan(**{**OK_PLAN, "platform": "dy", "keywords": ["a"], "comments_per_note": 3})
        xhs = CollectPlan(**{**OK_PLAN, "keywords": ["a"], "comments_per_note": 3})
        self.assertEqual(self.ledger.check(dy, no_cd), [])
        bad = self.ledger.check(xhs, no_cd)
        self.assertTrue(any("xhs" in b and "笔记" in b for b in bad), bad)
        self.assertEqual(self.ledger.usage_today("dy")["notes"], 0)
        self.assertEqual(self.ledger.usage_today()["notes"], 50)            # 不传平台 = 合计

    def test_same_platform_still_accumulates(self):
        self._run_on("dy", "b1", 30, 100)
        self._run_on("dy", "b2", 20, 100)
        self.assertEqual(self.ledger.usage_today("dy")["notes"], 50)
        self.assertEqual(self.ledger.usage_today("dy")["sessions"], 2)

    def test_legacy_batches_without_platform_count_everywhere(self):
        """老台账没有 platform 字段，无法归属：保守地算进每个平台，宁多勿少。"""
        self._run("legacy", 40, 100)
        self.assertEqual(self.ledger.usage_today("dy")["notes"], 40)
        self.assertEqual(self.ledger.usage_today("xhs")["notes"], 40)

    def test_cooldown_and_block_lock_stay_global(self):
        """额度分平台，但冷却和封控锁仍是全局：一个平台刚被风控，另一个也不该马上接着采。"""
        self.ledger.append(event="block", batch="b1", platform="xhs", reason="验证码")
        dy = CollectPlan(**{**OK_PLAN, "platform": "dy"})
        self.assertTrue(any("风控" in b for b in self.ledger.check(dy, self.lim)))

    def test_crashed_run_counts_by_estimate(self):
        """只有 start 没有 end（进程崩了）：按估算值计入，宁多勿少。"""
        self.ledger.append(event="start", batch="b1", est_notes=15, est_comments=45)
        self.assertEqual(self.ledger.usage_today()["notes"], 15)

    def test_block_locks_for_hours(self):
        self.ledger.append(event="block", batch="b1", reason="验证码")
        bad = self.ledger.check(self.plan, self.lim)
        self.assertTrue(any("风控" in b for b in bad))

    def test_two_blocks_lock_the_day(self):
        for i in range(2):
            self.ledger.append(event="block", batch=f"b{i}", reason="x")
        self.assertTrue(any("封存" in b for b in self.ledger.check(self.plan, self.lim)))


class MonitorTest(unittest.TestCase):
    def mon(self, **kw):
        plan = CollectPlan(**{**OK_PLAN, **kw})
        return RunMonitor.from_platform(plan, load_platform_map("xhs"))

    KEY = "x INFO (c.py:1) - [XiaoHongShuCrawler.search] Current search keyword: {}"
    DETAIL = "x INFO (c.py:2) - [get_note_detail_async_task] Begin get note detail, note_id: {}"

    def test_overfetch_per_keyword(self):
        m = self.mon(keywords=["a"])
        m.feed(self.KEY.format("a"))
        got = [m.feed(self.DETAIL.format(i)) for i in range(6)]
        self.assertTrue(all(g is None for g in got[:5]))
        self.assertIn("超过上限", got[5])
        self.assertEqual(m.abort_kind, "overfetch")

    def test_counter_resets_on_new_keyword(self):
        m = self.mon(keywords=["a", "b"])
        for kw in ("a", "b"):
            m.feed(self.KEY.format(kw))
            self.assertTrue(all(m.feed(self.DETAIL.format(i)) is None for i in range(5)))

    def test_block_signal_on_error_line(self):
        m = self.mon()
        self.assertTrue(m.feed("x ERROR (client.py:1) - CAPTCHA appeared, request failed"))
        self.assertEqual(m.abort_kind, "block")

    def test_comment_text_does_not_false_trigger(self):
        """评论原文里含「验证码」，且出现在 INFO 的存储回显行里：不能误停。"""
        m = self.mon()
        line = "x INFO (s.py:9) - [store.xhs.update_xhs_note_comment] comment content: 这个验证码好烦 滑块"
        self.assertIsNone(m.feed(line))
        self.assertIsNone(m.feed("x WARNING (s.py:9) - [store.xhs.update_xhs_note_comment] 滑块"))


if __name__ == "__main__":
    unittest.main()
