# 回滚

## Code

```text
previous RUNTIME_SHA
→ gateway/manual deploy previous SHA
→ verify
```

## Data

代码回滚不恢复数据或 schema。数据问题必须单独处理。

## Evidence

记录：

- failed SHA；
- previous SHA；
- migration；
- actual runtime；
- business status。
