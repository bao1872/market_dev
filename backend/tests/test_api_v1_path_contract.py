from __future__ import annotations

from app.main import app

_FRAMEWORK_PATHS = {
    "/",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


def _application_paths() -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        if hasattr(route, "path"):
            paths.add(route.path)
            continue
        included = getattr(route, "original_router", None)
        for nested in getattr(included, "routes", ()):  # FastAPI lazy router support
            path = getattr(nested, "path", None)
            if isinstance(path, str):
                paths.add(path)
    return paths


def test_all_business_routes_use_single_v1_prefix() -> None:
    paths = _application_paths()
    business_paths = paths - _FRAMEWORK_PATHS
    assert business_paths
    assert all(path.startswith("/v1/") or path == "/v1" for path in business_paths)
    assert not any(path.startswith("/api/") for path in business_paths)
    assert not any("/v1/v1/" in path for path in business_paths)


def test_canonical_gateway_targets_exist() -> None:
    paths = _application_paths()
    required = {
        "/v1/health",
        "/v1/auth/login",
        "/v1/me/access",
        "/v1/market/stocks",
        "/v1/instruments/{instrument_id}/bars",
        "/v1/review/dates",
        "/v1/auction",
    }
    assert required <= paths
