"""最小 xlsx 写入器，只用标准库（zipfile + 手写 XML）。

为什么自己写：CSV 存不了颜色，而「高相关的行标红」必须落在 Excel 里才看得见；
但本项目的原则是零第三方依赖（装 openpyxl 会让 skill 在别人机器上多一步安装），
所以只实现这里需要的一小撮功能：一个工作表、表头样式、冻结首行、筛选、列宽、整行标红、可点击的蓝色超链接。

兼容性：Excel / WPS / Numbers / LibreOffice 都能直接打开。文本用 inlineStr 写入（不需要 sharedStrings），
并且不会被当成公式执行——评论里恰好以 `=` 开头也只是普通文本。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

from .logger import get_logger

LOG = get_logger("xlsx")

# 样式下标（对应下面 _STYLES 里 cellXfs 的顺序）
S_HEADER, S_TEXT, S_RED_TEXT, S_NUM, S_RED_NUM, S_LINK, S_RED_LINK = 1, 2, 3, 4, 5, 6, 7

# XML 1.0 不允许的控制字符；评论里偶尔会混进去，不剔除会让整个文件打不开
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_MAX_CELL = 32767  # Excel 单元格字符上限
_MAX_URL = 2000    # Excel 超链接上限约 2079 字符，超了会被拒，留点余量
_HTTP = re.compile(r"^https?://\S+$", re.I)  # 只给 http(s) 做成链接，避免 file:// 等本地/脚本协议


def _col(n: int) -> str:
    """0 → A, 25 → Z, 26 → AA。"""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _text(value: Any) -> str:
    return escape(_ILLEGAL.sub("", "" if value is None else str(value))[:_MAX_CELL])


# 配色沿用 Excel 内置「差」样式：浅红底 + 深红字，打印成黑白也能看出是被强调的行
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="4">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><sz val="11"/><color rgb="FF9C0006"/><name val="Calibri"/></font>
<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font>
</fonts>
<fills count="4">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFC7CE"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right><top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="3" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def _sanitize_sheet_name(name: str) -> str:
    """清洗工作表名称：剔除非法字符，截断到31字符。"""
    clean = _ILLEGAL.sub("", name)[:31]
    for ch in "[]:*?/\\":
        clean = clean.replace(ch, "")
    return clean or "Sheet"


def _build_sheet_xml(headers: Sequence[str], rows: Sequence[Sequence[Any]], *,
                     red_rows: set[int] | frozenset[int] = frozenset(),
                     widths: Sequence[float] | None = None,
                     number_cols: set[int] | frozenset[int] = frozenset(),
                     link_cols: set[int] | frozenset[int] = frozenset(),
                     tab_selected: bool = True) -> tuple[str, str | None]:
    """构建单个工作表的XML内容和可能的hyperlinks rels内容。

    返回 (sheet_xml, sheet_rels_or_none)。
    """
    n_cols = len(headers)
    widths_list = list(widths or [])
    widths_list += [14] * (n_cols - len(widths_list))

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
           f'<dimension ref="A1:{_col(n_cols - 1)}{len(rows) + 1}"/>',
           '<sheetViews><sheetView workbookViewId="0"' + (' tabSelected="1"' if tab_selected else '') + '>'
           '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
           '<sheetFormatPr defaultRowHeight="15"/>',
           "<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                              for i, w in enumerate(widths_list)) + "</cols>",
           "<sheetData>"]

    # 表头
    out.append('<row r="1" ht="22" customHeight="1">' + "".join(
        f'<c r="{_col(i)}1" s="{S_HEADER}" t="inlineStr"><is><t xml:space="preserve">{_text(h)}</t></is></c>'
        for i, h in enumerate(headers)) + "</row>")

    links: list[tuple[str, str]] = []
    for r, row in enumerate(rows):
        red = r in red_rows
        cells = []
        for c, value in enumerate(row):
            ref = f"{_col(c)}{r + 2}"
            if c in number_cols and isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}" s="{S_RED_NUM if red else S_NUM}"><v>{value}</v></c>')
            elif c in link_cols and isinstance(value, str) and len(value) <= _MAX_URL and _HTTP.match(value.strip()):
                links.append((ref, value.strip()))
                cells.append(f'<c r="{ref}" s="{S_RED_LINK if red else S_LINK}" t="inlineStr">'
                             f'<is><t xml:space="preserve">{_text(value)}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}" s="{S_RED_TEXT if red else S_TEXT}" t="inlineStr">'
                             f'<is><t xml:space="preserve">{_text(value)}</t></is></c>')
        out.append(f'<row r="{r + 2}">' + "".join(cells) + "</row>")

    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A1:{_col(n_cols - 1)}{max(len(rows) + 1, 2)}"/>')

    sheet_rels = None
    if links:
        out.append("<hyperlinks>" + "".join(
            f'<hyperlink ref="{ref}" r:id="rId{i}"/>' for i, (ref, _) in enumerate(links, 1)) + "</hyperlinks>")
        sheet_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      + "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                                'Target="%s" TargetMode="External"/>' % (i, escape(url, {'"': "&quot;"}))
                                for i, (_, url) in enumerate(links, 1)) + "</Relationships>")
    out.append("</worksheet>")
    return "".join(out), sheet_rels


def write_xlsx(path: Path, headers: Sequence[str], rows: Sequence[Sequence[Any]], *,
               red_rows: set[int] | frozenset[int] = frozenset(), widths: Sequence[float] | None = None,
               number_cols: set[int] | frozenset[int] = frozenset(), link_cols: set[int] | frozenset[int] = frozenset(),
               sheet_name: str = "线索池") -> Path:
    """写一个单工作表的 xlsx。

    red_rows：需要整行标红的**数据行下标**（0 起，不含表头）。
    number_cols：按数字写入的列下标（可排序、可筛选范围）；其余按文本。
    link_cols：值是网址的列下标，写成蓝色下划线的真超链接（点击直接打开）；
               不是 http(s) 网址或过长的值退回普通文本，标红行里链接仍是蓝字、红底。
    """
    name = _sanitize_sheet_name(sheet_name)
    sheet_xml, sheet_rels = _build_sheet_xml(
        headers, rows, red_rows=red_rows, widths=widths,
        number_cols=number_cols, link_cols=link_cols, tab_selected=True)

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{escape(name) or "Sheet1"}" sheetId="1" r:id="rId1"/></sheets></workbook>')

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if sheet_rels:
            z.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)
    tmp.replace(path)
    LOG.info("写入 xlsx %s（%d 行，标红 %d 行）", path, len(rows), len(red_rows))
    return path


from dataclasses import dataclass


@dataclass
class SheetSpec:
    """多工作表写入时每个工作表的规格。"""
    name: str
    headers: Sequence[str]
    rows: Sequence[Sequence[Any]]
    red_rows: set[int] | frozenset[int] = frozenset()
    widths: Sequence[float] | None = None
    number_cols: set[int] | frozenset[int] = frozenset()
    link_cols: set[int] | frozenset[int] = frozenset()


def write_xlsx_multi(path: Path, sheets: Sequence[SheetSpec]) -> Path:
    """写一个多工作表的 xlsx。

    sheets：按顺序的工作表规格列表，第一个工作表默认选中。
    每个工作表都支持独立的表头、数据行、标红行、列宽、数字列、链接列。
    冻结首行、自动筛选、样式等功能在每个工作表上都启用。
    """
    if not sheets:
        raise ValueError("至少需要一个工作表")

    n_sheets = len(sheets)
    sheet_data: list[tuple[str, str, str | None]] = []  # (name, xml, rels_or_none)

    for i, spec in enumerate(sheets):
        name = _sanitize_sheet_name(spec.name)
        sheet_xml, sheet_rels = _build_sheet_xml(
            spec.headers, spec.rows, red_rows=spec.red_rows, widths=spec.widths,
            number_cols=spec.number_cols, link_cols=spec.link_cols, tab_selected=(i == 0))
        sheet_data.append((name, sheet_xml, sheet_rels))

    # 构建 Content_Types：每个工作表都需要一个 Override
    ct_parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                '<Default Extension="xml" ContentType="application/xml"/>',
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    for i in range(1, n_sheets + 1):
        ct_parts.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    ct_parts.append('<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>')
    ct_parts.append('</Types>')
    content_types = "".join(ct_parts)

    # 构建 workbook.xml：包含所有工作表
    wb_sheets = "".join(f'<sheet name="{escape(name) or f"Sheet{i+1}"}" sheetId="{i+1}" r:id="rId{i+1}"/>'
                        for i, (name, _, _) in enumerate(sheet_data))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{wb_sheets}</sheets></workbook>')

    # 构建 workbook.xml.rels：每个工作表一个 relationship，最后是 styles
    wb_rels_parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(1, n_sheets + 1):
        wb_rels_parts.append(f'<Relationship Id="rId{i}" '
                             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             f'Target="worksheets/sheet{i}.xml"/>')
    wb_rels_parts.append(f'<Relationship Id="rId{n_sheets + 1}" '
                         'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                         'Target="styles.xml"/>')
    wb_rels_parts.append('</Relationships>')
    wb_rels = "".join(wb_rels_parts)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_, sheet_xml, sheet_rels) in enumerate(sheet_data, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml)
            if sheet_rels:
                z.writestr(f"xl/worksheets/_rels/sheet{i}.xml.rels", sheet_rels)
    tmp.replace(path)

    total_rows = sum(len(spec.rows) for spec in sheets)
    total_red = sum(len(spec.red_rows) for spec in sheets)
    LOG.info("写入多工作表 xlsx %s（%d 个工作表，共 %d 行，标红 %d 行）",
             path, n_sheets, total_rows, total_red)
    return path
