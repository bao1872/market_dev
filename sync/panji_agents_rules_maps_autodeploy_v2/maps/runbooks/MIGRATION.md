# Migration

## Work/CN 开发

- 新增前向 migration；
- 测试 upgrade/downgrade/upgrade；
- 说明表、锁、写入和兼容性。

## 自动部署

检测 migration 即 BLOCKED。

## CN

用户明确批准后：

```text
记录当前 heads
→ 执行
→ single head
→ schema 验证
→ 部署应用
```

禁止清库重来。
