# 85 服务器目录边界

## `/root/web_dev`

TRAE CN 开发工作区。

允许：

- 分支切换；
- 未提交修改；
- 测试；
- commit / push。

禁止把该目录直接作为自动部署工作区。

## `/opt/panji-deploy`

自动部署工作区。

要求：

- clean；
- detached target SHA；
- 仅部署脚本写入；
- 不保存临时开发修改；
- 不存生产秘密；
- 不作为数据库目录。

## `/opt/panji-live`

运行时目录。

要求：

- 由部署脚本更新；
- `RUNTIME_SHA` 与实际代码一致；
- Python 文件和前端 dist 可按合同 live mount；
- 不作为 Git 开发仓库；
- 不手工修业务代码。

## PostgreSQL / Redis

与三个代码目录独立。自动部署不得重建。
