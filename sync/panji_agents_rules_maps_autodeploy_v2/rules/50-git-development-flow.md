# 50 Git 开发流

## 长期分支

```text
dev
main
```

## dev

- 日常开发默认分支；
- push 自动部署；
- Work 和 CN 均可开发；
- push 前提交必须可部署；
- 不允许把破碎 checkpoint push 到 dev；
- 复杂 WIP 应使用临时分支。

## main

- 阶段稳定锚点；
- 不要求与 dev 实时同步；
- 一个阶段验收后由 dev 合并；
- 不自动部署当前开发服务器，除非用户明确切换运行线。

## 临时分支

按需，不强制：

```text
fix/*
feat/*
refactor/*
docs/*
experiment/*
```

## 提交

禁止：

```text
git add .
git add -A
git add -u
```

必须精确暂存。

禁止 force push dev/main。

## 自动部署

push dev 后：

- 必须记录 Target SHA；
- Actions 自动部署；
- 后续提交不能被描述为已部署，直到 runtime SHA 更新；
- 部署失败时 dev 代码可以继续存在，但当前运行版本仍是旧 SHA。
