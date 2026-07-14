"""Excel 导出服务 — 使用标准库生成 .xlsx (OOXML)。

CHANGE-20260713-010: 列表导出 Excel

不依赖 openpyxl/xlsxwriter，使用 zipfile + xml.etree.ElementTree 生成最小可用的 .xlsx。
公式注入防护：以 =、+、-、@ 开头的文本值前缀单引号，Excel 不会解释为公式。
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from app.schemas.export import ExportColumn

MAX_EXPORT_ROWS = 10000


def _escape_xml(text: str) -> str:
    """转义 XML 特殊字符。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _sanitize_formula_injection(value: Any) -> str:
    """公式注入防护：以 =、+、-、@ 开头的文本前缀单引号。"""
    s = str(value) if value is not None else ""
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def _format_cell_value(value: Any, data_type: str) -> tuple[str, str]:
    """格式化单元格值，返回 (cell_type, formatted_value)。

    Returns:
        (type, value): type 为 "n" (数字) 或 "s" (字符串，进 sharedStrings) 或 "inlineStr"
    """
    if value is None:
        return ("s", "")

    if data_type == "number":
        if isinstance(value, (int, float)):
            return ("n", str(value))
        try:
            return ("n", str(float(value)))
        except (ValueError, TypeError):
            return ("s", _sanitize_formula_injection(value))

    if data_type == "percent":
        if isinstance(value, (int, float)):
            # 百分比格式：值保持原样（如 0.05 或 5.1），Excel 数字格式 applied via styles
            return ("n", str(value))
        try:
            return ("n", str(float(value)))
        except (ValueError, TypeError):
            return ("s", _sanitize_formula_injection(value))

    # text
    return ("s", _sanitize_formula_injection(value))


def _build_shared_strings(strings: list[str]) -> str:
    """构建 xl/sharedStrings.xml。"""
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    parts.append(
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">'
    )
    for s in strings:
        parts.append(f'<si><t xml:space="preserve">{_escape_xml(s)}</t></si>')
    parts.append("</sst>")
    return "".join(parts)


def _build_workbook_xml() -> str:
    """构建 xl/workbook.xml。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="DSA筛选结果" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )


def _build_workbook_rels() -> str:
    """构建 xl/_rels/workbook.xml.rels。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    )


def _build_styles_xml() -> str:
    """构建 xl/styles.xml（含百分比格式）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.00%"/></numFmts>'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _build_content_types() -> str:
    """构建 [Content_Types].xml。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )


def _build_rels() -> str:
    """构建 _rels/.rels。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _col_letter(idx: int) -> str:
    """将列索引（0-based）转换为 Excel 列字母（A, B, ..., Z, AA, ...）。"""
    result = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def build_sheet_xml_with_styles(
    rows: list[list[tuple[str, str, int]]],
    shared_strings: list[str],
    string_to_index: dict[str, int],
) -> str:
    """构建 xl/worksheets/sheet1.xml（带 style index）。

    Args:
        rows: 每行是 (type, value, style_index) 元组列表
        shared_strings: shared strings 列表
        string_to_index: 字符串到索引的映射
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<cols>",
    ]
    # 列宽由第一行确定
    if rows:
        for i in range(len(rows[0])):
            parts.append(f'<col min="{i+1}" max="{i+1}" width="18" customWidth="1"/>')
    parts.append("</cols>")
    parts.append("<sheetData>")
    for row_idx, row in enumerate(rows, 1):
        parts.append(f'<row r="{row_idx}">')
        for col_idx, (cell_type, value, style_idx) in enumerate(row):
            ref = f"{_col_letter(col_idx)}{row_idx}"
            style_attr = f' s="{style_idx}"' if style_idx > 0 else ""
            if cell_type == "n":
                parts.append(f'<c r="{ref}" t="n"{style_attr}><v>{value}</v></c>')
            else:
                if value not in string_to_index:
                    string_to_index[value] = len(shared_strings)
                    shared_strings.append(value)
                idx = string_to_index[value]
                parts.append(f'<c r="{ref}" t="s"{style_attr}><v>{idx}</v></c>')
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def generate_xlsx(
    columns: list[ExportColumn],
    data_rows: list[dict],
) -> bytes:
    """生成 .xlsx 文件 bytes。

    Args:
        columns: 导出列定义
        data_rows: 数据行列表，每行是 dict，包含 column.key → value 映射
                   特殊 key "stock" 应已在外部解析为 "股票名称(代码)" 字符串

    Returns:
        .xlsx 文件 bytes
    """
    shared_strings: list[str] = []
    string_to_index: dict[str, int] = {}

    # 构建所有行（含表头）
    all_rows: list[list[tuple[str, str, int]]] = []

    # 表头行（style=0，普通文本）
    header_row: list[tuple[str, str, int]] = []
    for col in columns:
        header_row.append(("s", col.title, 0))
    all_rows.append(header_row)

    # 数据行
    for row_data in data_rows:
        row: list[tuple[str, str, int]] = []
        for col in columns:
            value = row_data.get(col.key)
            cell_type, formatted = _format_cell_value(value, col.data_type)
            # style: 0=普通, 1=百分比格式
            style_idx = 1 if col.data_type == "percent" and cell_type == "n" else 0
            row.append((cell_type, formatted, style_idx))
        all_rows.append(row)

    # 构建 sheet XML
    sheet_xml = build_sheet_xml_with_styles(all_rows, shared_strings, string_to_index)

    # 构建 sharedStrings XML
    shared_strings_xml = _build_shared_strings(shared_strings)

    # 写入 zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _build_content_types())
        zf.writestr("_rels/.rels", _build_rels())
        zf.writestr("xl/workbook.xml", _build_workbook_xml())
        zf.writestr("xl/_rels/workbook.xml.rels", _build_workbook_rels())
        zf.writestr("xl/styles.xml", _build_styles_xml())
        zf.writestr("xl/sharedStrings.xml", shared_strings_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return buf.getvalue()


def extract_row_data(
    instrument_symbol: str | None,
    instrument_name: str | None,
    instrument_market: str | None,
    payload: dict | None,
    columns: list[ExportColumn],
    latest_change_pct: float | None = None,
    latest_change_trade_date: Any = None,
) -> dict:
    """从结果行提取导出数据。

    对于 key="stock" 的列，返回 "名称(代码)" 格式。
    对于 key="change_pct" 的列，使用 latest_change_pct（CHANGE-20260714-001：从 bars_daily 计算，与 DSA run payload 分离）。
    其他列按 payload_key 从 payload 提取值。
    """
    row_data: dict[str, Any] = {}
    for col in columns:
        if col.key == "stock":
            name = instrument_name or ""
            symbol = instrument_symbol or ""
            row_data[col.key] = f"{name}({symbol})" if name and symbol else name or symbol
        elif col.key == "change_pct":
            # CHANGE-20260714-001: 涨跌幅从 bars_daily 最新两根日线计算，不读 payload
            row_data[col.key] = latest_change_pct
        elif col.payload_key:
            row_data[col.key] = payload.get(col.payload_key) if payload else None
        else:
            row_data[col.key] = payload.get(col.key) if payload else None
    return row_data
