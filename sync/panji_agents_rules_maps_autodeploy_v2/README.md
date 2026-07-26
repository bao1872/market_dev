# 盘迹 AGENTS + Rules + Maps 自动部署文档系统 V2

适用阶段：调试、开发、内部测试。

## 核心结构

```text
AGENTS.md = Agent 入口和最高边界
rules/    = 长期强制规则
maps/     = 当前事实、代码地图、开发记忆、操作手册和证据
```

## 当前工作模型

```text
TRAE Work 或 TRAE CN
        ↓
      GitHub dev
        ↓ push 自动触发
  GitHub Actions
        ↓ 受限 SSH
腾讯云固定部署脚本
        ↓
 /opt/panji-deploy
        ↓
 /opt/panji-live
        ↓
当前唯一应用 + 核心 PostgreSQL/Redis
```

## 两个长期分支

```text
dev   日常开发、集成和自动部署
main  阶段稳定锚点
```

临时分支不是强制流程，只在复杂、跨天或高风险任务中使用。

## 腾讯云目录

```text
/root/web_dev       TRAE CN 完整开发和测试目录
/opt/panji-deploy   自动部署专用干净 Git 工作区
/opt/panji-live     当前运行文件
```

腾讯云只运行一套应用和一套数据库。

## 包含内容

- 完整 `AGENTS.md`
- 11 份规则文件
- 完整 `maps/` 知识系统
- TRAE Work 与 TRAE CN 工作模式
- dev push 自动部署流程
- GitHub Actions 模板
- 腾讯云受限 SSH 和部署脚本模板
- migration、回滚、故障恢复手册
- 开发、部署、证据和故障交接模板
- 文档系统检查脚本
- 实施计划与 TRAE CN 落地指令

## 重要说明

这是目标文档和部署模板包，不会自动改动仓库或腾讯云。合入前必须在独立任务中核对：

- 当前 Compose 服务名；
- 现有 `scripts/deploy_live_runtime.sh` 行为；
- `/version` 和健康检查路径；
- 当前服务器用户和目录；
- GitHub 仓库是否已有 Actions 配置。
