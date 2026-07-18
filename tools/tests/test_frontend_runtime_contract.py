"""前端运行时部署合同静态检查（CHANGE-20260718-007）。

P0 部署合同：生产入口永久固定 80:80，用户入口 80 必须直接呈现盘迹 SPA，
不得是默认 nginx 欢迎页。本测试对以下静态契约做断言，CI 阻断回归：

1. ``frontend/Dockerfile`` 必须把构建产物 COPY 到 ``/usr/share/nginx/html/``
   （nginx root），禁止复制到 ``/usr/share``（会导致服务默认欢迎页）。
2. ``frontend/Dockerfile`` 必须在 COPY 前 ``rm -rf /usr/share/nginx/html/*``
   删除默认 nginx index，确保 nginx root 只含 SPA 产物。
3. ``frontend/nginx.conf`` 的 ``root`` 必须是 ``/usr/share/nginx/html``，
   与 Dockerfile COPY 目标一致。
4. ``docker-compose.prod.yml`` 的 frontend 服务必须显式映射 ``80:80``，
   不得改端口。
5. ``docker-compose.prod.yml`` 的 frontend captures 卷挂载必须落在
   ``/usr/share/nginx/html/static/captures``（与 nginx root 一致）。

运行:
    python -m pytest tools/tests/test_frontend_runtime_contract.py -q

注意：本测试只做静态文件检查，不启动容器。运行时内容探针（/、JS/CSS 资源、
SPA 回退、API 代理、容器内 index.html）由部署验收脚本在部署后执行。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "frontend" / "Dockerfile"
NGINX_CONF = ROOT / "frontend" / "nginx.conf"
COMPOSE_PROD = ROOT / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    """读取 frontend/Dockerfile 全文。"""
    assert DOCKERFILE.exists(), f"frontend/Dockerfile 不存在: {DOCKERFILE}"
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def nginx_conf_text() -> str:
    """读取 frontend/nginx.conf 全文。"""
    assert NGINX_CONF.exists(), f"frontend/nginx.conf 不存在: {NGINX_CONF}"
    return NGINX_CONF.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_prod_text() -> str:
    """读取 docker-compose.prod.yml 全文。"""
    assert COMPOSE_PROD.exists(), f"docker-compose.prod.yml 不存在: {COMPOSE_PROD}"
    return COMPOSE_PROD.read_text(encoding="utf-8")


class TestDockerfileCopyTarget:
    """Dockerfile 必须把 dist COPY 到 nginx root /usr/share/nginx/html/。"""

    def test_copy_dist_to_nginx_html(self, dockerfile_text: str) -> None:
        """COPY --from=builder /app/dist/ 目标必须是 /usr/share/nginx/html/。

        回归守护：本分支前曾误写为 `COPY --from=builder /app/dist /usr/share`，
        导致 nginx root（/usr/share/nginx/html）下仍残留默认欢迎页，
        用户入口 80 直接呈现 "Welcome to nginx" 而非 SPA。P0 阻断项。
        """
        # 允许 /app/dist/ 或 /app/dist（尾斜杠可选）
        pattern = r"COPY\s+--from=builder\s+/app/dist/?\s+/usr/share/nginx/html/?"
        assert re.search(pattern, dockerfile_text), (
            "frontend/Dockerfile 必须包含 "
            "`COPY --from=builder /app/dist/ /usr/share/nginx/html/`；"
            "禁止复制到 /usr/share（会导致 nginx 服务默认欢迎页）"
        )

    def test_no_copy_dist_to_bare_usr_share(self, dockerfile_text: str) -> None:
        """禁止 COPY --from=builder /app/dist 到 /usr/share（非 nginx root）。"""
        # 匹配 COPY --from=builder /app/dist 后面紧跟 /usr/share 但不是 /usr/share/nginx
        pattern = r"COPY\s+--from=builder\s+/app/dist/?\s+/usr/share(?!\s*/nginx)"
        assert not re.search(pattern, dockerfile_text), (
            "frontend/Dockerfile 禁止 `COPY --from=builder /app/dist /usr/share`；"
            "必须复制到 /usr/share/nginx/html/（nginx root）"
        )

    def test_rm_default_nginx_html_before_copy(self, dockerfile_text: str) -> None:
        """COPY dist 前必须 rm -rf /usr/share/nginx/html/* 删除默认欢迎页。

        nginx:alpine 基础镜像自带 /usr/share/nginx/html/index.html（欢迎页）与
        index.nginx-debian.html。若不删除，COPY dist 不会覆盖 index.nginx-debian.html，
        nginx 可能优先服务默认欢迎页。
        """
        rm_pattern = r"RUN\s+rm\s+-rf\s+/usr/share/nginx/html/\*"
        copy_pattern = r"COPY\s+--from=builder\s+/app/dist/?\s+/usr/share/nginx/html/?"
        rm_match = re.search(rm_pattern, dockerfile_text)
        copy_match = re.search(copy_pattern, dockerfile_text)
        assert rm_match, (
            "frontend/Dockerfile 必须包含 "
            "`RUN rm -rf /usr/share/nginx/html/*` 删除默认 nginx 欢迎页"
        )
        assert copy_match, (
            "frontend/Dockerfile 必须包含 "
            "`COPY --from=builder /app/dist/ /usr/share/nginx/html/`"
        )
        # rm 必须在 COPY 之前（行号更小）
        assert rm_match.start() < copy_match.start(), (
            "`RUN rm -rf /usr/share/nginx/html/*` 必须在 "
            "`COPY --from=builder /app/dist/ /usr/share/nginx/html/` 之前"
        )


class TestNginxConfRoot:
    """nginx.conf 的 root 必须与 Dockerfile COPY 目标一致。"""

    def test_root_is_nginx_html(self, nginx_conf_text: str) -> None:
        """root 指令必须是 /usr/share/nginx/html。"""
        # 匹配 `root /usr/share/nginx/html;`（允许前后空白）
        pattern = r"^\s*root\s+/usr/share/nginx/html\s*;"
        assert re.search(pattern, nginx_conf_text, re.MULTILINE), (
            "frontend/nginx.conf 必须包含 `root /usr/share/nginx/html;`，"
            "与 Dockerfile COPY 目标一致"
        )

    def test_spa_fallback_to_index_html(self, nginx_conf_text: str) -> None:
        """SPA 路由回退：try_files $uri $uri/ /index.html。"""
        pattern = r"try_files\s+\$uri\s+\$uri/\s+/index\.html"
        assert re.search(pattern, nginx_conf_text), (
            "frontend/nginx.conf 必须包含 `try_files $uri $uri/ /index.html` "
            "以支持 SPA 前端路由回退"
        )

    def test_api_proxy_strips_api_prefix(self, nginx_conf_text: str) -> None:
        """/api/ 反向代理必须 rewrite 去掉 /api 前缀转发到后端。"""
        # 匹配 location /api/ 块内 rewrite ^/api/(.*) /$1 break
        assert "location /api/" in nginx_conf_text, (
            "frontend/nginx.conf 必须包含 `location /api/` 反向代理块"
        )
        rewrite_pattern = r"rewrite\s+\^/api/\(\.\*\)\s+/\$1\s+break"
        assert re.search(rewrite_pattern, nginx_conf_text), (
            "frontend/nginx.conf /api/ 块必须包含 "
            "`rewrite ^/api/(.*) /$1 break` 去掉 /api 前缀"
        )

    def test_listen_80(self, nginx_conf_text: str) -> None:
        """nginx 必须监听 80 端口（生产合同固定 80:80）。"""
        pattern = r"listen\s+80\s*;"
        assert re.search(pattern, nginx_conf_text), (
            "frontend/nginx.conf 必须包含 `listen 80;`（生产合同固定 80:80）"
        )


class TestComposeProdFrontend:
    """docker-compose.prod.yml frontend 服务端口与卷挂载合同。"""

    def test_frontend_ports_80_80(self, compose_prod_text: str) -> None:
        """frontend 服务必须显式映射 80:80，不得改端口。"""
        # 提取 frontend 服务块
        frontend_block = _extract_service_block(compose_prod_text, "frontend")
        assert frontend_block, "docker-compose.prod.yml 必须包含 frontend 服务定义"
        # 匹配 "80:80"（YAML 字符串形式）
        port_pattern = r"['\"]?80:80['\"]?"
        assert re.search(port_pattern, frontend_block), (
            "docker-compose.prod.yml frontend 服务必须映射 `80:80`；"
            "生产入口永久固定 80:80，禁止改端口"
        )

    def test_frontend_captures_volume_target(self, compose_prod_text: str) -> None:
        """captures 卷挂载目标必须是 /usr/share/nginx/html/static/captures。

        与 Dockerfile COPY 目标（/usr/share/nginx/html）一致，确保
        nginx 能直接服务截图静态资源。
        """
        frontend_block = _extract_service_block(compose_prod_text, "frontend")
        assert frontend_block, "frontend 服务块不存在"
        vol_pattern = r"capture_static:/usr/share/nginx/html/static/captures"
        assert re.search(vol_pattern, frontend_block), (
            "docker-compose.prod.yml frontend 服务必须挂载 "
            "`capture_static:/usr/share/nginx/html/static/captures` "
            "（与 nginx root 一致）"
        )

    def test_frontend_container_name_trading_frontend(
        self, compose_prod_text: str
    ) -> None:
        """frontend 容器名必须是 trading-frontend（部署脚本与监控依赖）。"""
        frontend_block = _extract_service_block(compose_prod_text, "frontend")
        assert frontend_block, "frontend 服务块不存在"
        assert "trading-frontend" in frontend_block, (
            "docker-compose.prod.yml frontend 服务必须设置 "
            "container_name: trading-frontend"
        )

    def test_frontend_restart_policy(self, compose_prod_text: str) -> None:
        """frontend 服务必须设置 restart: unless-stopped。"""
        frontend_block = _extract_service_block(compose_prod_text, "frontend")
        assert frontend_block, "frontend 服务块不存在"
        assert "unless-stopped" in frontend_block, (
            "docker-compose.prod.yml frontend 服务必须设置 restart: unless-stopped"
        )


class TestDockerfileStageConsistency:
    """Dockerfile 多阶段构建一致性检查。"""

    def test_builder_stage_builds_dist(self, dockerfile_text: str) -> None:
        """builder 阶段必须执行 npm run build 生成 dist/。"""
        assert "npm run build" in dockerfile_text, (
            "frontend/Dockerfile builder 阶段必须执行 `npm run build` 生成 dist/"
        )

    def test_runtime_stage_from_nginx_alpine(self, dockerfile_text: str) -> None:
        """runtime 阶段必须基于 nginx:alpine（或其 digest 固定变体）。"""
        # 匹配 FROM nginx:alpine 或 FROM nginx:alpine@sha256:...
        pattern = r"FROM\s+nginx:alpine(@sha256:[a-f0-9]+)?"
        assert re.search(pattern, dockerfile_text), (
            "frontend/Dockerfile runtime 阶段必须基于 `nginx:alpine` "
            "（允许 digest 固定）"
        )

    def test_nginx_conf_copied_to_conf_d(self, dockerfile_text: str) -> None:
        """nginx.conf 必须复制到 /etc/nginx/conf.d/default.conf。"""
        pattern = r"COPY\s+nginx\.conf\s+/etc/nginx/conf\.d/default\.conf"
        assert re.search(pattern, dockerfile_text), (
            "frontend/Dockerfile 必须包含 "
            "`COPY nginx.conf /etc/nginx/conf.d/default.conf`"
        )


def _extract_service_block(compose_text: str, service_name: str) -> str:
    """从 docker-compose YAML 提取指定服务块（行级朴素解析，不依赖 PyYAML）。

    Args:
        compose_text: docker-compose YAML 全文
        service_name: 服务名（如 frontend）

    Returns:
        服务块文本（从 `  <service_name>:` 到下一个同级服务或文件末尾）；
        若不存在返回空字符串。
    """
    lines = compose_text.splitlines()
    start_idx: int | None = None
    for i, line in enumerate(lines):
        # 匹配 `  frontend:` 形式（2 空格缩进，services 下一级）
        if line.rstrip() == f"  {service_name}:":
            start_idx = i
            break
    if start_idx is None:
        return ""
    # 收集该服务块所有行，直到遇到同级服务（`  xxx:` 2 空格缩进）或文件末尾
    block_lines: list[str] = []
    for line in lines[start_idx + 1 :]:
        # 同级服务开始（2 空格缩进 + 名称 + :），且不是空行/注释
        if (
            line.startswith("  ")
            and not line.startswith("    ")
            and line.strip()
            and not line.strip().startswith("#")
        ):
            # 检测 `  name:` 形式
            stripped = line.strip()
            if stripped.endswith(":") and not stripped.startswith("-"):
                break
        block_lines.append(line)
    return "\n".join(block_lines)


if __name__ == "__main__":
    # 直接运行时执行所有测试
    pytest.main([__file__, "-v"])
