"""拼音首字母生成工具 - advice.md 第六节：拼音搜索。

提供：
- compute_pinyin_initials(name): 将股票名称转为小写拼音首字母串

设计说明：
- 主数据同步时调用一次并落库（instruments.pinyin_initials），搜索时直接读字段，避免实时转拼音
- 中文字符取拼音首字母（pypinyin lazy_pinyin + Style.FIRST_LETTER）
- 非中文字符（字母/数字）保留原样并转小写；空格与符号（*、-、括号等）剔除
- 返回值均为小写，搜索时 keyword 也转小写后做前缀匹配

示例：
    '东睦股份' -> 'dmgf'
    '贵州茅台' -> 'gzmt'
    '*ST康美'  -> 'stkm'   （符号 '*' 剔除，字母 ST 转小写，康美取首字母）
    '宁德时代' -> 'ndsd'

用法：
    from app.services.pinyin_util import compute_pinyin_initials
    initials = compute_pinyin_initials("东睦股份")  # 'dmgf'
"""
from __future__ import annotations

from pypinyin import Style, lazy_pinyin

# 非中文字符中允许保留的类别：字母与数字；空格/标点/符号一律剔除
_KEEP_ASCII_ALNUM = str.isascii


def compute_pinyin_initials(name: str | None) -> str | None:
    """将股票名称转为小写拼音首字母串。

    Args:
        name: 股票名称，如 '东睦股份'

    Returns:
        小写拼音首字母串，如 'dmgf'；输入为空或无有效字符时返回 None
    """
    if not name or not name.strip():
        return None

    # lazy_pinyin + FIRST_LETTER：中文字符返回首字母（小写），非中文字符原样返回
    pieces = lazy_pinyin(name, style=Style.FIRST_LETTER, errors="default")

    chars: list[str] = []
    for piece in pieces:
        for ch in piece:
            # 仅保留 ASCII 字母与数字；剔除空格、*、-、括号等符号
            if _KEEP_ASCII_ALNUM(ch) and ch.isalnum():
                chars.append(ch.lower())
    result = "".join(chars)
    return result or None


if __name__ == "__main__":
    # 自测入口：验证核心用例（不写库表）
    cases = [
        ("东睦股份", "dmgf"),
        ("贵州茅台", "gzmt"),
        ("隆基绿能", "ljln"),
        ("平安银行", "payh"),
        ("招商银行", "zsyh"),
        ("宁德时代", "ndsd"),
        ("*ST康美", "stkm"),
        ("", None),
        (None, None),
        ("  ", None),
    ]
    print("=== pinyin_util 自测 ===")
    all_ok = True
    for name, expected in cases:
        got = compute_pinyin_initials(name)
        ok = got == expected
        all_ok = all_ok and ok
        print(f"{name!r:12} -> {got!r:10} expected={expected!r:10} {'OK' if ok else 'FAIL'}")
    print("全部通过" if all_ok else "存在失败用例")
    print("=== 自测结束 ===")
