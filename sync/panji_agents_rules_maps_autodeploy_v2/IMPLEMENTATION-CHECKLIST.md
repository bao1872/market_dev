# 实施检查清单

## 文档

- [ ] 独立分支加入 AGENTS/rules/maps
- [ ] 旧 docs 分阶段迁移
- [ ] knowledge checker 进入 CI
- [ ] CHANGE 和 ADR

## Git

- [ ] dev 存在并可自动部署
- [ ] main 作为稳定锚点
- [ ] dev/main 禁止 force push
- [ ] 临时分支按需

## 腾讯云

- [ ] CN 当前 WIP 先安全收尾
- [ ] `/root/web_dev` 保留
- [ ] 初始化 `/opt/panji-deploy`
- [ ] 核对 `/opt/panji-live`
- [ ] 创建 panji-deploy 用户
- [ ] 安装 forced-command key
- [ ] 安装 gateway 和 deploy script
- [ ] 配置 flock
- [ ] 核对现有 deploy_live_runtime.sh
- [ ] 核对 version/health URL

## GitHub

- [ ] 添加 deploy-dev workflow
- [ ] 添加 Environment `tencent-dev`
- [ ] 添加 host/port/user/key secrets
- [ ] workflow permissions read-only
- [ ] 不在 workflow 存数据库秘密

## 测试

- [ ] docs-only
- [ ] frontend-only
- [ ] backend-only
- [ ] combined
- [ ] migration blocked
- [ ] dependency blocked
- [ ] invalid SHA
- [ ] concurrent push
- [ ] failed health
- [ ] manual rerun
- [ ] dirty `/root/web_dev` 不影响部署
