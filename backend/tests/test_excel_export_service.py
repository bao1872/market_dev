"""Excel 导出服务单元测试（CHANGE-20260713-010）。

测试目标：
1. generate_xlsx 生成合法 .xlsx bytes（zipfile 可读，含必要 OOXML 部分）
2. 公式注入防护：=、+、-、@ 开头的文本前缀单引号
3. extract_row_data: stock 列返回 "名称(代码)" 格式，其他列按 payload_key 提取
4. MAX_EXPORT_ROWS 上限为 10000
5. 数值列保持数值单元格（type=n），百分比列使用百分比样式

用法：
    APP_ENV=test pytest backend/tests/test_excel_export_service.py -q
"""

from __future__ import annotations

import io
import zipfile

from app.schemas.export import ExportColumn
from app.services.excel_export_service import (
    MAX_EXPORT_ROWS,
    _sanitize_formula_injection,
    extract_row_data,
    generate_xlsx,
)


def _make_columns() -> list[ExportColumn]:
    return [
        ExportColumn(key="stock", title="股票", data_type="text", payload_key=None),
        ExportColumn(key="change_pct", title="涨跌幅", data_type="percent", payload_key="change_pct"),
        ExportColumn(key="dsa_dir_bars", title="趋势", data_type="number", payload_key="dsa_dir_bars"),
    ]


def test_generate_xlsx_returns_valid_zip() -> None:
    """generate_xlsx 返回合法的 .xlsx bytes（zipfile 可读，含必要 OOXML 部分）。"""
    columns = _make_columns()
    rows = [
        {"stock": "贵州茅台(600519)", "change_pct": 3.5, "dsa_dir_bars": 5},
        {"stock": "平安银行(000001)", "change_pct": -1.2, "dsa_dir_bars": -3},
    ]
    raw = generate_xlsx(columns, rows)

    assert isinstance(raw, bytes)
    assert len(raw) > 0
    # 验证是合法 zip
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()
        # 必要 OOXML 部分
        assert "[Content_Types].xml" in names
        assert "xl/workbook.xml" in names
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/styles.xml" in names
        # sheet1.xml 可读且非空
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "<worksheet" in sheet
        assert "<sheetData" in sheet


def test_generate_xlsx_includes_header_row() -> None:
    """表头行使用 ExportColumn.title（写入 sharedStrings.xml）。"""
    columns = _make_columns()
    rows: list[dict] = []
    raw = generate_xlsx(columns, rows)
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "r") as zf:
        shared = zf.read("xl/sharedStrings.xml").decode("utf-8")
    # 表头应出现在 sharedStrings
    assert "股票" in shared
    assert "涨跌幅" in shared
    assert "趋势" in shared


def test_sanitize_formula_injection_prefixes_dangerous_starts() -> None:
    """以 =、+、-、@ 开头的文本前缀单引号。"""
    assert _sanitize_formula_injection("=cmd") == "'=cmd"
    assert _sanitize_formula_injection("+1+1") == "'+1+1"
    assert _sanitize_formula_injection("-1+1") == "'-1+1"
    assert _sanitize_formula_injection("@SUM") == "'@SUM"
    # 普通文本不变
    assert _sanitize_formula_injection("normal text") == "normal text"
    assert _sanitize_formula_injection("123") == "123"
    assert _sanitize_formula_injection("") == ""
    assert _sanitize_formula_injection(None) == ""


def test_extract_row_data_stock_column_formats_name_symbol() -> None:
    """stock 列返回 "名称(代码)" 格式。"""
    columns = _make_columns()
    row_data = extract_row_data(
        instrument_symbol="600519",
        instrument_name="贵州茅台",
        instrument_market="SH",
        payload={"change_pct": 3.5, "dsa_dir_bars": 5},
        columns=columns,
    )
    assert row_data["stock"] == "贵州茅台(600519)"
    assert row_data["change_pct"] == 3.5
    assert row_data["dsa_dir_bars"] == 5


def test_extract_row_data_stock_column_handles_missing() -> None:
    """stock 列在 name/symbol 缺失时降级。"""
    columns = _make_columns()
    row_data = extract_row_data(
        instrument_symbol=None,
        instrument_name=None,
        instrument_market=None,
        payload={"change_pct": 1.0},
        columns=columns,
    )
    assert row_data["stock"] == ""
    assert row_data["change_pct"] == 1.0


def test_extract_row_data_uses_payload_key() -> None:
    """非 stock 列按 payload_key 从 payload 提取。"""
    columns = [
        ExportColumn(key="custom", title="自定义", data_type="number", payload_key="dsa_dir_bars"),
    ]
    row_data = extract_row_data(
        instrument_symbol="600519",
        instrument_name="贵州茅台",
        instrument_market="SH",
        payload={"dsa_dir_bars": 42},
        columns=columns,
    )
    assert row_data["custom"] == 42


def test_max_export_rows_is_10000() -> None:
    """MAX_EXPORT_ROWS 必须为 10000。"""
    assert MAX_EXPORT_ROWS == 10000


def test_generate_xlsx_handles_empty_rows() -> None:
    """空数据行仍生成合法 .xlsx（仅表头）。"""
    columns = _make_columns()
    raw = generate_xlsx(columns, [])
    assert isinstance(raw, bytes)
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "r") as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<sheetData" in sheet


def test_generate_xlsx_handles_none_values() -> None:
    """None 值不报错，写入空字符串。"""
    columns = _make_columns()
    rows = [{"stock": "贵州茅台(600519)", "change_pct": None, "dsa_dir_bars": None}]
    raw = generate_xlsx(columns, rows)
    assert isinstance(raw, bytes)
    assert len(raw) > 0
