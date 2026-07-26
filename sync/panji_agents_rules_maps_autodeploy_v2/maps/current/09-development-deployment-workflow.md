# 开发和部署工作流

## 1. 默认循环

```text
需求
→ Work 或 CN 修改 dev
→ 测试
→ commit
→ push dev
→ GitHub Actions
→ 腾讯云自动部署
→ 浏览器/日志验证
→ 继续迭代
```

## 2. 分支

```text
dev   每日开发和运行线
main  阶段稳定锚点
```

临时分支只在需要隔离 WIP 时使用。

## 3. TRAE Work

适合：

- 通用开发；
- 文档；
- 前端预览；
- 静态和测试；
- push dev。

不接触服务器和真实数据库。

## 4. TRAE CN

按需承担：

- 开发；
- 真实测试；
- 自动部署观察；
- 手动部署；
- 运维；
- 紧急修复。

## 5. 自动部署

GitHub push dev：

```text
快速检查
→ SSH fixed command
→ /opt/panji-deploy checkout SHA
→ 分类
→ deploy
→ verify
```

## 6. 目录

```text
/root/web_dev       CN 开发
/opt/panji-deploy   自动部署
/opt/panji-live     运行
```

## 7. 阻塞条件

- migration；
-依赖；
-Dockerfile；
-Compose；
-Nginx；
-环境合同；
-未知高风险文件。

阻塞后由 CN 继续处理，不回退为复杂审批体系。

## 8. main 推进

每完成一个明显阶段并确认 dev 稳定：

```text
dev → main
```

main 不需要同时运行第二套服务。
