"""R1 smoke test - 验证后端工程骨架可导入且 /health 返回 200。

测试内容：
1. FastAPI app 能导入
2. /health 端点返回 200 + status=ok
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_app_importable() -> None:
    """测试 FastAPI app 能正常导入。"""
    assert app is not None
    assert app.title == "Trading Platform V1.1"


def test_health_endpoint() -> None:
    """测试 /health 端点返回 200。"""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "trading-platform"


def test_root_endpoint() -> None:
    """测试根路径返回应用信息。"""
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["app"] == "trading-platform"
    assert data["version"] == "1.1.0"


if __name__ == "__main__":
    # 自测入口：直接运行验证
    test_app_importable()
    print("test_app_importable: PASS")
    test_health_endpoint()
    print("test_health_endpoint: PASS")
    test_root_endpoint()
    print("test_root_endpoint: PASS")
    print("All smoke tests passed.")
