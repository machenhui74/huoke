"""人工池导出：排序（高相关在前）、标红、Excel 文件本身是否合法、脱敏。"""
import contextlib
import csv
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import _helpers
from leadkit import cli, normalize, pool, xlsx
from leadkit.paths import Workspace
from leadkit.scoring import Scorer

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_sheet(path: Path, sheet_num: int = 1) -> list[list[tuple[str, str]]]:
    """读回 xlsx 指定工作表：每行是 [(单元格文本或数字, 样式下标), ...]，不依赖第三方库。"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet_num}.xml"))
    rows = []
    for row in root.findall(".//m:sheetData/m:row", NS):
        cells = []
        for c in row.findall("m:c", NS):
            t = c.find("m:is/m:t", NS)
            v = c.find("m:v", NS)
            cells.append(((t.text or "") if t is not None else (v.text if v is not None else ""), c.get("s")))
        rows.append(cells)
    return rows


def get_sheet_names(path: Path) -> list[str]:
    """从 workbook.xml 读取所有工作表名称。"""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
    return [s.get("name") for s in wb.findall(".//m:sheet", NS)]


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


class XlsxMultiSheetTest(unittest.TestCase):
    """多工作表写入测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "multi.xlsx"

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_sheets_with_correct_names(self):
        """两个工作表，名称正确。"""
        sheets = [
            xlsx.SheetSpec("线索池", ["甲", "乙"], [["a", 1], ["b", 2]]),
            xlsx.SheetSpec("AI智能排除", ["甲", "乙", "丙"], [["x", 3, "reason1"]]),
        ]
        xlsx.write_xlsx_multi(self.path, sheets)
        names = get_sheet_names(self.path)
        self.assertEqual(names, ["线索池", "AI智能排除"])

    def test_each_sheet_has_correct_data(self):
        """每个工作表有各自的表头和数据。"""
        sheets = [
            xlsx.SheetSpec("Sheet1", ["A", "B"], [["r1c1", "r1c2"]]),
            xlsx.SheetSpec("Sheet2", ["X", "Y", "Z"], [["s2r1", "s2r2", "s2r3"], ["s2r4", "s2r5", "s2r6"]]),
        ]
        xlsx.write_xlsx_multi(self.path, sheets)
        sheet1 = read_sheet(self.path, 1)
        sheet2 = read_sheet(self.path, 2)
        self.assertEqual([c[0] for c in sheet1[0]], ["A", "B"])
        self.assertEqual([c[0] for c in sheet1[1]], ["r1c1", "r1c2"])
        self.assertEqual([c[0] for c in sheet2[0]], ["X", "Y", "Z"])
        self.assertEqual(len(sheet2), 3)  # 表头 + 2 行数据

    def test_all_parts_well_formed_xml(self):
        """多工作表文件的所有 XML 部件都是合法的。"""
        sheets = [
            xlsx.SheetSpec("线索池", ["甲"], [["a"]]),
            xlsx.SheetSpec("AI智能排除", ["乙"], [["b"]]),
        ]
        xlsx.write_xlsx_multi(self.path, sheets)
        with zipfile.ZipFile(self.path) as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                ET.fromstring(z.read(name))

    def test_red_rows_and_links_work_per_sheet(self):
        """标红和链接在每个工作表上独立工作。"""
        url = "https://example.com/test"
        sheets = [
            xlsx.SheetSpec("S1", ["A", "B"], [["text", url], ["more", url]],
                           red_rows={0}, link_cols={1}),
            xlsx.SheetSpec("S2", ["X"], [["plain"]]),
        ]
        xlsx.write_xlsx_multi(self.path, sheets)
        sheet1 = read_sheet(self.path, 1)
        # 第一行数据应该是红色，第二行不是
        self.assertEqual(sheet1[1][0][1], str(xlsx.S_RED_TEXT))
        self.assertEqual(sheet1[2][0][1], str(xlsx.S_TEXT))
        # 链接应该存在于 sheet1 的 rels 中
        with zipfile.ZipFile(self.path) as z:
            self.assertIn("xl/worksheets/_rels/sheet1.xml.rels", z.namelist())
            self.assertNotIn("xl/worksheets/_rels/sheet2.xml.rels", z.namelist())

    def test_sheet_name_sanitized(self):
        """工作表名称中的非法字符被清除。"""
        sheets = [xlsx.SheetSpec("Test[]:*?/\\Name", ["A"], [[1]])]
        xlsx.write_xlsx_multi(self.path, sheets)
        names = get_sheet_names(self.path)
        self.assertFalse(set(names[0]) & set("[]:*?/\\"))

    def test_first_sheet_is_selected(self):
        """第一个工作表默认选中。"""
        sheets = [
            xlsx.SheetSpec("S1", ["A"], [[1]]),
            xlsx.SheetSpec("S2", ["B"], [[2]]),
        ]
        xlsx.write_xlsx_multi(self.path, sheets)
        with zipfile.ZipFile(self.path) as z:
            sheet1 = z.read("xl/worksheets/sheet1.xml").decode()
            sheet2 = z.read("xl/worksheets/sheet2.xml").decode()
        self.assertIn('tabSelected="1"', sheet1)
        self.assertNotIn('tabSelected="1"', sheet2)


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

    def test_filenames_are_stamped_with_today(self):
        """正式项目按天留档：同一天重导覆盖当天，不覆盖其他日期。"""
        public, internal, _, _ = self.export()
        day = pool.export_day(self.prof)
        self.assertEqual(public.name, f"{self.prof.name}_pool_{day}.csv")
        self.assertEqual(internal.name, f"{self.prof.name}_pool_with_nickname_{day}.csv")
        self.assertEqual(pool.xlsx_path(self.ws, self.prof).name, f"{self.prof.name}_pool_{day}.xlsx")
        self.assertEqual(pool.xlsx_path_public(self.ws, self.prof).name, f"{self.prof.name}_pool_{day}.xlsx")
        self.assertTrue(pool.xlsx_path_public(self.ws, self.prof).is_file())

    def test_header_is_exactly_the_requested_columns(self):
        """最终表头是用户指定的 9 列，顺序固定；不能混进状态、强度、ID 等别的列。"""
        _, _, _, rows = self.export()
        self.assertEqual([c[0] for c in rows[0]], self.HEADERS)

    def test_csvs_are_chinese_only_and_mirror_the_excel_columns(self):
        """用户只看中文：两份 CSV 的表头、取值都不得出现英文（ready / needs_review / comment_id 等）。"""
        self.force({"评论1": ("ready", 80), "评论2": ("needs_review", 60), "评论3": ("ready", 75), "评论4": ("needs_review", 50)})
        public, internal, _, _ = self.export()
        for path in (public, internal):
            text = path.read_text(encoding="utf-8-sig")
            header = text.splitlines()[0].split(",")
            self.assertEqual(header[:5], ["意向分", "问题类型", "原帖标题", "评论内容", "评论时间"])
            self.assertFalse(any(c.isascii() and c.isalpha() for h in header for c in h), header)   # 表头里没有任何英文字母
            for raw in ("ready", "needs_review", "pending_review", "excluded"):
                self.assertNotIn(raw, text)
            self.assertIn("高相关", text)
            self.assertIn("待复核", text)
        pub_header = public.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
        int_header = internal.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
        self.assertNotIn("用户名", pub_header)                       # 可外传版仍然不含用户名
        self.assertEqual(int_header[5], "用户名")                   # 内部版位置与 Excel 一致
        with internal.open(encoding="utf-8-sig") as f:
            self.assertIn("用户1", [r["用户名"] for r in csv.DictReader(f)])

    def test_ready_always_before_needs_review_even_with_lower_score(self):
        """只按分数排的话，99 分的待复核会插到 70 分的 ready 前面——那就不是「高相关在前」了。"""
        self.force({"评论1": ("ready", 70), "评论2": ("needs_review", 99), "评论3": ("ready", 85), "评论4": ("needs_review", 60)})
        public, _, n, rows = self.export()
        self.assertEqual(n, 4)
        order = [r[3][0] for r in rows[1:]]                      # 第 4 列 = 评论内容
        self.assertEqual(order, ["评论3", "评论1", "评论2", "评论4"])
        self.assertEqual([r[0][0] for r in rows[1:]], ["85", "70", "99", "60"])   # 意向分是数字单元格
        with public.open(encoding="utf-8-sig") as f:
            csv_order = [r["评论内容"] for r in csv.DictReader(f)]
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

    def test_excel_has_two_sheets_with_excluded_on_second(self):
        """Excel 有两个工作表：线索池 和 AI智能排除。"""
        self.force({"评论1": ("ready", 80), "评论2": ("needs_review", 60),
                    "评论3": ("excluded", 40), "评论4": ("excluded", 30)})
        # 设置排除原因
        self.con.execute("UPDATE leads SET exclude_reason='低意向归档' WHERE comment_text='评论3'")
        self.con.execute("UPDATE leads SET exclude_reason='地域不符' WHERE comment_text='评论4'")
        self.con.commit()
        self.export()
        xlsx_file = pool.xlsx_path(self.ws, self.prof)
        names = get_sheet_names(xlsx_file)
        self.assertEqual(names, ["线索池", "AI智能排除"])

    def test_excluded_sheet_contains_excluded_leads_with_reason(self):
        """AI智能排除工作表包含排除的线索及其排除原因。"""
        # 设置所有4条评论的状态
        self.force({"评论1": ("ready", 80), "评论2": ("excluded", 50),
                    "评论3": ("excluded", 30), "评论4": ("needs_review", 60)})
        self.con.execute("UPDATE leads SET exclude_reason='low_archive score=50' WHERE comment_text='评论2'")
        self.con.execute("UPDATE leads SET exclude_reason='地域不符' WHERE comment_text='评论3'")
        self.con.commit()
        self.export()
        xlsx_file = pool.xlsx_path(self.ws, self.prof)
        # Sheet1 只有人工池（ready + needs_review）
        sheet1 = read_sheet(xlsx_file, 1)
        self.assertEqual(len(sheet1) - 1, 2)  # 1 ready + 1 needs_review
        # Sheet2 有排除的线索
        sheet2 = read_sheet(xlsx_file, 2)
        self.assertEqual(len(sheet2) - 1, 2)  # 2 条 excluded
        # 按意向分降序排列
        self.assertEqual(sheet2[1][3][0], "评论2")  # 分数 50
        self.assertEqual(sheet2[2][3][0], "评论3")  # 分数 30
        # 最后一列是排除原因
        header = [c[0] for c in sheet2[0]]
        self.assertIn("排除原因", header)
        reason_idx = header.index("排除原因")
        self.assertEqual(sheet2[1][reason_idx][0], "low_archive score=50")
        self.assertEqual(sheet2[2][reason_idx][0], "地域不符")

    def test_excluded_sheet_has_no_red_rows(self):
        """AI智能排除工作表不标红任何行。"""
        self.force({"评论1": ("excluded", 90), "评论2": ("excluded", 80)})
        self.export()
        sheet2 = read_sheet(pool.xlsx_path(self.ws, self.prof), 2)
        red_styles = {str(xlsx.S_RED_TEXT), str(xlsx.S_RED_NUM), str(xlsx.S_RED_LINK)}
        for row in sheet2[1:]:  # 跳过表头
            for _, style in row:
                self.assertNotIn(style, red_styles)

    def test_public_xlsx_exists_and_no_nickname(self):
        """公开版 xlsx 存在于 exports/，且不含用户名列。"""
        self.force({"评论1": ("ready", 80), "评论2": ("excluded", 40)})
        self.export()
        public_xlsx = pool.xlsx_path_public(self.ws, self.prof)
        self.assertTrue(public_xlsx.exists())
        self.assertEqual(public_xlsx.parent, self.ws.exports)
        # 检查两个工作表都没有用户名列
        for sheet_num in (1, 2):
            sheet = read_sheet(public_xlsx, sheet_num)
            header = [c[0] for c in sheet[0]]
            self.assertNotIn("用户名", header)
            self.assertNotIn("昵称", header)

    def test_public_xlsx_has_same_two_sheets(self):
        """公开版 xlsx 也有两个工作表，名称相同。"""
        self.force({"评论1": ("ready", 80), "评论2": ("excluded", 40)})
        self.export()
        internal_names = get_sheet_names(pool.xlsx_path(self.ws, self.prof))
        public_names = get_sheet_names(pool.xlsx_path_public(self.ws, self.prof))
        self.assertEqual(internal_names, public_names)
        self.assertEqual(public_names, ["线索池", "AI智能排除"])

    def test_csv_still_only_pool_not_excluded(self):
        """CSV 仍然只导出人工池（ready + needs_review），不包含 excluded。"""
        self.force({"评论1": ("ready", 80), "评论2": ("needs_review", 60),
                    "评论3": ("excluded", 40), "评论4": ("excluded", 30)})
        public, internal, n, _ = self.export()
        self.assertEqual(n, 2)  # 只有 ready + needs_review
        for path in (public, internal):
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("评论1", text)
            self.assertIn("评论2", text)
            self.assertNotIn("评论3", text)
            self.assertNotIn("评论4", text)


class ScoreCommandTest(unittest.TestCase):
    """leadctl score（不入库的快速打分）输出的 CSV 也只能是中文。"""

    def test_scored_csv_has_chinese_headers_and_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.csv"
            src.write_text("评论\n多少钱一个月\n哈哈哈\n", encoding="utf-8-sig")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(["--workdir", tmp, "-q", "score", "--input", str(src), "--text-col", "评论"])
            self.assertEqual(rc, 0)
            files = list((Path(tmp) / "exports").glob("in.scored.*.csv"))
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0].name, r"^in\.scored\.\d{4}-\d{2}-\d{2}\.csv$")
            text = files[0].read_text(encoding="utf-8-sig")
            header = text.splitlines()[0].split(",")
            self.assertEqual(header[:3], ["意向分", "问题类型", "评论内容"])
            self.assertFalse(any(c.isascii() and c.isalpha() for h in header for c in h), header)
            for raw in ("ready", "needs_review", "low_archive", "excluded"):
                self.assertNotIn(raw, text)


if __name__ == "__main__":
    unittest.main()
