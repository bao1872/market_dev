# 70 TRAE CN 多模式规则

## 定位

TRAE CN 是腾讯云完整工程环境。不是单一部署机器人。

## 模式 A：开发

在 `/root/web_dev`：

- 阅读、修改和测试代码；
- 可直接开发真实环境相关问题；
- 完成后 commit + push；
- 正式版本仍通过 GitHub dev 自动部署。

## 模式 B：真实测试

- API；
- PostgreSQL；
- Worker；
-盘后任务；
-飞书和 Capture；
- Nginx；
-资源和日志。

## 模式 C：自动部署观察

- 查看 Actions 结果；
- 核对 `/version`；
- 查看日志和容器；
- 补充业务验收 evidence。

## 模式 D：手动部署

自动部署失败、需重跑或用户要求时，调用与 Actions 相同的固定脚本。

## 模式 E：运维排障

- Docker、网络、Nginx、资源、环境变量；
- 不自行重写业务逻辑。

## 模式 F：紧急修复

只有必要时在 `/root/web_dev` 修改；完成后必须提交 GitHub，再由固定流程部署。

## 禁止

- 在 `/opt/panji-deploy` 日常开发；
- 在 `/opt/panji-live` 手改正式代码；
- 只修服务器不 push；
- 让自动部署使用 dirty `/root/web_dev`。
