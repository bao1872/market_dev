"""配置文件示例 - 展示 config.local.py / config.test.py / 生产配置文件的必需字段。

用法：
    cp config.example.py config.local.py   # 开发环境
    cp config.example.py config.test.py    # 测试环境

生产环境（Docker）还可以通过 CONFIG_FILE 环境变量挂载外部文件，例如：
    CONFIG_FILE=/app/app/config.production.py

然后填入实际的 DATABASE_URL、JWT_SECRET、SECRET_MASTER_KEY。

约束（由 app/config.py 启动硬校验强制）：
- DATABASE_URL 必须为 postgresql+psycopg:// 格式，禁止 sqlite
- 开发环境（config.local.py）：库名必须含 bz_stock 且不得含 _test
- 测试环境（config.test.py）：库名必须含 _test
- 生产环境：DATABASE_URL 不得含 _test
- 生产环境：JWT_SECRET 不能为默认值 change-me 或空
- 生产环境：SECRET_MASTER_KEY 不能为 replace-in-development-only / local-dev-only 或空
"""

# PostgreSQL 连接串（postgresql+psycopg://，禁止 sqlite）
# 开发环境：库名必须含 bz_stock 且不得含 _test
DATABASE_URL = "postgresql+psycopg://<DB_USER>:<DB_PASSWORD>@127.0.0.1:5432/bz_stock"

# 可选：覆盖其他启动级配置（不覆盖则用 app/config.py 中的默认值或环境变量）
# REDIS_URL = "redis://localhost:6379/0"
# JWT_SECRET = "<REPLACE_WITH_STRONG_JWT_SECRET>"            # 生产环境必须替换为强密钥
# SECRET_MASTER_KEY = "<REPLACE_WITH_STRONG_MASTER_KEY>"     # 生产环境必须替换为强密钥
# APP_ENV = "development"
# LOG_LEVEL = "INFO"
# FRONTEND_BASE_URL = "http://localhost:5173"
# CAPTURE_WORKER_URL = "http://worker-capture:8001"
