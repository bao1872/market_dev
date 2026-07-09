"""API 路由类型收窄 helper。

FastAPI 的 router.routes 是 list[BaseRoute]，BaseRoute 没有 path/methods 属性。
直接访问 r.path 会触发 mypy [attr-defined] 错误。

用 isinstance(route, APIRoute) 收窄后可安全访问 path/methods。
"""

from collections.abc import Iterator

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute


def iter_api_routes(routes: list[BaseRoute]) -> Iterator[APIRoute]:
    """从 BaseRoute 列表中筛选 APIRoute，收窄后可安全访问 .path/.methods。"""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route


def get_route_paths(routes: list[BaseRoute]) -> list[str]:
    """获取所有 APIRoute 的 path 列表。"""
    return [r.path for r in iter_api_routes(routes)]
