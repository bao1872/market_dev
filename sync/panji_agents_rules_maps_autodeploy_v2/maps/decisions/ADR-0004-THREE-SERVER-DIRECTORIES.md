# ADR-0004: 腾讯云三目录分离

- Status: Proposed

## Decision

```text
/root/web_dev       开发
/opt/panji-deploy   自动部署 checkout
/opt/panji-live     运行
```

## Reason

TRAE CN 需要保留未提交开发能力，但自动部署必须使用 clean 工作区。运行目录不应承担 Git 开发。
