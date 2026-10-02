"""抖音（dy）链路：字段映射、时间单位、去重、导出，以及监控正则与补丁分组。

用合成数据（不含任何真实用户），表头与上游 MediaCrawler 抖音 CSV 保持一致。
真实联网采集（扫码、a_bogus 签名、风控）不在单元测试范围内，需要人工实跑。
"""
import csv
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import _helpers
from leadkit import normalize, pool, setup, xlsx
from leadkit.guard import CollectPlan, RunMonitor
from leadkit.paths import Workspace
from leadkit.profile import load_platform_map, load_profile
from leadkit.scoring import Scorer
from test_export import read_sheet

# 上游抖音 CSV 的真实表头（2026-09-30 的真实产物核对过）
CONTENT_HEADERS = ["aweme_id", "aweme_type", "title", "desc", "create_time", "creator_hash", "nickname",
                   "liked_count", "collected_count", "comment_count", "share_count", "last_modify_ts",
                   "aweme_url", "cover_url", "video_download_url", "music_download_url", "note_download_url",
                   "source_keyword"]
COMMENT_HEADERS = ["comment_id", "create_time", "aweme_id", "content", "creator_hash", "nickname",
                   "sub_comment_count", "like_count", "last_modify_ts", "parent_comment_id", "pictures"]


def write_rows(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # 上游带 BOM
        w = csv.DictWriter(f, fieldnames=headers, restval="")
        w.writeheader()
        w.writerows(rows)


class DouyinFixture(unittest.TestCase):
    """在 <批次>/douyin/csv/ 下放一对抖音 CSV——上游把平台目录叫 douyin，而不是项目里的 dy。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.batch = self.root / "raw" / "dy-batch" / "douyin" / "csv"
        write_rows(self.batch / "search_contents_2026-10-02.csv", CONTENT_HEADERS, [{
            "aweme_id": "7600000000000000001", "title": "台州感统训练课体验", "desc": "台州感统训练课体验",
            "aweme_url": "https://www.douyin.com/video/7600000000000000001", "source_keyword": "台州感统训练",
        }])
        base = {"aweme_id": "7600000000000000001", "creator_hash": "abc123", "sub_comment_count": "0",
                "like_count": "1", "parent_comment_id": "0"}
        write_rows(self.batch / "search_comments_2026-10-02.csv", COMMENT_HEADERS, [
            {**base, "comment_id": "cmt1", "create_time": "1789181786", "content": "多少一个月", "nickname": "小明妈妈", "last_modify_ts": "1"},
            {**base, "comment_id": "cmt1", "create_time": "1789181786", "content": "多少一个月", "nickname": "小明妈妈", "last_modify_ts": "2"},  # 重复
            {**base, "comment_id": "cmt2", "create_time": "1789181800", "content": "哈哈哈哈", "nickname": "路人甲", "last_modify_ts": "3"},
            {**base, "comment_id": "cmt3", "create_time": "1789181900", "content": "[鼓掌][鼓掌]", "nickname": "路人乙", "last_modify_ts": "4"},
        ])
        self.pmap = load_platform_map("dy")
        self.prof = load_profile("gantong_taizhou")

    def tearDown(self):
        self.tmp.cleanup()

    def records(self):
        return normalize.load_mediacrawler(self.root / "raw" / "dy-batch", self.pmap, self.prof.tz)


class DouyinNormalizeTest(DouyinFixture):
    def test_finds_csv_under_douyin_dir_not_dy_dir(self):
        self.assertEqual(len(normalize.find_csvs(self.root / "raw" / "dy-batch", "comments")), 1)

    def test_every_mapped_column_exists_in_upstream_headers(self):
        """dy.toml 里写的每个列名，都必须是上游真实表头里的列，否则字段会静默变空。"""
        for key, col in self.pmap["contents"].items():
            self.assertIn(col, CONTENT_HEADERS, f"contents.{key}={col}")
        for key, col in self.pmap["comments"].items():
            if key == "ip_province":   # 可选补丁 0005 才有的列，没打补丁时本来就不存在（按空处理）
                continue
            self.assertIn(col, COMMENT_HEADERS, f"comments.{key}={col}")

    def test_seconds_timestamp_is_not_mistaken_for_milliseconds(self):
        """抖音评论时间是秒；若当成毫秒会变成 1970 年。"""
        rec = next(r for r in self.records() if r["comment_id"] == "cmt1")
        self.assertTrue(rec["created_at"].startswith("2026-"), rec["created_at"])

    def test_title_url_nickname_and_dedupe(self):
        recs = self.records()
        self.assertEqual(sorted(r["comment_id"] for r in recs), ["cmt1", "cmt2", "cmt3"])   # 重复的只留一条
        r = next(r for r in recs if r["comment_id"] == "cmt1")
        self.assertEqual(r["post_title"], "台州感统训练课体验")
        self.assertEqual(r["post_url"], "https://www.douyin.com/video/7600000000000000001")
        self.assertEqual(r["search_keyword"], "台州感统训练")
        self.assertEqual(r["nickname"], "小明妈妈")
        self.assertEqual(r["platform"], "dy")


class DouyinPipelineTest(DouyinFixture):
    def test_ingest_and_excel(self):
        ws = Workspace(self.root / "ws").ensure()
        con = pool.connect(ws.db_path("t"))
        try:
            counts = pool.ingest(con, self.prof, Scorer(self.prof), self.records(), "b1")
            pool.export(con, ws, self.prof)
        finally:
            con.close()
        self.assertEqual((counts["new"], counts["ready"]), (3, 1))        # 「多少一个月」是明确咨询
        path = pool.xlsx_path(ws, self.prof)
        rows = read_sheet(path)
        self.assertEqual(len(rows), 2)                                    # 表头 + 唯一的 ready
        row = rows[1]
        self.assertEqual([row[0][0], row[3][0], row[5][0], row[8][0]], ["70", "多少一个月", "小明妈妈", "抖音"])
        self.assertEqual(row[2][0], "台州感统训练课体验")
        self.assertEqual(row[7][1], str(xlsx.S_RED_LINK))                 # ready 行：红底 + 蓝色链接
        with zipfile.ZipFile(path) as z:
            rels = ET.fromstring(z.read("xl/worksheets/_rels/sheet1.xml.rels"))
        self.assertEqual([r.get("Target") for r in rels], ["https://www.douyin.com/video/7600000000000000001"])

    def test_platform_label_covers_both_codes(self):
        self.assertEqual(pool.PLATFORM_LABEL["dy"], "抖音")
        self.assertEqual(pool.PLATFORM_LABEL["douyin"], "抖音")


class DouyinCollectGuardTest(unittest.TestCase):
    PLAN = dict(platform="dy", keywords=["台州感统训练"], notes_per_keyword=5, comments_per_note=10, concurrency=1,
                sleep_sec=3, login="qrcode", proxy=False, sub_comments=False, media=False)

    def monitor(self, **kw):
        return RunMonitor.from_platform(CollectPlan(**{**self.PLAN, **kw}), load_platform_map("dy"))

    # 与上游抖音日志文案一致（MediaCrawler/media_platform/douyin/core.py、store/douyin/__init__.py）
    KEY = "2026-10-02 INFO (core.py:138) - [DouYinCrawler.search] Current keyword: {}"
    DETAIL = "2026-10-02 INFO (__init__.py:118) - [store.douyin.update_douyin_aweme] douyin aweme id:{}, title:x"
    COMMENT = "2026-10-02 INFO (__init__.py:145) - [store.douyin.update_dy_aweme_comment] douyin aweme comment: {}, content: x"

    def test_overfetch_is_detected_with_douyin_log_lines(self):
        """监控只认日志文案；文案对不上就形同虚设，所以用上游真实的行格式测。"""
        m = self.monitor()
        m.feed(self.KEY.format("台州感统训练"))
        got = [m.feed(self.DETAIL.format(i)) for i in range(6)]
        self.assertTrue(all(g is None for g in got[:5]))
        self.assertIn("超过上限", got[5])

    def test_comment_counter_counts_douyin_comment_lines(self):
        m = self.monitor()
        for i in range(3):
            m.feed(self.COMMENT.format(i))
        self.assertEqual(m.comments_total, 3)

    def test_comment_text_cannot_trigger_block_abort(self):
        """评论原文里出现「验证码」不能误杀采集：存储回显行不参与风控判断。"""
        m = self.monitor()
        self.assertIsNone(m.feed("2026-10-02 WARNING (x.py:1) - [store.douyin.update_dy_aweme_comment] content: 求验证码"))


class RawIdentityPatchTest(unittest.TestCase):
    def names(self, flag):
        return [p.name for p in setup.patch_files(flag)]

    def test_nickname_patches_are_opt_in(self):
        """明文昵称补丁涉及隐私，默认不打；显式开启时才包含（xhs 与 dy 各一份）。"""
        self.assertFalse([n for n in self.names(False) if "raw" in n])
        on = self.names(True)
        self.assertIn("0003-xhs-raw-user-identity.patch", on)
        self.assertIn("0004-douyin-raw-comment-nickname.patch", on)

    def test_douyin_patch_is_minimal_and_does_not_leak_uid(self):
        text = (setup.PATCHES_DIR / "optional" / "0004-douyin-raw-comment-nickname.patch").read_text(encoding="utf-8")
        self.assertEqual([l for l in text.splitlines() if l.startswith("+++ ")], ["+++ b/store/douyin/__init__.py"])
        added = [l for l in text.splitlines() if l.startswith("+") and not l.startswith("+++")]
        self.assertTrue(any('"nickname": user_info.get("nickname")' in l for l in added))
        self.assertFalse(any('"user_id"' in l or '"uid"' in l for l in added))   # 只放开昵称，不存原始 uid


if __name__ == "__main__":
    unittest.main()
