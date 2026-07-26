# Worker Map

统一入口：`backend/app/worker.py`

Compose 服务名必须以当前 `docker-compose.prod.yml` 为准。后端代码部署时需要判断哪些 Worker 共享相同运行代码。
