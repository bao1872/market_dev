"""app.core.exchange 包的自测入口。

运行方式：python -m app.core.exchange
"""
from app.core.exchange import Exchange, clear_exchange_cache, get_exchange

if __name__ == "__main__":
    import inspect

    # 1. 验证 Exchange 为 ABC
    assert inspect.isabstract(Exchange), "Exchange 应为抽象基类"
    print("Exchange 是抽象基类 ✓")

    # 2. 验证抽象方法列表
    abstract_methods = {
        "get_daily_bars", "get_weekly_bars", "get_monthly_bars",
        "get_15min_bars", "get_60min_bars", "get_minute_bars",
        "get_xdxr_info", "get_stock_list",
    }
    actual_abstract = set(Exchange.__abstractmethods__)
    assert actual_abstract == abstract_methods, \
        f"抽象方法不匹配: {actual_abstract} != {abstract_methods}"
    print(f"抽象方法: {sorted(actual_abstract)} ✓")

    # 3. 验证工厂函数签名
    sig = inspect.signature(get_exchange)
    params = list(sig.parameters.keys())
    assert params == ["market"], f"get_exchange 参数不匹配: {params}"
    print(f"get_exchange params={params} ✓")

    # 4. 验证默认配置为 pytdx
    from app.config import get_settings

    settings = get_settings()
    assert settings.bars_data_source == "pytdx", \
        f"默认 bars_data_source 应为 pytdx，实际 {settings.bars_data_source}"
    print(f"默认 bars_data_source={settings.bars_data_source} ✓")

    # 5. 验证 PytdxAdapter 实现 Exchange 接口
    from app.core.pytdx_adapter import PytdxAdapter

    assert issubclass(PytdxAdapter, Exchange), "PytdxAdapter 应继承 Exchange"
    print("PytdxAdapter 继承 Exchange ✓")

    # 6. 验证未知数据源抛 ValueError
    clear_exchange_cache()
    object.__setattr__(settings, "bars_data_source", "unknown")
    try:
        try:
            get_exchange("A")
            raise AssertionError("未知数据源应抛 ValueError")
        except ValueError as e:
            assert "未知数据源" in str(e), f"错误信息不匹配: {e}"
            print(f"未知数据源抛 ValueError: {e} ✓")
    finally:
        object.__setattr__(settings, "bars_data_source", "pytdx")
        clear_exchange_cache()

    # 7. 验证缓存机制
    clear_exchange_cache()
    ex1 = get_exchange("A")
    ex2 = get_exchange("A")
    assert ex1 is ex2, "相同 market 应返回同一实例（缓存）"
    print(f"缓存机制: ex1 is ex2 = {ex1 is ex2} ✓")
    clear_exchange_cache()

    print("\n所有自测通过 ✓（未进行 DB/网络测试）")
