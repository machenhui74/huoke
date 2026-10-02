"""节奏随机化与关键词队列。"""
import asyncio
import contextlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import _helpers
from leadkit import cli
from leadkit.collectors import BACKENDS, CollectResult, MediaCrawlerCollector, pacing
from leadkit.guard import Ledger
from leadkit.queue import MAX_ATTEMPTS, KeywordQueue, parse_proposal, parse_words


class PacingTest(unittest.TestCase):
    def sample(self, n=6000, base=3, cfg=None, seed=7):
        p = pacing.Pacer(base, cfg, random.Random(seed))
        return [p.next() for _ in range(n)]

    def test_never_faster_than_floor(self):
        """红线 §1：间隔下限 3 秒。随机化只能更慢。"""
        self.assertGreaterEqual(min(d for d, _ in self.sample()), 3)

    def test_regular_intervals_respect_cap(self):
        cfg = pacing.normalize(None)
        regular = [d for d, long in self.sample() if not long]
        self.assertLessEqual(max(regular), 3 * cfg["cap_factor"])

    def test_long_pause_in_configured_range_and_rate(self):
        cfg = pacing.normalize(None)
        s = self.sample()
        rate = sum(1 for _, long in s if long) / len(s)
        self.assertAlmostEqual(rate, cfg["long_pause_prob"], delta=0.03)
        lo, hi = cfg["long_pause_sec"]
        self.assertTrue(all(3 + lo <= d <= 3 * cfg["cap_factor"] + hi for d, long in s if long))

    def test_no_spike_at_the_cap(self):
        """重抽而不是截断：不能有大量间隔恰好等于上限值。"""
        cap = 3 * pacing.normalize(None)["cap_factor"]
        regular = [d for d, long in self.sample() if not long]
        self.assertLess(sum(1 for d in regular if d == cap) / len(regular), 0.02)

    def test_not_a_constant(self):
        self.assertGreater(len({round(d, 2) for d, _ in self.sample(200)}), 150)

    def test_deterministic_with_seed(self):
        self.assertEqual(self.sample(50, seed=1), self.sample(50, seed=1))

    def test_validate(self):
        base = 3
        self.assertEqual(pacing.validate(pacing.normalize(None), base), [])
        bad = lambda **kw: pacing.validate(pacing.normalize(kw), base)
        self.assertTrue(bad(median_factor=0.5))                   # 会比下限更快
        self.assertTrue(bad(long_pause_sec=[1, 10]))              # 长停顿比下限还短
        self.assertTrue(bad(long_pause_sec=[10, 5]))
        self.assertTrue(bad(long_pause_sec=[10, 900]))
        self.assertTrue(bad(long_pause_prob=0.9))
        self.assertTrue(bad(cap_factor=1.0, median_factor=2.0))
        self.assertTrue(bad(sigma="x"))
        self.assertEqual(bad(enabled=False, median_factor=0.1), [])  # 关闭时不校验

    def test_install_only_intercepts_crawl_interval(self):
        """只换掉「等于请求间隔」的 sleep，登录轮询之类的其它等待原样放行。"""
        seen = []

        class FakeAsyncio:
            @staticmethod
            async def sleep(delay, result=None):
                seen.append(delay)
                return result

        pacer = pacing.install(FakeAsyncio, 3, None, emit=lambda m: None, rng=random.Random(3))
        self.assertIsNotNone(pacer)

        async def go():
            r = await FakeAsyncio.sleep(3, result="x")
            await FakeAsyncio.sleep(0.5)
            await FakeAsyncio.sleep(6)
            return r

        self.assertEqual(asyncio.run(go()), "x")           # result 参数要原样传回
        self.assertGreaterEqual(seen[0], 3)
        self.assertNotEqual(seen[0], 3)                    # 被换掉了
        self.assertEqual(seen[1:], [0.5, 6])                # 其它不动

    def test_install_disabled_leaves_asyncio_alone(self):
        class FakeAsyncio:
            @staticmethod
            async def sleep(delay, result=None):
                return result

        orig = FakeAsyncio.sleep
        self.assertIsNone(pacing.install(FakeAsyncio, 3, {"enabled": False}))
        self.assertIs(FakeAsyncio.sleep, orig)

    def test_session_estimate_is_longer_than_fixed(self):
        fixed = pacing.estimate_minutes(15, 3, 3, {"enabled": False})
        paced = pacing.estimate_minutes(15, 3, 3, None)
        self.assertGreater(paced, fixed * 2)

    def test_runner_applies_pacing_in_a_real_subprocess(self):
        """用真实 mc_runner.py 跑一个假的「上游」：确认环境变量传到了、sleep 真的被拉长。"""
        runner = Path(_helpers.ROOT) / "scripts" / "leadkit" / "collectors" / "mc_runner.py"
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.py").write_text("", encoding="utf-8")
            (Path(d) / "main.py").write_text(textwrap.dedent("""
                import asyncio, time
                async def go():
                    t = time.monotonic()
                    await asyncio.sleep(0.01)          # 等于 base → 应被换成 0.01*10=0.1
                    print("ELAPSED", time.monotonic() - t)
                asyncio.run(go())
            """), encoding="utf-8")
            env = {**os.environ, "LEADKIT_MC_OVERRIDES": "{}", "PYTHONIOENCODING": "utf-8",
                   "LEADKIT_PACING": json.dumps({"base": 0.01, "median_factor": 10, "sigma": 0, "cap_factor": 10,
                                                 "long_pause_prob": 0, "long_pause_sec": [1, 2]})}
            out = subprocess.run([sys.executable, str(runner)], cwd=d, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("LEADKIT_PACING_APPLIED base=0.01 on", out.stdout)
        elapsed = float(next(l for l in out.stdout.splitlines() if l.startswith("ELAPSED")).split()[1])
        self.assertGreaterEqual(elapsed, 0.09)


class CollectorPacingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from leadkit.paths import Workspace
        self.prof = _helpers.profile()
        self.col = MediaCrawlerCollector(Workspace(Path(self.tmp.name)), self.prof, Path(self.tmp.name) / "mc")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bad_profile_pacing_is_refused_at_preflight(self):
        from leadkit.guard import CollectPlan
        from leadkit.profile import load_platform_map
        self.prof.data.setdefault("collect", {})["pacing"] = {"median_factor": 0.2}
        issues = self.col.preflight(CollectPlan("xhs", ["a"], 5, 3), load_platform_map("xhs"))
        self.assertTrue(any("pacing" in i for i in issues))

    def test_describe_mentions_pacing_and_duration(self):
        from leadkit.guard import CollectPlan
        from leadkit.profile import load_platform_map
        text = self.col.describe(CollectPlan("xhs", ["a", "b", "c"], 5, 3), Path("/b"), load_platform_map("xhs"))
        self.assertIn("随机间隔", text)
        self.assertIn("分钟", text)

    def test_describe_warns_when_disabled(self):
        from leadkit.guard import CollectPlan
        from leadkit.profile import load_platform_map
        self.prof.data.setdefault("collect", {})["pacing"] = {"enabled": False}
        text = self.col.describe(CollectPlan("xhs", ["a"], 5, 3), Path("/b"), load_platform_map("xhs"))
        self.assertIn("固定间隔", text)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "q.json"
        self.q = KeywordQueue(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_words(self):
        self.assertEqual(parse_words("甲，乙, 丙\t# 注释 ,,丁"), ["甲", "乙", "丙", "丁"])

    def test_parse_proposal_skips_comments_and_nearby(self):
        f = Path(self.tmp.name) / "p.txt"
        f.write_text("# 头\n# 说明\n词一\n词二\n邻区词\t# 附近区，仅备选；本次不要采集\n\n词三\n", encoding="utf-8")
        self.assertEqual(parse_proposal(f), ["词一", "词二", "词三"])

    def test_add_keeps_order_and_dedupes_with_reason(self):
        self.assertEqual(self.q.add("xhs", ["a", "b"])["added"], ["a", "b"])
        res = self.q.add("xhs", ["b", "c", "x" * 40])
        self.assertEqual(res["added"], ["c"])
        self.assertIn("已在队列", res["skipped"]["b"])
        self.assertIn("太长", res["skipped"]["x" * 40])
        self.assertEqual([i["keyword"] for i in self.q.pending("xhs")], ["a", "b", "c"])

    def test_platforms_are_independent(self):
        self.q.add("xhs", ["a"])
        self.assertEqual(self.q.add("dy", ["a"])["added"], ["a"])
        self.assertEqual(len(self.q.pending("dy")), 1)

    def test_done_words_are_not_requeued_or_picked(self):
        self.q.add("xhs", ["a", "b"])
        self.q.mark_done("xhs", ["a"], "batch1")
        self.assertEqual([i["keyword"] for i in self.q.pending("xhs")], ["b"])
        self.assertIn("已采过", self.q.add("xhs", ["a"])["skipped"]["a"])

    def test_failures_get_stuck_after_max_attempts(self):
        self.q.add("xhs", ["a", "b"])
        for _ in range(MAX_ATTEMPTS):
            self.q.mark_failed("xhs", ["a"], "bx")
        self.assertEqual([i["keyword"] for i in self.q.pending("xhs")], ["b"])
        self.assertEqual(self.q.counts("xhs")["stuck"], 1)
        self.assertEqual(self.q.retry("xhs"), ["a"])   # 不传词 = 恢复所有卡住的
        self.assertEqual(len(self.q.pending("xhs")), 2)

    def test_uncounted_failure_does_not_get_word_stuck(self):
        """被风控打断不是词的问题，不能因此把词判死刑。"""
        self.q.add("xhs", ["a"])
        for _ in range(MAX_ATTEMPTS + 2):
            self.q.mark_failed("xhs", ["a"], "bx", count=False)
        self.assertEqual(len(self.q.pending("xhs")), 1)

    def test_skip_and_retry(self):
        self.q.add("xhs", ["a", "b"])
        self.q.skip("xhs", ["a"])
        self.assertEqual([i["keyword"] for i in self.q.pending("xhs")], ["b"])
        self.q.retry("xhs", ["a"])
        self.assertEqual(len(self.q.pending("xhs")), 2)

    def test_persists_across_instances(self):
        self.q.add("xhs", ["a"])
        self.assertEqual(len(KeywordQueue(self.path).pending("xhs")), 1)

    def test_corrupt_file_is_kept_and_treated_as_empty(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.q.pending("xhs"), [])
        self.assertTrue(self.path.with_suffix(".bad.json").exists())   # 坏文件保留给人看
        self.assertEqual(self.q.add("xhs", ["a"])["added"], ["a"])      # 之后能正常用


class FakeCollector(MediaCrawlerCollector):
    """不碰上游、不碰网络的后端：预检放行，run 返回预设结果。"""
    status = "ok"

    def preflight(self, *a, **k):
        return []

    def run(self, plan, platform_map, batch_id, batch_dir):
        return CollectResult(batch_id, batch_dir, FakeCollector.status, 0, plan.est_notes, plan.est_comments)


class CliQueueFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = self.tmp.name
        p = mock.patch.dict(BACKENDS, {"fake": FakeCollector})
        p.start()
        self.addCleanup(p.stop)
        FakeCollector.status = "ok"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        """返回 (退出码, stdout 里的 JSON)。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--workdir", self.wd, "--json", "-q", *argv])
        text = buf.getvalue().strip()
        return rc, (json.loads(text) if text.startswith(("{", "[")) else text)

    def queue(self):
        return KeywordQueue(Path(self.wd) / "state" / f"queue_{_helpers.PROFILE}.json")

    def test_keywords_enqueue_excludes_nearby_and_puts_agent_words_first(self):
        rc, out = self.run_cli("keywords", "--category", "感统训练", "--place", "台州黄岩",
                               "--add", "孩子注意力不集中怎么办", "--enqueue")
        self.assertEqual(rc, 0)
        queued = [i["keyword"] for i in self.queue().pending("xhs")]
        self.assertEqual(queued[0], "孩子注意力不集中怎么办")
        self.assertTrue(out["nearby"])
        self.assertFalse(any(w.startswith(n) for w in queued for n in out["nearby"]))   # 备选词没入队
        self.assertEqual(self.queue().items("xhs")[0]["source"], "agent")

    def test_next_dry_run_does_not_touch_queue(self):
        self.queue().add("xhs", ["a", "b", "c", "d"])
        rc, out = self.run_cli("collect", "--next", "--backend", "fake")
        self.assertEqual(rc, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.queue().counts("xhs")["pending"], 4)

    def test_next_takes_up_to_max_keywords_and_marks_done(self):
        words = [f"w{i:02d}" for i in range(13)]
        self.queue().add("xhs", words)
        rc, out = self.run_cli("collect", "--next", "--yes", "--backend", "fake")
        self.assertEqual(rc, 0)
        self.assertEqual(out["queue"]["done"], 10)          # 单次最多 10 个词（HARD_CEILING）
        self.assertEqual([i["keyword"] for i in self.queue().pending("xhs")], words[10:])

    def test_max_words_takes_fewer_words_and_only_those_are_marked_done(self):
        """新平台首次实机：--max-words 3 只取 3 个，且只有这 3 个被标记已采，其余仍待采。"""
        words = [f"w{i:02d}" for i in range(8)]
        self.queue().add("xhs", words)
        rc, out = self.run_cli("collect", "--next", "--max-words", "3", "--yes", "--backend", "fake")
        self.assertEqual(rc, 0)
        self.assertEqual(out["queue"]["done"], 3)
        self.assertEqual([i["keyword"] for i in self.queue().pending("xhs")], words[3:])

    def test_max_words_cannot_exceed_quota_or_hard_limit(self):
        self.queue().add("xhs", [f"w{i:02d}" for i in range(13)])
        rc, out = self.run_cli("collect", "--next", "--max-words", "99", "--yes", "--backend", "fake")
        self.assertEqual(out["queue"]["done"], 10)          # 传得再大也不超过 HARD_CEILING

    def test_next_takes_fewer_words_when_daily_quota_is_low(self):
        """今日笔记配额只剩 10 篇、每词 5 篇 → 只够取 2 个词，而不是取满 10 个再被预检拒绝。"""
        self.queue().add("xhs", ["a", "b", "c", "d"])
        prof = _helpers.profile()
        Ledger(Path(self.wd) / "state", prof.tz).append(event="start", batch="earlier", est_notes=40, est_comments=300)
        rc, out = self.run_cli("collect", "--next", "--yes", "--backend", "fake")
        self.assertEqual(rc, 0)
        self.assertEqual(out["queue"]["done"], 2)

    def test_failed_run_keeps_words_and_counts_attempt(self):
        self.queue().add("xhs", ["a", "b"])
        FakeCollector.status = "aborted:overfetch"
        rc, out = self.run_cli("collect", "--next", "--yes", "--backend", "fake")
        self.assertEqual(rc, 1)
        items = self.queue().items("xhs")
        self.assertEqual([i["status"] for i in items], ["pending", "pending"])
        self.assertEqual([i["attempts"] for i in items], [1, 1])

    def test_blocked_run_does_not_count_against_words(self):
        self.queue().add("xhs", ["a"])
        FakeCollector.status = "aborted:block"
        self.run_cli("collect", "--next", "--yes", "--backend", "fake")
        self.assertEqual(self.queue().items("xhs")[0]["attempts"], 0)

    def test_empty_queue_is_not_an_error(self):
        rc, out = self.run_cli("collect", "--next", "--backend", "fake")
        self.assertEqual(rc, 0)
        self.assertTrue(out["empty"])

    def test_next_and_keywords_are_mutually_exclusive(self):
        rc, _ = self.run_cli("collect", "--next", "--keywords", "a", "--backend", "fake")
        self.assertEqual(rc, 2)
        rc, _ = self.run_cli("collect", "--backend", "fake")
        self.assertEqual(rc, 2)

    def test_queue_command_add_list_skip_retry(self):
        rc, _ = self.run_cli("queue", "add", "--words", "甲,乙")
        self.assertEqual(rc, 0)
        rc, out = self.run_cli("queue", "list")
        self.assertEqual(out["counts"]["pending"], 2)
        self.run_cli("queue", "skip", "--words", "甲")
        self.assertEqual(self.queue().counts("xhs")["skipped"], 1)
        self.run_cli("queue", "retry", "--words", "甲")
        self.assertEqual(self.queue().counts("xhs")["pending"], 2)


if __name__ == "__main__":
    unittest.main()
