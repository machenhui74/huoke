"""标准化 → 入库 → 重打分 → 导出 的端到端（全部在临时目录里，不碰网络和真实数据）。"""
import csv
import tempfile
import unittest
from pathlib import Path

import _helpers
from leadkit import normalize, pool
from leadkit.paths import Workspace
from leadkit.profile import load_platform_map
from leadkit.scoring import Scorer


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name)).ensure()
        self.prof = _helpers.profile()
        self.scorer = Scorer(self.prof)
        batch = self.ws.raw / "b1" / "xhs" / "csv"
        write_csv(batch / "search_contents_2026-10-01.csv", ["note_id", "title", "note_url", "source_keyword"],
                  [["n1", "托管班测评", "https://x.com/explore/n1?xsec_token=SECRET&x=1", "椒江托管班"]])
        write_csv(batch / "search_comments_2026-10-01.csv",
                  ["comment_id", "create_time", "note_id", "content", "creator_hash", "nickname", "like_count", "parent_comment_id"],
                  [["c1", "1759300000000", "n1", "3周岁娃什么价位", "h1", "张**", "1", ""],
                   ["c2", "1759300000000", "n1", "欢迎咨询，私信我", "h2", "机构", "0", ""],
                   ["c1", "1759300000000", "n1", "重复的 c1", "h1", "张**", "1", ""]])  # 重复 ID 应被丢弃
        self.records = normalize.load_mediacrawler(self.ws.raw / "b1", load_platform_map("xhs"), self.prof.tz, "xhs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_strips_token_and_dedupes(self):
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.records[0]["post_url"], "https://x.com/explore/n1")
        self.assertEqual(self.records[0]["search_keyword"], "椒江托管班")
        self.assertRegex(self.records[0]["created_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_ingest_is_idempotent_and_exports_are_clean(self):
        db = self.ws.db_path("t")
        con = pool.connect(db)
        first = pool.ingest(con, self.prof, self.scorer, self.records, "b1")
        self.assertEqual((first["new"], first["ready"], first["excluded"]), (2, 1, 1))
        second = pool.ingest(con, self.prof, self.scorer, self.records, "b1")
        self.assertEqual((second["new"], second["updated"]), (0, 2))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 2)

        public, internal, n = pool.export(con, self.ws, self.prof)
        self.assertEqual(n, 1)
        header = public.read_text(encoding="utf-8-sig").splitlines()[0]
        for bad in pool.FORBIDDEN_EXPORT_COLS:
            self.assertNotIn(bad, header)
        self.assertIn("用户名", internal.read_text(encoding="utf-8-sig").splitlines()[0])
        # 库里任何 URL 都不能带令牌
        self.assertEqual(con.execute("SELECT COUNT(*) FROM leads WHERE post_url LIKE '%token%'").fetchone()[0], 0)
        con.close()

    def test_locked_lead_survives_rescore(self):
        """人工点过头的线索，重打分只补判断列，不动分数和触达状态。"""
        db = self.ws.db_path("t")
        con = pool.connect(db)
        pool.ingest(con, self.prof, self.scorer, self.records, "b1")
        pool.mark(con, self.prof, "xhs", "c1", "approved", "已抽检")
        con.execute("UPDATE leads SET intent_score=99 WHERE comment_id='c1'")
        con.commit()
        counts = pool.rescore(con, self.prof, self.scorer, db)
        self.assertEqual(counts["locked"], 1)
        row = con.execute("SELECT intent_score, reach_status FROM leads WHERE comment_id='c1'").fetchone()
        self.assertEqual((row["intent_score"], row["reach_status"]), (99, "approved"))
        con.close()

    def test_generic_csv(self):
        src = Path(self.tmp.name) / "any.csv"
        write_csv(src, ["评论", "词"], [["怎么收费", "托管班"], ["不错", "托管班"]])
        recs = normalize.load_generic_csv(src, text_col="评论", keyword_col="词")
        self.assertEqual([r["comment_id"] for r in recs], ["row1", "row2"])
        with self.assertRaises(KeyError):
            normalize.load_generic_csv(src, text_col="不存在的列")


if __name__ == "__main__":
    unittest.main()
