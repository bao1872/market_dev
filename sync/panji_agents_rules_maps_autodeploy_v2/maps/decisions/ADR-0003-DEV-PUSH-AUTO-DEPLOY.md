# ADR-0003: dev push 自动部署

- Status: Proposed
- Date: 2026-07-26

## Context

盘迹处于调试开发阶段，单人高频迭代。每次人工审批部署和过多分支明显降低反馈速度。

## Decision

- dev 为日常运行线；
- push dev 自动部署腾讯云；
- main 为阶段稳定锚点；
- 临时分支按需；
- CN 保留完整开发测试能力；
- 自动部署与 CN 开发目录分离；
- migration 保留手动门禁。

## Consequences

优点：

- 反馈快；
- Git 可追踪；
- CN 和 Work 都能开发；
- 自动部署不受 dirty 开发目录影响。

代价：

- dev push 必须是可运行 commit；
- deployment workflow 和 SSH key 需要受限；
- migration/依赖/Compose 暂时不全自动。
