"""分层地域过滤：证据 → 状态 → 动作，数据流（标准化/入库/重打分/导出），补丁分组。

重点保护三件事：
  1. 默认关闭时行为与旧版完全一致（education_taizhou 不受影响）；
  2. 地域过滤只会把线索压低或排除，从不抬分；
  3. 没拿到上下文的旧数据不会因为「没线索」被误杀。
"""
import tempfile
import unittest
from pathlib import Path

import _helpers
from leadkit import normalize, pool, setup
from leadkit.geo import GeoJudge, province_of
from leadkit.paths import Workspace
from leadkit.profile import Profile, load_platform_map, load_profile
from leadkit.scoring import Scorer
from test_douyin import COMMENT_HEADERS, DouyinFixture, write_rows
from test_export import read_sheet

LOCAL_NOTE = "台州感统训练课体验 #台州#"
GENERIC_NOTE = "感统训练专注力训练到底是不是智商税"


def ctx(note="", nickname="", ip="", platform="xhs"):
    return {"note_text": note, "nickname": nickname, "ip_province": ip, "platform": platform}


class JudgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof = load_profile("gantong_taizhou")
        cls.judge = GeoJudge(cls.prof)

    def state(self, text, **kw):
        return self.judge.judge(text, ctx(**kw))

    # ---- 硬证据 ----
    def test_local_word_in_comment_is_confirmed(self):
        v = self.state("路桥有吗 3岁的宝宝", note=GENERIC_NOTE)
        self.assertEqual(v.state, "local_confirmed")
        self.assertTrue(any(s.startswith("H1") for s in v.signals))

    def test_ip_equal_to_target_city_is_confirmed(self):
        self.assertEqual(self.state("多少钱", ip="台州").state, "local_confirmed")

    # ---- 歧义词 ----
    def test_ambiguous_words_need_context(self):
        self.assertNotIn("H1", " ".join(self.state("天台上玩一下", note="x").signals))
        self.assertEqual(self.state("天台县有吗", note="x").state, "local_confirmed")
        self.assertNotIn("H1", " ".join(self.state("三门峡有吗", note="x").signals))

    def test_false_positive_phrases_are_dropped(self):
        self.assertNotEqual(self.state("黄岩岛事件", note="x").state, "local_confirmed")
        self.assertNotEqual(self.state("神仙居住的地方", note="x").state, "local_confirmed")

    # ---- 软证据组合 ----
    def test_note_local_alone_is_note_local(self):
        self.assertEqual(self.state("多少钱一节", note=LOCAL_NOTE).state, "note_local")

    def test_note_local_plus_nickname_is_likely(self):
        v = self.state("多少钱一节", note=LOCAL_NOTE, nickname="台州温岭小宝妈")
        self.assertEqual(v.state, "local_likely")

    def test_zhejiang_ip_plus_local_note_is_likely_but_alone_is_weak(self):
        self.assertEqual(self.state("多少钱", note=LOCAL_NOTE, ip="浙江").state, "local_likely")
        self.assertEqual(self.state("多少钱", note=GENERIC_NOTE, ip="浙江").state, "weak")

    def test_ip_label_is_normalized(self):
        self.assertEqual(province_of("IP属地：浙江省"), "浙江")
        self.assertEqual(self.state("多少钱", note=LOCAL_NOTE, ip="IP属地：浙江").state, "local_likely")

    def test_unknown_ip_label_is_ignored_not_treated_as_other_province(self):
        self.assertEqual(self.state("多少钱", note=LOCAL_NOTE, ip="未知").state, "note_local")

    # ---- 负证据 ----
    def test_other_province_ip_is_out_of_region(self):
        self.assertEqual(self.state("多少钱", note=LOCAL_NOTE, ip="北京").state, "out_of_region")

    def test_self_declared_other_city_is_out_of_region(self):
        self.assertEqual(self.state("我在深圳，多少钱一节", note=LOCAL_NOTE).state, "out_of_region")

    def test_merely_mentioning_another_city_is_not_out(self):
        """只是提到杭州、没有自述所在地，不能判外地（可能在对比或问有没有分店）。"""
        self.assertEqual(self.state("杭州也有吗", note=LOCAL_NOTE).state, "note_local")

    def test_local_word_plus_self_declared_elsewhere_is_conflict(self):
        self.assertEqual(self.state("我在深圳，台州有吗", note=LOCAL_NOTE).state, "conflict")

    def test_note_about_other_city_without_local_word_is_out(self):
        self.assertEqual(self.state("多少钱", note="杭州感统训练机构推荐").state, "out_of_region")

    def test_note_mentioning_both_is_not_out(self):
        self.assertNotEqual(self.state("多少钱", note="杭州台州感统机构汇总").state, "out_of_region")

    # ---- 无线索 ----
    def test_no_context_is_not_treated_as_unknown(self):
        """旧数据没有笔记文本也没有 IP：不能因为没线索就按「泛内容」处理。"""
        self.assertEqual(self.state("多少钱").state, "no_context")

    def test_context_without_any_local_clue_is_unknown(self):
        self.assertEqual(self.state("多少钱", note=GENERIC_NOTE).state, "unknown")

    # ---- 动作 ----
    def test_actions_follow_config_and_platform_override(self):
        self.assertEqual(self.state("多少钱", note=GENERIC_NOTE, platform="xhs").action, "keep")
        self.assertEqual(self.state("多少钱", note=GENERIC_NOTE, platform="dy").action, "exclude")
        self.assertEqual(self.state("多少钱", note=LOCAL_NOTE, platform="dy").action, "keep")
        self.assertEqual(self.state("多少钱", note=LOCAL_NOTE, ip="北京").action, "exclude")
        self.assertEqual(self.state("我在深圳，台州有吗", note=LOCAL_NOTE).action, "review")

    def test_invalid_action_config_fails_loudly(self):
        data = {**self.prof.data, "geo_filter": {**self.prof.data["geo_filter"], "actions": {"unknown": "delete"}}}
        with self.assertRaises(ValueError):
            GeoJudge(Profile(path=self.prof.path, data=data))

    def test_disabled_by_default(self):
        edu = load_profile("education_taizhou")
        v = GeoJudge(edu).judge("多少钱", ctx(note=GENERIC_NOTE, platform="dy"))
        self.assertEqual((v.state, v.action), ("", "keep"))


class ScorerIntegrationTest(unittest.TestCase):
    """用「多少一个月」这条明确咨询（本身会直接进 ready）检验过滤如何改判。"""

    @classmethod
    def setUpClass(cls):
        cls.scorer = Scorer(load_profile("gantong_taizhou"))

    def score(self, text="多少一个月", **kw):
        return self.scorer.score(text, "台州感统训练", ctx(**kw))

    def test_baseline_is_ready(self):
        self.assertEqual(self.score(note=LOCAL_NOTE)["status"], "ready")

    def test_out_of_region_is_excluded_with_reason(self):
        r = self.score("我在深圳，多少一个月", note=LOCAL_NOTE)
        self.assertEqual(r["status"], "excluded")
        self.assertEqual(r["exclude_reason"], "geo:out_of_region")
        self.assertEqual(r["geo_state"], "out_of_region")
        self.assertIn("N2:深圳", r["geo_signals"])

    def test_conflict_caps_ready_to_review(self):
        r = self.score("我在深圳，台州有吗，多少一个月", note=LOCAL_NOTE)
        self.assertEqual(r["status"], "needs_review")
        self.assertEqual(r["geo_state"], "conflict")

    def test_douyin_generic_video_is_excluded_but_xhs_is_kept(self):
        self.assertEqual(self.score(note=GENERIC_NOTE, platform="dy")["status"], "excluded")
        self.assertEqual(self.score(note=GENERIC_NOTE, platform="xhs")["status"], "ready")

    def test_old_rows_without_context_are_never_excluded(self):
        r = self.score(note="", platform="dy")
        self.assertEqual((r["geo_state"], r["status"]), ("no_context", "ready"))

    def test_no_ctx_argument_behaves_like_before(self):
        r = self.scorer.score("多少一个月", "台州感统训练")
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["geo_state"], "no_context")

    def test_filter_never_raises_score_or_status(self):
        """对一批文本，开着过滤的结果不得比关着更高。"""
        off_data = {**self.scorer.profile.data, "geo_filter": {"enabled": False}}
        off = Scorer(Profile(path=self.scorer.profile.path, data=off_data))
        rank = {"excluded": 0, "low_archive": 0, "needs_review": 1, "ready": 2}
        for t in ["多少一个月", "路桥有吗", "孩子坐不住怎么办", "我在深圳，多少一个月", "哈哈哈", "体验课怎么约"]:
            for note, plat in ((LOCAL_NOTE, "xhs"), (GENERIC_NOTE, "dy"), ("", "xhs")):
                a, b = self.score(t, note=note, platform=plat), off.score(t, "台州感统训练")
                self.assertLessEqual(rank[a["status"]], rank[b["status"]], (t, note, plat))
                self.assertEqual(a["intent_score"], b["intent_score"])

    def test_geo_tag_is_added_when_enabled(self):
        self.assertIn("地域:仅笔记本地", self.score(note=LOCAL_NOTE)["tags"])

    def test_education_profile_unchanged(self):
        edu = Scorer(load_profile("education_taizhou"))
        r = edu.score("多少钱一个月", "", ctx(note=GENERIC_NOTE, platform="dy"))
        self.assertEqual(r["geo_state"], "")
        self.assertNotIn("地域:", r["tags"])


class NormalizeContextTest(DouyinFixture):
    def test_note_text_joins_title_tags_and_desc(self):
        rec = self.records()[0]
        self.assertIn("台州感统训练课体验", rec["note_text"])
        self.assertEqual(rec["ip_province"], "")          # 没打补丁：没有这一列，按空处理

    def test_xhs_tags_are_included(self):
        cm = load_platform_map("xhs")["contents"]
        text = normalize._note_text({"title": "标题", "tag_list": "台州,黄岩", "desc": "描述" * 400}, cm)
        self.assertTrue(text.startswith("标题 台州,黄岩 描述"))
        self.assertLessEqual(len(text), len("标题 台州,黄岩 ") + normalize.NOTE_DESC_CHARS)

    def test_ip_province_is_read_when_patch_0005_column_exists(self):
        write_rows(self.batch / "search_comments_2026-10-02.csv", COMMENT_HEADERS + ["ip_province"], [
            {"comment_id": "c9", "aweme_id": "7600000000000000001", "content": "多少一个月", "nickname": "甲",
             "create_time": "1789181786", "ip_province": "浙江"}])
        self.assertEqual(self.records()[0]["ip_province"], "浙江")


class PoolFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name))
        self.prof = load_profile("gantong_taizhou")
        self.scorer = Scorer(self.prof)
        self.con = pool.connect(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def rec(self, cid="c1", text="多少一个月", platform="dy", note="", ip="", nick="甲"):
        return {"platform": platform, "comment_id": cid, "post_id": "p1", "post_url": "https://www.douyin.com/video/1",
                "post_title": "标题", "search_keyword": "台州感统训练", "text": text, "nickname": nick,
                "user_hash": "", "created_at": "2026-10-02 10:00", "like_count": "0", "parent_comment_id": "",
                "note_text": note, "ip_province": ip}

    def lead(self, cid="c1"):
        return self.con.execute("SELECT * FROM leads WHERE comment_id=?", (cid,)).fetchone()

    def test_ingest_stores_state_and_context(self):
        pool.ingest(self.con, self.prof, self.scorer, [self.rec(note=LOCAL_NOTE, ip="浙江")], "b1")
        row = self.lead()
        self.assertEqual((row["geo_state"], row["ip_province"], row["note_context"]), ("local_likely", "浙江", LOCAL_NOTE))
        self.assertEqual(row["status"], "ready")

    def test_ingest_excludes_generic_douyin_video(self):
        pool.ingest(self.con, self.prof, self.scorer, [self.rec(note=GENERIC_NOTE)], "b1")
        row = self.lead()
        self.assertEqual((row["status"], row["exclude_reason"], row["geo_state"]), ("excluded", "geo:unknown", "unknown"))

    def test_rescore_uses_stored_context_without_raw_comments(self):
        pool.ingest(self.con, self.prof, self.scorer, [self.rec(note=GENERIC_NOTE)], "b1")
        self.con.execute("DELETE FROM raw_comments")       # 原始评论 30 天后会被清掉，重打分不能依赖它
        self.con.commit()
        pool.rescore(self.con, self.prof, self.scorer, Path(self.tmp.name) / "t.sqlite")
        self.assertEqual(self.lead()["status"], "excluded")

    def test_reingest_backfills_context_for_old_rows_and_does_not_blank_it(self):
        pool.ingest(self.con, self.prof, self.scorer, [self.rec()], "b1")                      # 旧数据：没有上下文
        self.assertEqual((self.lead()["geo_state"], self.lead()["status"]), ("no_context", "ready"))
        pool.ingest(self.con, self.prof, self.scorer, [self.rec(note=GENERIC_NOTE)], "b2")     # 补采：带上了上下文
        self.assertEqual((self.lead()["geo_state"], self.lead()["status"]), ("unknown", "excluded"))
        self.assertEqual(self.lead()["note_context"], GENERIC_NOTE)
        pool.ingest(self.con, self.prof, self.scorer, [self.rec()], "b3")                      # 再来一次空上下文：不得清空
        self.assertEqual(self.lead()["note_context"], GENERIC_NOTE)

    def test_old_database_gets_new_columns(self):
        path = Path(self.tmp.name) / "old.sqlite"
        import sqlite3
        # 用真实建表语句去掉新增的 4 列，模拟升级前的旧库（含 parent_likely 等老列，只缺这次新增的）
        new_cols = ("geo_state", "geo_signals", "ip_province", "note_context")
        old_sql = "\n".join(l for l in pool.SCHEMA.read_text(encoding="utf-8").splitlines()
                            if not any(l.strip().startswith(c + " ") for c in new_cols))
        raw = sqlite3.connect(path)
        raw.executescript(old_sql)
        self.assertFalse({"geo_state"} & {r[1] for r in raw.execute("PRAGMA table_info(leads)")})
        raw.commit(), raw.close()
        con = pool.connect(path)
        have = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
        con.close()
        self.assertTrue({"geo_state", "geo_signals", "ip_province", "note_context"} <= have)

    def export_headers(self, prof):
        pool.export(self.con, self.ws, prof)
        return [c[0] for c in read_sheet(pool.xlsx_path(self.ws, prof))[0]]

    def test_excel_gets_geo_column_last_and_first_nine_unchanged(self):
        pool.ingest(self.con, self.prof, self.scorer, [self.rec(note=LOCAL_NOTE, ip="浙江")], "b1")
        headers = self.export_headers(self.prof)
        self.assertEqual(headers[:9], ["意向分", "问题类型", "原帖标题", "评论内容", "评论时间", "用户名", "搜索词", "笔记链接", "平台"])
        self.assertEqual(headers[9:], ["地域把握"])
        self.assertEqual(read_sheet(pool.xlsx_path(self.ws, self.prof))[1][9][0], "可能本地")

    def test_excel_has_exactly_nine_columns_when_switch_is_off(self):
        data = {**self.prof.data, "export": {"xlsx_geo_column": False}}
        self.assertEqual(len(self.export_headers(Profile(path=self.prof.path, data=data))), 9)


class PatchGroupTest(unittest.TestCase):
    def names(self, *flags):
        return [p.name[:4] for p in setup.patch_files(*flags)]

    def test_optional_patches_are_opt_in_and_independent(self):
        self.assertEqual(self.names(False, False), ["0001", "0002"])
        self.assertEqual(self.names(True, False), ["0001", "0002", "0003", "0004"])
        self.assertEqual(self.names(False, True), ["0001", "0002", "0005"])
        self.assertEqual(self.names(True, True), ["0001", "0002", "0003", "0004", "0005"])

    def test_patch_0005_stores_only_a_province_string(self):
        text = next(p for p in setup.patch_files(False, True) if p.name.startswith("0005")).read_text(encoding="utf-8")
        added = [l for l in text.splitlines() if l.startswith("+") and not l.startswith("+++")]
        self.assertTrue(any('"ip_province"' in l and "[:8]" in l for l in added))      # 截断到 8 个字
        # 不得把上游禁用的键当作存储字段（与上游隐私测试的 grep 规则一致：键后面跟冒号）
        import re
        self.assertFalse(re.search(r'"(ip_location|ip_label|ip_address|user_id|sec_uid|gender)"\s*:', " ".join(added)))

    def test_patch_0005_is_guarded_so_database_storage_cannot_crash(self):
        """回归：曾经无条件加键，sqlite/db 存储会因表里没有这一列而 TypeError。必须只对文件存储生效。"""
        text = next(p for p in setup.patch_files(False, True) if p.name.startswith("0005")).read_text(encoding="utf-8")
        added = [l[1:] for l in text.splitlines() if l.startswith("+") and not l.startswith("+++")]
        self.assertEqual(sum('config.SAVE_DATA_OPTION in ("csv", "json", "jsonl")' in l for l in added), 2)   # xhs、dy 各一处
        for i, l in enumerate(added):   # 每一处写 ip_province 的语句都必须是缩进两层（即在 if 里面）
            if "ip_province" in l and "=" in l and not l.lstrip().startswith("#"):
                self.assertTrue(l.startswith("        "), l)


if __name__ == "__main__":
    unittest.main()
