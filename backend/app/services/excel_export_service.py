"""Excel 导出服务 — 使用标准库生成 .xlsx (OOXML)。

CHANGE-20260713-010: 列表导出 Excel

不依赖 openpyxl/xlsxwriter，使用 zipfile + xml.etree.ElementTree 生成最小可用的 .xlsx。
公式注入防护：以 =、+、-、@ 开头的文本值前缀单引号，Excel 不会解释为公式。
"""

from __future__ import annotations

import io
import os
import zipfile
from typing import Any

from app.schemas.export import ExportColumn
from app.services.first_pyramid_flatten import FP_QUERY_FIELD_SPECS

MAX_EXPORT_ROWS = 10000

# [S2-A-C1] 导出列白名单（服务决定 source，不接受客户端 payload_key 控制读取路径）
_BASE_EXPORT_KEYS = {
    "symbol", "name", "market", "is_watchlisted",
    "latest_price", "change_pct",
    "industry", "concepts",
    "dsa_state", "structure_state",
    "chip_status", "stock",
}

MAX_EXPORT_COLUMNS = 256
MAX_EXPORT_TITLE_LEN = 256


def validate_export_columns(columns: list[ExportColumn]) -> None:
    """[S2-A-C1] 执行前 fail-fast 校验客户端 visible_columns（不可信输入）。

    校验：非空、列数上限、key 不重复、禁止 action/select/注入风格 key、
    仅允许白名单基础列或 fp_ 字段、data_type 合法、title 长度上限。
    payload_key 不被信任来控制服务器数据读取路径。
    """
    if not columns:
        raise ValueError("visible_columns 不能为空")
    if len(columns) > MAX_EXPORT_COLUMNS:
        raise ValueError(f"visible_columns 列数超过上限 {MAX_EXPORT_COLUMNS}")
    seen: set[str] = set()
    for col in columns:
        key = col.key
        if not isinstance(key, str) or not key:
            raise ValueError("column key 非法")
        if key in seen:
            raise ValueError(f"重复列 key: {key}")
        seen.add(key)
        # 禁止 action/select 类危险列；禁止 SQL 注入风格 key
        if key in ("action", "select") or key.startswith("action") or ";" in key or " " in key:
            raise ValueError(f"禁止的列 key: {key}")
        # 白名单：基础列 或 fp_ 字段
        if key not in _BASE_EXPORT_KEYS and not key.startswith("fp_"):
            raise ValueError(f"未知列 key: {key}")
        if key.startswith("fp_") and key not in FP_QUERY_FIELD_SPECS:
            raise ValueError(f"未知 fp 列: {key}")
        if col.data_type not in ("text", "number", "percent"):
            raise ValueError(f"非法 data_type: {col.data_type}")
        title = col.title
        if title is None or len(str(title)) > MAX_EXPORT_TITLE_LEN:
            raise ValueError("title 长度超限")


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


def extract_market_row_data(market_row: Any, columns: list[ExportColumn]) -> dict[str, Any]:
    """从 /market/stocks canonical 行（MarketStockRow）提取导出单元格。

    与旧 export 的 extract_row_data（读旧 DSA payload，已 deprecate 为 None）不同：
    - fp_* 列从 ``market_row.first_pyramid``（canonical 第一金字塔 99 键）读取，与列表页同源；
    - 基础列（stock/change_pct/price/industry/...）从 MarketStockRow 字段读取；
    - 不读取旧 payload，避免 fp 列恒为空。

    Args:
        market_row: MarketStockRow（Pydantic 模型，含 first_pyramid 等字段）
        columns: 导出列定义（ExportColumn）

    Returns:
        dict: column.key -> 单元格值（Null 保留为 None，交由 generate_xlsx 处理）
    """
    row_data: dict[str, Any] = {}
    fp = market_row.first_pyramid or {}
    for col in columns:
        k = col.key
        if k == "stock":
            name = market_row.name or ""
            symbol = market_row.symbol or ""
            row_data[k] = f"{name}({symbol})" if name and symbol else name or symbol
        elif k.startswith("fp_"):
            row_data[k] = fp.get(k)
        else:
            value = getattr(market_row, k, None)
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            row_data[k] = value
    return row_data


def _build_writer_content_types() -> str:
    """写入器专用 [Content_Types].xml（使用 inlineStr，不引用 sharedStrings）。"""
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
        "</Types>"
    )


def _build_writer_workbook_xml() -> str:
    """写入器专用 xl/workbook.xml（sheet 名称贴合导出语义）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="行情导出" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )


def _build_writer_workbook_rels() -> str:
    """写入器专用 xl/_rels/workbook.xml.rels（仅 worksheet + styles，无 sharedStrings）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


class MarketXlsxWriter:
    """[S2-A-C1] 低内存增量 XLSX 写入器（market export 专用）。

    合同（硬约束）：
    - 不持有完整 data_rows 列表；
    - 不持有整张 worksheet XML string；
    - 不持有 BytesIO 最终文件；
    - 每行以 inlineStr 增量追加到临时 worksheet 文件（避免 sharedStrings 全量驻留）；
    - 最终 zip 写入临时 xlsx 文件，由调用方以 StreamingResponse 分块发送；
    - 第一行为列标题（表头）。
    """

    def __init__(self, columns: list[ExportColumn], tmp_dir: str) -> None:
        self.columns = columns
        self._row_index = 0
        self._ws_path = os.path.join(tmp_dir, "sheet1.xml")
        self._file = open(self._ws_path, "w", encoding="utf-8", newline="")
        self._write_header()

    def _write_header(self) -> None:
        self._file.write(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        )
        self._file.write("<cols>")
        for i in range(len(self.columns)):
            self._file.write(f'<col min="{i+1}" max="{i+1}" width="18" customWidth="1"/>')
        self._file.write("</cols>")
        self._file.write("<sheetData>")
        # 表头行（row 1）
        self._row_index = 1
        self._file.write('<row r="1">')
        for ci, col in enumerate(self.columns, 1):
            ref = f"{_col_letter(ci - 1)}1"
            text = "" if col.title is None else str(col.title)
            text = _sanitize_formula_injection(text)
            self._file.write(
                f'<c r="{ref}" t="inlineStr"><is>'
                f'<t xml:space="preserve">{_escape_xml(text)}</t></is></c>'
            )
        self._file.write("</row>")

    def add_rows(self, rows: list[dict]) -> None:
        """增量追加一批行（在 worker 线程中调用，避免阻塞 event loop）。"""
        for row in rows:
            self._row_index += 1
            self._file.write(f'<row r="{self._row_index}">')
            for ci, col in enumerate(self.columns, 1):
                ref = f"{_col_letter(ci - 1)}{self._row_index}"
                val = row.get(col.key)
                if col.data_type in ("number", "percent") and isinstance(val, (int, float)):
                    style = ' s="1"' if col.data_type == "percent" else ""
                    self._file.write(f'<c r="{ref}"{style} t="n"><v>{val}</v></c>')
                else:
                    text = "" if val is None else str(val)
                    text = _sanitize_formula_injection(text)
                    self._file.write(
                        f'<c r="{ref}" t="inlineStr"><is>'
                        f'<t xml:space="preserve">{_escape_xml(text)}</t></is></c>'
                    )
            self._file.write("</row>")

    def finalize(self) -> None:
        self._file.write("</sheetData></worksheet>")
        self._file.close()

    def build_zip(self, out_path: str) -> None:
        """将临时 worksheet 文件 zip 为最终 xlsx（在 worker 线程中调用）。

        使用 inlineStr，不写 sharedStrings（避免全量驻留 + 与 rels/content-types 一致）。
        """
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", _build_writer_content_types())
            zf.writestr("_rels/.rels", _build_rels())
            zf.writestr("xl/workbook.xml", _build_writer_workbook_xml())
            zf.writestr("xl/_rels/workbook.xml.rels", _build_writer_workbook_rels())
            zf.writestr("xl/styles.xml", _build_styles_xml())
            zf.write(self._ws_path, "xl/worksheets/sheet1.xml")
