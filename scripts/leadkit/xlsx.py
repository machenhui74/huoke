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
    n_cols = len(headers)
    widths = list(widths or [])
    widths += [14] * (n_cols - len(widths))
    name = _ILLEGAL.sub("", sheet_name)[:31]
    for ch in "[]:*?/\\":  # Excel 工作表名的非法字符
        name = name.replace(ch, "")

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
           f'<dimension ref="A1:{_col(n_cols - 1)}{len(rows) + 1}"/>',
           '<sheetViews><sheetView workbookViewId="0" tabSelected="1">'
           '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
           '<sheetFormatPr defaultRowHeight="15"/>',
           "<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>",
           "<sheetData>"]

    # 表头
    out.append('<row r="1" ht="22" customHeight="1">' + "".join(
        f'<c r="{_col(i)}1" s="{S_HEADER}" t="inlineStr"><is><t xml:space="preserve">{_text(h)}</t></is></c>'
        for i, h in enumerate(headers)) + "</row>")

    links: list[tuple[str, str]] = []  # (单元格引用, 网址)，稍后生成 hyperlinks 与 rels
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
    if links:  # hyperlinks 必须紧跟 autoFilter 之后；目标网址放在 sheet 的 rels 里（外部链接）
        out.append("<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rId{i}"/>' for i, (ref, _) in enumerate(links, 1)) + "</hyperlinks>")
        sheet_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      + "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                                'Target="%s" TargetMode="External"/>' % (i, escape(url, {'"': "&quot;"}))
                                for i, (_, url) in enumerate(links, 1)) + "</Relationships>")
    out.append("</worksheet>")

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{escape(name) or "Sheet1"}" sheetId="1" r:id="rId1"/></sheets></workbook>')

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")  # 先写临时文件再替换，避免写一半崩溃留下坏文件
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", "".join(out))
        if sheet_rels:
            z.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)
    tmp.replace(path)
    LOG.info("写入 xlsx %s（%d 行，标红 %d 行，链接 %d 个）", path, len(rows), len(red_rows), len(links))
    return path
