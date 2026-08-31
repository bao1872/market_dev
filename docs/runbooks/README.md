# Runbooks

本目录存放盘迹项目中可重复执行的操作步骤。Runbooks 只说明如何执行操作，不重新定义产品行为或实现架构。

当正式命令或操作步骤已经真实执行成功，或已由经过验证的自动化合同覆盖时，Runbook
可随实现任务同步，无需第二次文档授权。未经执行或验证的计划不得写成当前操作事实；
Runbook 更新本身不授权部署、Migration、生产数据写入或远程运行修改。

## 可用 Runbooks

- [本地开发环境启动与停止](local-development.md)：SSH 隧道、Backend、Frontend 的启动、停止、状态检查和只读核验。
