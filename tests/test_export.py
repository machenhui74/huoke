"""人工池导出：排序（高相关在前）、标红、Excel 文件本身是否合法、脱敏。"""
import csv
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import _helpers
from leadkit import normalize, pool, xlsx
from leadkit.paths import Workspace
from leadkit.scoring import Scorer

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_sheet(path: Path) -> list[list[tuple[str, str]]]:
    """读回 xlsx：每行是 [(单元格文本或数字, 样式下标), ...]，不依赖第三方库。"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in root.findall(".//m:sheetData/m:row", NS):
        cells = []
        for c in row.findall("m:c", NS):
            t = c.find("m:is/m:t", NS)
            v = c.find("m:v", NS)
            cells.append(((t.text or "") if t is not None else (v.text if v is not None else ""), c.get("s")))
        rows.append(cells)
    return rows


class XlsxWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.xlsx"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, **kw):
        xlsx.write_xlsx(self.path, ["甲", "乙"], rows, **kw)
        return read_sheet(self.path)

    def test_every_part_is_well_formed_xml(self):
        self.write([["a", 1]])
        with zipfile.ZipFile(self.path) as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                ET.fromstring(z.read(name))  # 任何一个部件坏了，Excel 都会报「文件已损坏」

    def test_red_rows_get_red_styles_and_others_do_not(self):
        rows = self.write([["a", 1], ["b", 2], ["c", 3]], red_rows={0, 2}, number_cols={1})
        styles = [[s for _, s in r] for r in rows]
        self.assertEqual(styles[0], [str(xlsx.S_HEADER)] * 2)
        self.assertEqual(styles[1], [str(xlsx.S_RED_TEXT), str(xlsx.S_RED_NUM)])
        self.assertEqual(styles[2], [str(xlsx.S_TEXT), str(xlsx.S_NUM)])
        self.assertEqual(styles[3], [str(xlsx.S_RED_TEXT), str(xlsx.S_RED_NUM)])

    def test_hostile_text_is_escaped_and_never_a_formula(self):
        """评论是不可信输入：XML 特殊字符要转义，控制字符要剔除，以 = 开头的也只是文本。"""
        nasty = ['a<b & c>d "q"', "含控制字符\x01\x08结束", '=HYPERLINK("http://evil","x")', "+1+1", "line1\nline2"]
        rows = self.write([[t, 0] for t in nasty])
        got = [r[0][0] for r in rows[1:]]
        self.assertEqual(got[0], nasty[0])
        self.assertEqual(got[1], "含控制字符结束")
        self.assertEqual(got[2], nasty[2])
        self.assertEqual(got[4], "line1\nline2")
        with zipfile.ZipFile(self.path) as z:
            self.assertNotIn(b"<f>", z.read("xl/worksheets/sheet1.xml"))   # 没有任何公式单元格

    def test_numbers_are_numeric_cells(self):
        self.write([["a", 90]], number_cols={1})
        with zipfile.ZipFile(self.path) as z:
            xml = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn('<c r="B2" s="4"><v>90</v></c>', xml)

    def test_header_frozen_filtered_and_sheet_name_sanitized(self):
        xlsx.write_xlsx(self.path, ["甲", "乙"], [["a", 1]], sheet_name="线索[池]:*?/\\" + "x" * 40)
        with zipfile.ZipFile(self.path) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
            wb = ET.fromstring(z.read("xl/workbook.xml"))
        self.assertIn('state="frozen"', sheet)
        self.assertIn("<autoFilter", sheet)
        name = wb.find(".//m:sheet", NS).get("name")
        self.assertLessEqual(len(name), 31)
        self.assertFalse(set(name) & set("[]:*?/\\"))

    def test_urls_become_real_blue_hyperlinks(self):
        rows = self.write([["https://www.xiaohongshu.com/explore/abc?x=1&y=2", 1], ["javascript:alert(1)", 2], ["", 3]],
                          link_cols={0}, red_rows={0})
        self.assertEqual([r[0][1] for r in rows[1:]], [str(xlsx.S_RED_LINK), str(xlsx.S_TEXT), str(xlsx.S_TEXT)])
        with zipfile.ZipFile(self.path) as z:
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            rels = ET.fromstring(z.read("xl/worksheets/_rels/sheet1.xml.rels"))
        links = sheet.findall(".//m:hyperlinks/m:hyperlink", NS)
        self.assertEqual([l.get("ref") for l in links], ["A2"])        # 只有 http(s) 网址才成链接
        target = rels[0].get("Target")
        self.assertEqual(target, "https://www.xiaohongshu.com/explore/abc?x=1&y=2")   # & 转义后读回应一致
        self.assertEqual(rels[0].get("TargetMode"), "External")

    def test_no_links_means_no_rels_part(self):
        self.write([["普通文本", 1]], link_cols={0})
        with zipfile.ZipFile(self.path) as z:
            self.assertNotIn("xl/worksheets/_rels/sheet1.xml.rels", z.namelist())

    def test_empty_table_still_valid(self):
        rows = self.write([])
        self.assertEqual(len(rows), 1)  # 只有表头


class ExportOrderTest(unittest.TestCase):
    HEADERS = ["意向分", "问题类型", "原帖标题", "评论内容", "评论时间", "用户名", "搜索词", "笔记链接", "平台"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name)).ensure()
        self.prof = _helpers.profile()
        src = Path(self.tmp.name) / "in.csv"
        with src.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["评论", "词", "昵称"])
            for i in range(1, 5):
                w.writerow([f"评论{i}", "椒江感统", f"用户{i}"])
        recs = normalize.load_generic_csv(src, text_col="评论", keyword_col="词", nickname_col="昵称")
        self.con = pool.connect(self.ws.db_path("t"))
        pool.ingest(self.con, self.prof, Scorer(self.prof), recs, "b1")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def force(self, spec: dict[str, tuple[str, int]]):
        """绕过打分，直接设定每条的状态和分数，让排序测试不依赖词表。"""
        for text, (status, score) in spec.items():
            self.con.execute("UPDATE leads SET status=?, intent_score=? WHERE comment_text=?", (status, score, text))
        self.con.commit()

    def export(self):
        public, internal, n = pool.export(self.con, self.ws, self.prof)
        return public, internal, n, read_sheet(pool.xlsx_path(self.ws, self.prof))

    def test_header_is_exactly_the_requested_columns(self):
        """最终表头是用户指定的 9 列，顺序固定；不能混进状态、强度、ID 等别的列。"""
        _, _, _, rows = self.export()
        self.assertEqual([c[0] for c in rows[0]], self.HEADERS)

    def test_ready_always_before_needs_review_even_with_lower_score(self):
        """只按分数排的话，99 分的待复核会插到 70 分的 ready 前面——那就不是「高相关在前」了。"""
        self.force({"评论1": ("ready", 70), "评论2": ("needs_review", 99), "评论3": ("ready", 85), "评论4": ("needs_review", 60)})
        public, _, n, rows = self.export()
        self.assertEqual(n, 4)
        order = [r[3][0] for r in rows[1:]]                      # 第 4 列 = 评论内容
        self.assertEqual(order, ["评论3", "评论1", "评论2", "评论4"])
        self.assertEqual([r[0][0] for r in rows[1:]], ["85", "70", "99", "60"])   # 意向分是数字单元格
        with public.open(encoding="utf-8-sig") as f:
            csv_order = [r["comment_text"] for r in csv.DictReader(f)]
        self.assertEqual(csv_order, order)                       # CSV 顺序与 Excel 一致

    def test_only_ready_rows_are_red_by_default(self):
        self.force({"评论1": ("ready", 70), "评论2": ("needs_review", 99), "评论3": ("ready", 85), "评论4": ("needs_review", 60)})
        _, _, _, rows = self.export()
        red = [r[3][0] for r in rows[1:] if r[3][1] == str(xlsx.S_RED_TEXT)]
        plain = [r[3][0] for r in rows[1:] if r[3][1] == str(xlsx.S_TEXT)]
        self.assertEqual((red, plain), (["评论3", "评论1"], ["评论2", "评论4"]))
        # 整行都标红，不是只标某一格
        red_styles = {str(xlsx.S_RED_TEXT), str(xlsx.S_RED_NUM), str(xlsx.S_RED_LINK)}
        self.assertTrue(all(s in red_styles for _, s in rows[1]))

    def test_highlight_is_configurable_in_profile(self):
        self.force({"评论1": ("ready", 70), "评论2": ("needs_review", 99), "评论3": ("needs_review", 85), "评论4": ("excluded", 1)})
        self.prof.data["export"] = {"highlight_status": ["ready", "needs_review"]}
        _, _, _, rows = self.export()
        self.assertEqual(len(rows) - 1, 3)                       # excluded 不进表
        self.assertTrue(all(r[3][1] == str(xlsx.S_RED_TEXT) for r in rows[1:]))

    def test_username_is_shown_not_hidden(self):
        """用户要求用户名原样输出；Excel 放 internal/，脱敏 CSV 仍然没有昵称。"""
        self.force({f"评论{i}": ("ready", 80) for i in range(1, 5)})
        public, _, _, rows = self.export()
        self.assertEqual(sorted(r[5][0] for r in rows[1:]), ["用户1", "用户2", "用户3", "用户4"])
        self.assertEqual(pool.xlsx_path(self.ws, self.prof).parent, self.ws.internal)
        for bad in pool.FORBIDDEN_EXPORT_COLS:
            self.assertNotIn(bad, public.read_text(encoding="utf-8-sig").splitlines()[0])

    def test_title_time_platform_and_blue_link(self):
        """原帖标题来自 raw_comments；评论时间去掉时区；平台显示中文；笔记链接是蓝色可点的真链接。"""
        self.force({"评论1": ("ready", 80)})
        url = "https://www.xiaohongshu.com/explore/abc123"
        self.con.execute("UPDATE leads SET platform='xhs', post_url=?, commented_at='2026-09-29 11:16:26+0800' "
                         "WHERE comment_text='评论1'", (url,))
        self.con.execute("UPDATE raw_comments SET platform='xhs', note_title='三岁娃感统训练哪里好' WHERE content='评论1'")
        self.con.commit()
        _, _, _, rows = self.export()
        row = next(r for r in rows[1:] if r[3][0] == "评论1")
        self.assertEqual(row[2][0], "三岁娃感统训练哪里好")
        self.assertEqual(row[4][0], "2026-09-29 11:16:26")
        self.assertEqual(row[8][0], "小红书")
        self.assertEqual((row[7][0], row[7][1]), (url, str(xlsx.S_RED_LINK)))   # ready 行：红底上的蓝色链接
        with zipfile.ZipFile(pool.xlsx_path(self.ws, self.prof)) as z:
            rels = ET.fromstring(z.read("xl/worksheets/_rels/sheet1.xml.rels"))
        self.assertEqual([r.get("Target") for r in rels], [url])

    def test_missing_raw_comment_leaves_title_empty_not_drop_lead(self):
        self.force({"评论1": ("ready", 80)})
        self.con.execute("DELETE FROM raw_comments")             # 模拟原始评论过期被清理
        self.con.commit()
        _, _, n, rows = self.export()
        self.assertEqual(n, 1)
        self.assertEqual(rows[1][2][0], "")

    def test_empty_pool_exports_headers_only(self):
        self.con.execute("DELETE FROM leads")
        self.con.commit()
        _, _, n, rows = self.export()
        self.assertEqual((n, len(rows)), (0, 1))


if __name__ == "__main__":
    unittest.main()
