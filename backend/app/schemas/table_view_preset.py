"""用户表格视图配置 Pydantic schemas - /me/table-view-presets API 请求/响应。

提供：
- TableViewPresetConfig: config 字段 schema（白名单 + 类型校验）
- TableViewPresetCreate: 创建请求 schema
- TableViewPresetPatch: 更新请求 schema（部分字段可选）
- TableViewPresetResponse: 响应 schema
- TableViewPresetListResponse: 列表响应 schema

服务端校验规则：
- config 仅允许 keyword/sort/filters/hiddenColumns/columnOrder/pageSize 六个字段
- 禁止保存 selectedKeys/page/activeRunId/rows（业务数据与会话态）
- pageSize 范围 1-500
- filters 元素必须含 key/op/value
- sort 必须含 key + direction（asc/desc）
- columnOrder 每项必须是 string
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# config 允许的字段白名单（其他字段一律拒绝）
_ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset(
    {"keyword", "sort", "filters", "hiddenColumns", "columnOrder", "pageSize",
     "industry", "concept"}
)

# config 禁止的字段（业务数据与会话态，冗余防御）
_FORBIDDEN_CONFIG_KEYS: frozenset[str] = frozenset(
    {"selectedKeys", "page", "activeRunId", "rows", "results", "resultData"}
)

# filters.op 白名单（与前端 StrategyDataTable 支持的操作符一致）
_ALLOWED_FILTER_OPS: frozenset[str] = frozenset(
    {"contains", "eq", "gt", "gte", "lt", "lte", "between", "empty", "not_empty"}
)

# 每 user+table_id+strategy_key 最多 preset 数量
MAX_PRESETS_PER_SCOPE: int = 20


class TableViewPresetConfig(BaseModel):
    """表格视图配置内容 - 仅允许 keyword/sort/filters/hiddenColumns/columnOrder/pageSize。

    额外字段会被拒绝（422），防止前端误传 selectedKeys/page/activeRunId/rows。
    """

    model_config = ConfigDict(extra="forbid")

    keyword: str | None = Field(
        default=None, max_length=200, description="关键字搜索"
    )
    sort: dict[str, Any] | None = Field(
        default=None,
        description="排序配置 {key: str, direction: 'asc'|'desc'}",
    )
    filters: list[dict[str, Any]] | None = Field(
        default=None,
        description="筛选条件列表 [{key, op, value}]",
    )
    hiddenColumns: list[str] | None = Field(  # noqa: N815
        default=None,
        description="隐藏列 key 列表",
    )
    columnOrder: list[str] | None = Field(  # noqa: N815
        default=None,
        description="列顺序 key 列表（自定义列排列顺序）",
    )
    pageSize: int | None = Field(  # noqa: N815
        default=None, ge=1, le=500, description="每页大小（1-500）"
    )
    industry: str | None = Field(
        default=None, max_length=100, description="行业板块名称"
    )
    concept: str | None = Field(
        default=None, max_length=100, description="概念板块名称"
    )

    @model_validator(mode="after")
    def validate_sort_shape(self) -> TableViewPresetConfig:
        """sort 必须含 key（非空 string）与 direction（asc/desc）。"""
        if self.sort is None:
            return self
        if "key" not in self.sort or "direction" not in self.sort:
            raise ValueError("sort 必须含 key 与 direction 字段")
        sort_key = self.sort["key"]
        if not isinstance(sort_key, str) or not sort_key.strip():
            raise ValueError(
                f"sort.key 必须为非空 string，实际: {sort_key!r} (type={type(sort_key).__name__})"
            )
        direction = self.sort["direction"]
        if direction not in ("asc", "desc"):
            raise ValueError(f"sort.direction 必须为 asc 或 desc，实际: {direction!r}")
        return self

    @model_validator(mode="after")
    def validate_filters_shape(self) -> TableViewPresetConfig:
        """filters 元素必须是 dict 且含 key/op/value，op 限制白名单。"""
        if self.filters is None:
            return self
        for i, f in enumerate(self.filters):
            if not isinstance(f, dict):
                raise ValueError(f"filters[{i}] 必须为 dict，实际类型: {type(f).__name__}")
            if "key" not in f or "op" not in f or "value" not in f:
                raise ValueError(f"filters[{i}] 必须含 key/op/value 字段")
            if f["op"] not in _ALLOWED_FILTER_OPS:
                raise ValueError(
                    f"filters[{i}].op 不在白名单: {f['op']!r}。"
                    f"允许: {sorted(_ALLOWED_FILTER_OPS)}"
                )
        return self


class TableViewPresetCreate(BaseModel):
    """创建 preset 请求 - POST /me/table-view-presets。

    user_id 由 JWT 上下文注入，不接受 body 传入（安全约束）。
    """

    table_id: str = Field(..., min_length=1, max_length=64, description="表格标识")
    strategy_key: str | None = Field(
        default=None, max_length=64, description="策略 key（可空）"
    )
    name: str = Field(..., min_length=1, max_length=64, description="配置名称")
    config: dict[str, Any] = Field(..., description="配置内容")
    is_default: bool = Field(default=False, description="是否默认配置")

    @model_validator(mode="after")
    def validate_config_forbidden_keys(self) -> TableViewPresetCreate:
        """config 禁止保存 selectedKeys/page/activeRunId/rows 等业务数据。"""
        _validate_config_keys(self.config)
        return self


class TableViewPresetPatch(BaseModel):
    """更新 preset 请求 - PATCH /me/table-view-presets/{id}。

    所有字段可选，至少传一个。
    user_id/table_id/strategy_key 不可修改（保持隔离维度不变）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64, description="新名称")
    config: dict[str, Any] | None = Field(default=None, description="新配置内容")
    is_default: bool | None = Field(default=None, description="是否默认")

    @model_validator(mode="after")
    def validate_at_least_one_field(self) -> TableViewPresetPatch:
        """至少传一个字段。"""
        if self.name is None and self.config is None and self.is_default is None:
            raise ValueError("至少需要更新一个字段（name/config/is_default）")
        return self

    @model_validator(mode="after")
    def validate_config_forbidden_keys(self) -> TableViewPresetPatch:
        """config 禁止保存 selectedKeys/page/activeRunId/rows 等业务数据。"""
        if self.config is not None:
            _validate_config_keys(self.config)
        return self


class TableViewPresetResponse(BaseModel):
    """preset 响应 schema。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="preset ID")
    user_id: UUID = Field(..., description="用户 ID（由认证上下文注入）")
    table_id: str = Field(..., description="表格标识")
    strategy_key: str | None = Field(None, description="策略 key")
    name: str = Field(..., description="配置名称")
    config: dict[str, Any] = Field(..., description="配置内容")
    is_default: bool = Field(..., description="是否默认")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class TableViewPresetListResponse(BaseModel):
    """preset 列表响应。"""

    items: list[TableViewPresetResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


def _validate_config_keys(config: dict[str, Any]) -> None:
    """校验 config 字段只含白名单内的 key，并深度校验 filters/hiddenColumns/sort。

    - 拒绝 selectedKeys/page/activeRunId/rows/results/resultData 等业务数据
    - 拒绝未在白名单内的其他 key
    - filters 每项必须是 dict 且含 key/op/value，op 限制白名单
    - hiddenColumns 每项必须是 string
    - sort.key 必须是非空 string
    """
    if not isinstance(config, dict):
        raise ValueError(f"config 必须为 dict，实际类型: {type(config).__name__}")

    keys = set(config.keys())
    forbidden = keys & _FORBIDDEN_CONFIG_KEYS
    if forbidden:
        raise ValueError(
            f"config 禁止保存以下字段: {sorted(forbidden)}。"
            f"仅允许: {sorted(_ALLOWED_CONFIG_KEYS)}"
        )

    unknown = keys - _ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"config 包含未知字段: {sorted(unknown)}。"
            f"仅允许: {sorted(_ALLOWED_CONFIG_KEYS)}"
        )

    # 类型校验（与 TableViewPresetConfig 一致）
    if "pageSize" in config and config["pageSize"] is not None:
        if not isinstance(config["pageSize"], int) or isinstance(config["pageSize"], bool):
            raise ValueError(
                f"config.pageSize 必须为 int，实际类型: {type(config['pageSize']).__name__}"
            )
        if not (1 <= config["pageSize"] <= 500):
            raise ValueError(f"config.pageSize 必须在 1-500 之间，实际: {config['pageSize']}")

    if "filters" in config and config["filters"] is not None:
        if not isinstance(config["filters"], list):
            raise ValueError(
                f"config.filters 必须为 list，实际类型: {type(config['filters']).__name__}"
            )
        # [ConfigValidation] - 描述: filters 每项必须是 dict 且含 key/op/value，op 限制白名单
        for i, f in enumerate(config["filters"]):
            if not isinstance(f, dict):
                raise ValueError(
                    f"config.filters[{i}] 必须为 dict，实际类型: {type(f).__name__}"
                )
            if "key" not in f or "op" not in f or "value" not in f:
                raise ValueError(f"config.filters[{i}] 必须含 key/op/value 字段")
            if f["op"] not in _ALLOWED_FILTER_OPS:
                raise ValueError(
                    f"config.filters[{i}].op 不在白名单: {f['op']!r}。"
                    f"允许: {sorted(_ALLOWED_FILTER_OPS)}"
                )

    if "hiddenColumns" in config and config["hiddenColumns"] is not None:
        if not isinstance(config["hiddenColumns"], list):
            raise ValueError(
                f"config.hiddenColumns 必须为 list，实际类型: {type(config['hiddenColumns']).__name__}"
            )
        # [ConfigValidation] - 描述: hiddenColumns 每项必须是 string
        for i, col in enumerate(config["hiddenColumns"]):
            if not isinstance(col, str):
                raise ValueError(
                    f"config.hiddenColumns[{i}] 必须为 string，实际类型: {type(col).__name__}"
                )

    if "columnOrder" in config and config["columnOrder"] is not None:
        if not isinstance(config["columnOrder"], list):
            raise ValueError(
                f"config.columnOrder 必须为 list，实际类型: {type(config['columnOrder']).__name__}"
            )
        # [ConfigValidation] - 描述: columnOrder 每项必须是 string
        for i, col in enumerate(config["columnOrder"]):
            if not isinstance(col, str):
                raise ValueError(
                    f"config.columnOrder[{i}] 必须为 string，实际类型: {type(col).__name__}"
                )

    if "keyword" in config and config["keyword"] is not None:
        if not isinstance(config["keyword"], str):
            raise ValueError(
                f"config.keyword 必须为 str，实际类型: {type(config['keyword']).__name__}"
            )

    if "sort" in config and config["sort"] is not None:
        if not isinstance(config["sort"], dict):
            raise ValueError(
                f"config.sort 必须为 dict，实际类型: {type(config['sort']).__name__}"
            )
        sort_val = config["sort"]
        if "key" not in sort_val or "direction" not in sort_val:
            raise ValueError("sort 必须含 key 与 direction 字段")
        # [ConfigValidation] - 描述: sort.key 必须是非空 string
        sort_key = sort_val["key"]
        if not isinstance(sort_key, str) or not sort_key.strip():
            raise ValueError(
                f"sort.key 必须为非空 string，实际: {sort_key!r} (type={type(sort_key).__name__})"
            )
        if sort_val["direction"] not in ("asc", "desc"):
            raise ValueError(
                f"sort.direction 必须为 asc 或 desc，实际: {sort_val['direction']!r}"
            )


if __name__ == "__main__":
    # 自测入口：验证 schema 校验逻辑
    from pydantic import ValidationError

    # 合法 payload
    obj = TableViewPresetCreate(
        table_id="screener",
        strategy_key="dsa_selector",
        name="默认",
        config={"keyword": "茅台", "pageSize": 50, "columnOrder": ["stock", "price"]},
    )
    assert obj.table_id == "screener"
    print(f"合法 payload: {obj}")

    # 非法：selectedKeys
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config={"selectedKeys": ["a"]},
        )
        raise AssertionError("应拒绝 selectedKeys")
    except ValidationError as e:
        print(f"selectedKeys 已拒绝: {str(e)[:80]}")

    # 非法：page
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config={"page": 3},
        )
        raise AssertionError("应拒绝 page")
    except ValidationError as e:
        print(f"page 已拒绝: {str(e)[:80]}")

    # 非法：activeRunId
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config={"activeRunId": "x"},
        )
        raise AssertionError("应拒绝 activeRunId")
    except ValidationError as e:
        print(f"activeRunId 已拒绝: {str(e)[:80]}")

    # 非法：rows
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config={"rows": [{"symbol": "x"}]},
        )
        raise AssertionError("应拒绝 rows")
    except ValidationError as e:
        print(f"rows 已拒绝: {str(e)[:80]}")

    # 非法：config 不是 dict
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config="not-a-dict",
        )
        raise AssertionError("应拒绝非 dict config")
    except ValidationError as e:
        print(f"非 dict config 已拒绝: {str(e)[:80]}")

    # 非法：pageSize 类型
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config={"pageSize": "fifty"},
        )
        raise AssertionError("应拒绝非 int pageSize")
    except ValidationError as e:
        print(f"非 int pageSize 已拒绝: {str(e)[:80]}")

    # 非法：filters 类型
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config={"filters": "not-a-list"},
        )
        raise AssertionError("应拒绝非 list filters")
    except ValidationError as e:
        print(f"非 list filters 已拒绝: {str(e)[:80]}")

    # 非法：columnOrder 元素非 string
    try:
        TableViewPresetCreate(
            table_id="screener",
            name="bad",
            config={"columnOrder": ["stock", 123]},
        )
        raise AssertionError("应拒绝 columnOrder 非 string 元素")
    except ValidationError as e:
        print(f"columnOrder 非 string 元素已拒绝: {str(e)[:80]}")

    # 合法：空 config
    obj2 = TableViewPresetCreate(
        table_id="screener",
        name="empty",
        config={},
    )
    assert obj2.config == {}
    print(f"空 config 合法: {obj2}")

    # PATCH 至少一个字段
    try:
        TableViewPresetPatch()
        raise AssertionError("应拒绝空 PATCH")
    except ValidationError as e:
        print(f"空 PATCH 已拒绝: {str(e)[:80]}")

    print("OK")
