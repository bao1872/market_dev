# 给 TRAE CN 的自动部署落地指令

本任务只建设 dev push 自动部署，不继续扩展业务功能。

## 目标

```text
dev push
→ GitHub Actions
→ 受限 SSH
→ 腾讯云固定脚本
→ /opt/panji-deploy
→ /opt/panji-live
```

同时保留 `/root/web_dev` 的完整开发测试能力。

## 执行要求

1. 先读取 AGENTS、相关 rules、maps 和现有部署脚本。
2. 检查当前 git/status/runtime/Compose/资源。
3. 不使用 `/root/web_dev` 作为自动部署目录。
4. 不修改数据库、不删除 volume、不执行 pg_dump。
5. 先输出当前部署架构和差异，再改文件。
6. 所有新脚本先用临时路径验证。
7. forced command 必须只接受完整 40 位 SHA。
8. 验证 SHA 必须属于 `origin/dev`。
9. migration、依赖、Dockerfile、Compose、Nginx 默认 BLOCKED。
10. frontend/backend 普通源码才允许 live auto deploy。
11. 使用 `flock`。
12. 部署失败恢复 previous runtime SHA。
13. 完成 docs-only、frontend-only、backend-only、migration-block 四组测试。
14. 不直接启用 workflow，先提交分支等待用户确认。
15. 输出 GitHub secrets、服务器一次性初始化命令和启用步骤，但不得回显私钥。

## 不做

- 不建立 main/dev 两套服务；
- 不要求每次 PR；
- 不把 CN 限制为只部署；
- 不把 self-hosted runner 作为前置；
- 不修改业务算法；
- 不执行真实 migration。
