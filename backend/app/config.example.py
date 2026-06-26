"""配置文件示例 - 展示 config.local.py / config.test.py 的必需字段。

用法：
    cp config.example.py config.local.py   # 开发环境
    cp config.example.py config.test.py    # 测试环境

然后填入实际的 DATABASE_URL。

约束（由 app/config.py 启动硬校验强制）：
- DATABASE_URL 必须为 postgresql+psycopg:// 格式，禁止 sqlite
- 开发环境（config.local.py）：库名必须含 bz_stock 且不得含 _test
- 测试环境（config.test.py）：库名必须含 _test
"""

# PostgreSQL 连接串（postgresql+psycopg://，禁止 sqlite）
# 开发环境：库名必须含 bz_stock 且不得含 _test
DATABASE_URL = "postgresql+psycopg://user:password@127.0.0.1:5432/bz_stock"

# 可选：覆盖其他启动级配置（不覆盖则用 app/config.py 中的默认值或环境变量）
# REDIS_URL = "redis://localhost:6379/0"
# JWT_SECRET = "change-me"
# APP_ENV = "development"
# LOG_LEVEL = "INFO"
