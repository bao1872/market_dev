# 25 工程实现规范

> **定位：通用工程实现规范（Cross-cutting Implementation Standard）**
>
> 本文件规定盘迹项目中后端、前端、数据处理、数据库访问、并发、性能、测试代码及相关工具代码的**通用实现方式**。
>
> 本文件回答：
>
> **“已经明确要实现什么之后，代码具体应该怎样写？”**
>
> 本文件不重新定义产品业务、Canonical 计算合同、安全边界、测试层级、运行验收、Git 流程或部署规则。

---

# 0. 权威边界

## 0.1 本文件负责什么

本文件负责跨模块长期稳定的**实现层规则**，主要包括：

* Python 代码结构；
* 数值计算与 DataFrame 实现；
* 算法复杂度；
* 数据库访问方式；
* Async / 并发 / IO；
* 性能实现方法；
* API 代码结构；
* React / TypeScript 实现；
* 测试代码本身的写法；
* Logging；
* 配置；
* Dependency；
* 文件、模块、命名和注释；
* 实现阶段完成标准。

---

## 0.2 本文件不拥有的合同

以下内容不得在本文件重新定义。

| 内容                                              | 权威来源                                      |
| ----------------------------------------------- | ----------------------------------------- |
| 产品目标、用户行为、指标定义、业务规则                             | `docs/prd/`                               |
| 产品域长期不变量                                        | `rules/10-product-domain-invariants.md`   |
| 行情读取、PIT、Canonical、No Future Leakage、正式计算 owner | `rules/20-market-data-computation.md`     |
| 权限、秘密、真实数据安全                                    | `rules/30-security-data-safety.md`        |
| 应运行哪些测试、验证层级和证据                                 | `rules/40-testing-quality.md`             |
| Git、branch、commit、push                          | `rules/50-git-development-flow.md`        |
| Runtime、API → Frontend 技术闭环                     | `rules/60-runtime-frontend-acceptance.md` |
| Hardening / Release                             | `rules/70-hardening-release.md`           |
| Deployment / Migration / Long Task Safety       | `rules/80-deployment-migration.md`        |
| 永久禁止和 deprecated path                           | `rules/90-deprecated-forbidden.md`        |

如本文件与更具体规则发生冲突：

> **更具体规则优先。**

---

# 1. 规则语言与执行范围

## 1.1 规则语言

本文件统一使用：

* **MUST / 必须**：当前适用范围内违反即属于实现缺陷；
* **MUST NOT / 禁止**：当前适用范围内不得采用；
* **SHOULD / 应当**：默认采用；不采用时应有明确技术理由；
* **SHOULD NOT / 原则上禁止**：只有明确例外理由时采用；
* **MAY / 可以**：可选实现。

---

## 1.2 新代码与既有代码适用边界

本文件主要约束：

1. 新增代码；
2. 当前任务实际修改的代码；
3. 为完成当前任务必须同步调整的直接依赖路径。

发现当前任务范围之外的历史代码违反本规范时：

* 若影响当前结果正确性、安全、测试可信度或当前运行闭环，按对应规则处理；
* 若属于性能、风格、命名、抽象、模块组织等非当前 blocker，记录为 Deferred Debt；
* **MUST NOT 因发现历史不符合本规范而自动扩大当前 Hypothesis Slice。**

> 历史代码不符合最新实现规范，本身不构成 Exploration blocker。

---

## 1.3 Exploration 优先

当前项目阶段与执行模式由 `AGENTS.md` 定义。

本规范不得被解释为：

* 每次开发都进行全仓重构；
* 每次发现历史问题都立即修复；
* 每次功能修改都升级为 engineering hardening；
* 因代码风格问题阻塞当前产品假设验证。

正确性、安全、测试可信度和真实结果不因 Exploration 降低。

其他非当前阻塞项遵循 Deferred Debt 原则。

---

# 2. 通用实现原则

## 2.1 Existing Owner First

新增实现前，MUST 先寻找现有 owner。

至少检查是否已经存在：

* domain service；
* canonical computation；
* repository；
* schema / model；
* API DTO；
* frontend service / hook；
* formatter；
* fixture；
* shared primitive。

禁止默认通过新建：

```text
*_v2.py
*_new.py
*_helper.py
*_calculator.py
*_for_review.py
*_for_monitor.py
```

绕过已有 owner。

如果发现需求可能形成第二业务 owner，应停止新增平行实现，并检查：

* `rules/20-market-data-computation.md`
* 相关 PRD
* 当前 Architecture / Map

Canonical ownership 的正式定义不在本文件重复。

---

## 2.2 Two-Strike 优先于抽象冲动

新增通用 abstraction、framework、base class、factory、通用 service 前，MUST 遵守 `rules/00-core-governance.md` 的 Two-Strike Architecture Rule。

第一次出现局部问题：

> 优先局部、明确、可测试地解决。

只有：

* 同类问题真实出现至少第二次；
* 或已经存在两个明确消费者；
* 或当前业务本身要求共享 owner；

才考虑抽象。

禁止为了“以后可能复用”提前建设 framework。

---

## 2.3 DRY 不是形式去重

只有以下条件同时成立时，SHOULD 抽取共享实现：

1. 业务语义相同；
2. 生命周期相同；
3. 修改原因相同；
4. 输入输出合同稳定。

两段代码“看起来一样”不等于它们属于同一个 abstraction。

---

## 2.4 计算与副作用分离

核心计算 SHOULD 尽可能设计为：

```python
result = calculate(inputs)
```

而不是：

```python
result = load_calculate_save_notify_everything()
```

优先保持：

```text
Load
  ↓
Validate
  ↓
Compute
  ↓
Persist
  ↓
Publish / Render
```

计算层原则上不直接：

* 查询数据库；
* 调用外部 HTTP；
* 写文件；
* 发送通知；
* 修改全局状态。

确有必要时必须有明确职责理由。

---

## 2.5 显式依赖

核心逻辑依赖以下信息时，SHOULD 显式传入或通过明确 dependency 获得：

* `trade_date`
* `as_of`
* scope
* universe
* runtime config
* external source
* persistence context

避免深层业务函数直接读取：

```python
datetime.now()
os.environ[...]
global_state
module_singleton
```

时间因果和正式数据口径仍以 `rules/20-*` 为准。

---

## 2.6 Failure 必须保持信息

实现层不得无信息地把：

```text
FAILED
UNAVAILABLE
STALE
PARTIAL
```

转成：

```text
None
[]
0
正常结果
```

具体业务状态定义由对应 domain / API contract 拥有。

本规范只要求：

> **实现不得在中间层丢失重要状态语义。**

---

## 2.7 Scope 最小化

当前任务只修改完成目标所需路径。

禁止：

* 顺手重构无关模块；
* 顺手统一整个仓库命名；
* 顺手替换框架；
* 顺手升级所有旧代码；
* 因当前局部问题进行大范围 architecture cleanup。

---

## 2.8 开发、测试、实验与正式运行共享同一语义路径（硬规则）

同一业务能力从开发到正式运行：

* MUST 复用同一 semantic owner；
* MUST NOT 为 Test / Experiment / Benchmark / Canary / Full Run 重新实现相同业务语义；
* SHOULD 尽可能复用同一 production code path；
* MAY 因运行目的不同而替换 adapter、fixture、input scale、runtime environment、execution mode、output destination 与 observability。

即：

```text
Semantic Owner       = MUST SAME
Infrastructure/Adapter/Input = MAY DIFFER
```

以下内容 MUST NOT 因 Development / Test / Experiment / Benchmark / Canary / Full Run 而被重新实现（语义不可漂移）：

* business classification；
* canonicalization；
* calculation formula；
* status derivation；
* lineage logic；
* production cost semantics。

推荐结构：

```text
Same Semantic Owner
        │
        ├── Unit / deterministic fixture
        ├── Micro / small input
        ├── Canary / representative real input
        └── Full / full input

即：

same semantics
+ different input / adapter / scale / environment
```

自检问题：

> 如果无法回答“哪一段正式代码从小规模验证一直运行到完整输入？”，
> 则应检查是否产生了 parallel implementation。

具体测试层级与 Runtime 验证要求仍由 `rules/40-testing-quality.md` 与相关 Runtime 规则负责。

---

# 3. Python 模块与脚本规范

Python 版本、lint、type-check 和工具配置以 `backend/pyproject.toml` 为准。

---

## 3.1 可执行脚本必须有明确入口

新建或实质修改的、可直接执行的 Python script MUST 提供明确 `main()`。

推荐：

```python
def main() -> int:
    ...
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

无需 exit code 时可以：

```python
def main() -> None:
    ...


if __name__ == "__main__":
    main()
```

本规则所称 executable script 不包括：

* FastAPI router；
* model；
* service；
* repository；
* library module；
* pytest test module。

---

## 3.2 Import 不得产生外部业务副作用

模块 import 时 MUST NOT：

* 连接数据库并执行业务操作；
* 写数据库；
* 调用外部 API；
* 执行业务 job；
* 启动线程或进程；
* 写文件；
* 执行 migration；
* 发送消息；
* 修改业务状态。

允许合理的纯内存初始化，例如：

```python
router = APIRouter()
logger = structlog.get_logger()
PATTERN = re.compile(...)
```

判断标准是：

> **Import 是否产生可观察的外部或业务副作用。**

---

## 3.3 函数职责单一

函数拆分依据不是固定行数，而是：

> 是否承担多个独立变化原因。

如果一个函数同时负责：

```text
fetch
+
validate
+
calculate
+
persist
+
notify
```

通常应拆分。

---

## 3.4 类型表达真实合同

公共 function、service、repository、domain model SHOULD 有明确 type hints。

核心业务对象 SHOULD NOT 长期依赖：

```python
dict[str, Any]
```

优先考虑：

* Pydantic model；
* dataclass；
* TypedDict；
* 明确 domain object。

---

## 3.5 禁止 Mutable Default

禁止：

```python
def process(items=[]):
    ...
```

应使用：

```python
def process(items=None):
    if items is None:
        items = []
```

或其他 immutable default。

---

## 3.6 避免 Magic Number / Magic String

错误：

```python
if score > 0.67:
    ...
```

应至少提升为具名语义：

```python
if score > POSITIVE_THRESHOLD:
    ...
```

如果阈值属于正式业务合同，则其定义 owner 由 PRD / domain rule 决定，本文件不重新定义。

---

## 3.7 Exception 必须表达原因

当业务失败具有稳定语义时，SHOULD 使用明确异常类型，例如：

```python
class InsufficientHistoryError(Exception):
    pass
```

避免所有问题都退化为：

```python
RuntimeError
ValueError
```

但也禁止为了形式给每个函数创造无价值异常类型。

---

## 3.8 禁止宽泛异常吞噬

禁止：

```python
try:
    ...
except Exception:
    pass
```

Boundary 层确需统一捕获时，应：

* 保留 traceback；
* 记录上下文；
* 明确 fallback 或重新抛出。

---

## 3.9 Resource 生命周期显式

以下资源 SHOULD 有清晰 lifecycle：

* DB session；
* transaction；
* file；
* lock；
* HTTP client；
* browser/session；
* temporary resource。

优先使用：

* context manager；
* dependency scope；
* explicit close/finalization。

---

# 4. 数值计算与 DataFrame 实现规范

盘迹存在大量：

* 横截面计算；
* rolling；
* ranking；
* aggregation；
* normalization；
* scope/member 计算；
* 时间序列处理。

这些路径默认必须考虑数据规模。

---

## 4.1 Vectorization First

对于天然可以批量表达的数值逻辑，MUST 优先评估：

* NumPy vector operations；
* Pandas column operations；
* boolean masks；
* `groupby`；
* `agg`；
* `transform`；
* `merge`；
* `join`；
* broadcasting。

如果最终仍采用 Python row loop，应有合理原因。

---

## 4.2 `iterrows()` 不是默认批量计算方式

正式批量计算中出现：

```python
for _, row in df.iterrows():
    ...
```

时，代码审查应要求回答：

> 为什么不能通过 vectorization / groupby / merge / batch 处理？

如果没有合理原因，应视为实现质量问题。

---

## 4.3 `apply(axis=1)` 不等于向量化

以下代码：

```python
df.apply(lambda row: ..., axis=1)
```

通常仍是逐行 Python execution。

禁止因为把 `for` 改成 `apply(axis=1)` 就宣称完成性能优化。

---

## 4.4 禁止循环中反复 `concat`

错误：

```python
result = pd.DataFrame()

for item in items:
    part = calculate(item)
    result = pd.concat([result, part])
```

至少应改为：

```python
parts = []

for item in items:
    parts.append(calculate(item))

result = pd.concat(parts)
```

如果业务可以进一步批量化，则继续优先批量计算。

---

## 4.5 避免重复扫描大型 DataFrame

错误：

```python
df[df["valid"]]["amount"].sum()
df[df["valid"]]["volume"].sum()
df[df["valid"]]["close"].mean()
```

应复用：

```python
valid_mask = df["valid"]
valid_df = df.loc[valid_mask]
```

---

## 4.6 避免无意义 `.copy()`

大型 DataFrame `.copy()` 会产生真实内存和时间成本。

只有在以下场景 SHOULD copy：

* 需要 mutation isolation；
* 避免修改 caller 数据；
* lifecycle 明确隔离；
* Pandas ownership/assignment 语义确实需要。

不要因为“保险”在每层都 `.copy()`。

---

## 4.7 dtype 必须有意识

性能敏感路径 SHOULD 关注：

* `object`
* string
* integer
* float
* boolean
* categorical
* datetime

避免大型 pipeline 中无意识反复 dtype conversion。

---

## 4.8 Missing / Zero / None 不应被实现层随意合并

禁止为了代码方便直接：

```python
df = df.fillna(0)
```

除非上位业务合同确认 missing 与 zero 等价。

数值实现必须保留足够信息，让 domain 层能够正确判断。

---

## 4.9 NaN / Inf 必须在计算边界考虑

以下运算必须考虑：

* divide；
* ratio；
* percentage；
* log；
* normalization；
* rolling；
* standardization。

至少检查：

```text
NaN
+Inf
-Inf
zero denominator
```

业务上如何处理由对应 domain contract 决定。

---

## 4.10 浮点比较必须使用合理精度

避免对计算结果直接：

```python
actual == expected
```

应根据数据和业务精度选择：

* tolerance；
* `pytest.approx`；
* NumPy comparison。

---

# 5. 算法复杂度

向量化不能掩盖错误算法复杂度。

对于以下规模会增长的对象：

* 股票数量；
* bars 数量；
* board/member 数量；
* scope 数量；
* 用户数量；

实现时 SHOULD 对明显高成本路径判断：

```text
O(N)
O(N log N)
O(N²)
```

新增 `O(N²)` 或更高复杂度路径时，应说明：

* N 的真实规模；
* 为什么无法采用更低复杂度方案；
* 当前性能是否可接受。

---

## 5.1 Scale-sensitive 路径必须评估物理成本

代码复杂度不仅看算法复杂度，还要算物理操作次数。

对于以下会随输入规模明显放大的路径：

* 全市场 / 大 universe；
* 历史回补；
* `date × symbol`；
* `scope × member`；
* 大批量数据库查询；
* 外部 API / 网络分页；
* Scheduler / Worker 主路径；
* 大文件处理；

在确定正式实现方案前 SHOULD 至少估算：

```text
input cardinality
×
per-item physical operations
=
projected physical cost
```

重点关注：

* DB query count；
* network request count；
* serialization volume；
* memory growth；
* CPU complexity；
* expected runtime。

不要求为普通局部功能建立正式 Performance Contract。

但如果简单规模估算已经证明完整输入下方案明显不可行，MUST NOT 以“小样本可以运行”为理由继续把该方案作为正式实现。

---

# 6. 数据库访问实现规范

本章只规定日常代码访问方式。

Migration、安全和真实数据库操作授权分别由 `rules/80-*` 与 `rules/30-*` 管理。

---

## 6.1 禁止 N+1 Query

错误：

```python
for symbol in symbols:
    row = await repository.get_by_symbol(symbol)
```

当数据可批量获取时，MUST 优先：

* batch query；
* `IN`；
* JOIN；
* CTE；
* bulk fetch。

---

## 6.2 批量写入优先

大量 insert/update SHOULD 使用：

* bulk insert；
* batch operation；
* upsert；
* 单事务批处理。

避免：

```python
for row in rows:
    await session.execute(...)
    await session.commit()
```

---

## 6.3 查询只读取需要字段

性能敏感或大表路径 SHOULD 避免：

```sql
SELECT *
```

如果只需要少量字段，就只读取对应字段。

---

## 6.4 Index 由 Access Pattern 驱动

不得因为字段“看起来重要”就增加 index。

性能问题 SHOULD 经过：

```text
Actual Query
↓
Baseline
↓
EXPLAIN / EXPLAIN ANALYZE
↓
Change
↓
Re-measure
```

Migration 流程仍由 `rules/80-*` 管理。

---

## 6.5 Transaction Boundary 明确

事务 owner SHOULD 位于明确的：

* use-case；
* service；
* repository boundary。

深层 helper SHOULD NOT 无意识：

```python
await session.commit()
```

导致上层无法控制 transaction。

---

## 6.6 Calculation 与 Persistence 分离

默认优先：

```text
Compute
↓
Result
↓
Persist
```

而不是：

```text
Compute 一部分
↓
写 DB
↓
继续 Compute
```

如果业务本身要求 streaming / checkpoint persistence，则按对应 pipeline contract 实现。

---

# 7. Async、并发与 IO

---

## 7.1 Async Function 不得无意识阻塞 Event Loop

`async def` 中 SHOULD NOT 直接执行明显：

* CPU-heavy 同步计算；
* blocking network；
* blocking disk IO；
* 长时间同步 third-party SDK。

必要时应考虑：

* batch worker；
* thread；
* process；
* async adapter；
* 独立 job。

---

## 7.2 独立 IO 应尽量并行

如果 A / B / C 没有依赖关系：

```python
await a()
await b()
await c()
```

应评估是否适合并发。

但不得为了“并发”破坏：

* provider rate limit；
* transaction ordering；
* dependency ordering；
* memory budget。

---

## 7.3 并发必须有上限

禁止默认：

```python
await asyncio.gather(*(call(x) for x in huge_list))
```

对以下资源必须考虑 bounded concurrency：

* DB connection；
* external API；
* browser；
* file IO；
* CPU-heavy work。

---

## 7.4 外部 IO 应有 Timeout

外部网络请求 SHOULD 有明确 timeout。

禁止把无限等待作为默认行为。

具体 retry / liveness / long-task policy 由相关规则拥有。

---

# 8. 性能实现规范

盘迹当前默认处于快速验证阶段。

性能原则：

> **避免明显低效实现；真正优化必须基于测量。**

---

## 8.1 Measure Before Optimize

性能问题处理 SHOULD 遵循：

```text
Baseline
↓
Profile
↓
Locate Hot Path
↓
Form Hypothesis
↓
Change
↓
Benchmark
```

禁止：

* 看见 Python 就假设 Python 是瓶颈；
* 看见 SQL 就加 index；
* 看见 loop 就立即大重构；
* 性能未测量就增加 cache。

---

## 8.2 性能瓶颈必须分类

分析至少区分：

```text
Python CPU
Database
Network
Disk IO
External API
Serialization
Frontend Rendering
```

避免用单一结论解释所有“慢”。

---

## 8.3 循环中的 Invariant 外提

错误：

```python
for member in members:
    total = expensive_scope_total(df)
```

如果 `total` 在整个循环内不变化，应在循环外计算一次。

---

## 8.4 批处理优先

适合批量的：

* DB；
* compute；
* serialization；
* persistence；
* external requests；

应优先 batch。

---

## 8.5 Cache 不是默认性能方案

引入 cache 前应回答：

1. 当前真实瓶颈是什么？
2. cache 是否真实复用？
3. cache key 是什么？
4. freshness 如何定义？
5. invalidation 如何定义？
6. stale 结果是否可接受？
7. cache owner 是谁？

不能回答这些问题时，不应通过 cache 掩盖底层性能问题。

---

## 8.6 性能修复应保留证据

已经确认的性能缺陷修复后 SHOULD 保留至少一种：

* benchmark；
* profiling fixture；
* performance test；
* documented before/after evidence。

具体是否成为正式测试 gate 由 `rules/40-*` 决定。

---

## 8.7 性能敏感路径的观测应由正式实现产生

对于 scale-sensitive 或 performance-sensitive 的正式路径，正式实现 SHOULD 根据其真实物理成本暴露必要的运行指标。可包括：

* physical request count；
* repository query count；
* retry / reconnect count；
* cache hit / miss；
* items processed；
* elapsed time；
* throughput；
* fallback count；
* progress。

对于已经确认属于 scale-sensitive 或 performance-sensitive 的正式路径，性能 evidence SHOULD 尽可能由真实 production owner 自身暴露，而不是由每次实验临时重新统计。

Development、Micro、Canary 和 Full Run SHOULD 尽可能复用同一指标定义。

禁止：

```text
Micro 使用临时 counter A
Canary 使用 counter B
Full Run 再从日志推测 counter C
```

从而形成多个不一致的性能事实来源。

本规则不要求所有普通代码预先建设完整 instrumentation；只有当路径具有真实规模、运行窗口或已确认性能风险时才启用。

---

# 9. External API 实现规范

本章只定义通用程序实现方式。

具体合法数据源及 fallback 业务合同由相关 domain rule / PRD 拥有。

---

## 9.1 External Call 应明确边界

外部调用 SHOULD 考虑：

* timeout；
* bounded concurrency；
* retry；
* error propagation；
* response validation；
* source identity。

---

## 9.2 Retry 必须有限

Retry SHOULD 有：

```text
max attempts
backoff
terminal failure
```

无限 retry 禁止。

Long-running task 的特殊 timeout / progress / stall 规则以 `rules/80-*` 为准。

---

## 9.3 实现层不得静默切换 Source

如果业务允许 fallback，代码必须保留足够信息表达：

```text
actual source
fallback used
reason
freshness
```

是否允许某个 fallback，由业务规则决定，不在本文件定义。

---

# 10. Worker / Job 实现原则

Worker 的正式生命周期、测试和 long-task safety 分别由 `rules/40-*` 和 `rules/80-*` 拥有。

本文件只规定以下通用实现要求。

---

## 10.1 Side Effect 应设计为 Retry-safe

重复执行同一阶段时，不应无意识：

* 重复插入；
* 重复 publish；
* 重复发送；
* 重复累计。

具体幂等合同由对应业务/worker owner 定义。

---

## 10.2 Batch Size 与 Concurrency 有界

大规模 job 不应：

* 一次把所有数据放入内存；
* 无限并发；
* 无限积累 pending task。

应根据真实数据量选择：

* chunk；
* batch；
* bounded worker；
* streaming。

---

## 10.3 不重复计算 Stable Context

同一 batch / scope 中不变化的信息，应在合理层级计算一次并复用。

---

## 10.4 Core 与 Enhancement 的正式边界不在本文件重新定义

任何 Core / Enhancement / readiness / retry 语义均遵循对应：

* PRD；
* `rules/10-*`；
* `rules/20-*`；
* `rules/40-*`；
* `rules/80-*`。

---

# 11. API 代码实现规范

---

## 11.1 Router 保持轻量

Router SHOULD 主要负责：

* parse；
* validate；
* auth；
* service call；
* serialize。

复杂业务逻辑、DataFrame 算法和数据库 orchestration 不应长期放在 Router。

---

## 11.2 API DTO 与内部对象应有明确边界

如果 persistence model、domain model 和 API response 语义不同，SHOULD 明确转换。

避免直接把 ORM object 当作 API domain contract。

---

## 11.3 API Adapter 不得无信息折叠状态

例如内部能够区分：

```text
unavailable
failed
```

则 API adapter 不应为了“简单”都变成：

```json
null
```

是否需要对客户端公开这些状态由 API contract 决定。

本规则只要求：

> Adapter 不得在没有明确合同的情况下丢失语义。

---

## 11.4 大结果集必须有边界意识

高数据量接口 SHOULD 评估：

* pagination；
* filtering；
* limiting；
* aggregation；
* lazy loading。

不要默认一次返回整个大集合。

---

# 12. TypeScript / React 实现规范

前端实际版本和依赖以 `frontend/package.json` 为准。

---

## 12.1 TypeScript 类型必须可信

SHOULD 避免：

```typescript
any
as any
```

确实需要时：

* 局部化；
* 明确边界；
* 尽快通过 validation / narrowing 转换为真实类型。

---

## 12.2 Server State、Shared Client State、Local State 分离

默认原则：

```text
Server State
→ React Query / API data owner

Shared Client State
→ Zustand

Local Interaction State
→ React component state
```

不得无意义复制 server state 到 Zustand。

---

## 12.3 Local State 保持 Local

仅影响单个局部交互的：

* open/close；
* hover；
* input；
* local selection；
* temporary UI state；

SHOULD NOT 升级为 global state。

---

## 12.4 前端不得成为第二业务计算 owner

前端可以：

* formatting；
* display sorting；
* filtering for view；
* text mapping；
* chart mapping；
* ViewModel derivation。

前端不得重新实现后端 Canonical business computation。

正式禁止边界以 `rules/20-market-data-computation.md` 为准。

---

# 13. React 性能实现规范

---

## 13.1 避免 Request Waterfall

相互独立的请求 SHOULD 并行，而不是无理由：

```text
A
↓
B
↓
C
```

---

## 13.2 避免重复请求同一事实

同一页面多个组件需要同一服务器数据时，应复用相同 query/cache owner。

不要每个 component 独立请求同一 endpoint。

---

## 13.3 State Subscription 靠近消费者

避免页面根节点订阅大量高频变化状态，导致整个页面无意义 rerender。

行情、chart、table、watchlist 等路径应特别关注。

---

## 13.4 不机械使用 Memoization

不要因为“性能最佳实践”就到处添加：

```typescript
useMemo
useCallback
React.memo
```

只有当：

* 计算昂贵；
* reference stability 有真实作用；
* rerender 成本明确；

才使用。

---

## 13.5 大列表考虑 DOM 成本

数据量明显扩大时 SHOULD 评估：

* pagination；
* virtualization；
* incremental rendering；
* aggregation。

---

# 14. React Component 设计

组件拆分依据：

> **职责和变化原因。**

而不是固定行数。

优先：

```text
Page
 ↓
Feature
 ↓
Component
 ↓
Hook / Service
```

避免两种极端：

### 巨型组件

一个组件同时承担：

* API；
* state；
* business mapping；
* chart；
* modal；
* table；
* navigation。

### 过度碎片化

为两三行 JSX 建立大量没有业务含义的小组件。

---

# 15. 测试代码实现规范

> 本章只规定测试代码**怎么写**。
>
> 测试应运行哪些层级、何时需要 PG/Runtime/Frontend evidence，以 `rules/40-testing-quality.md` 为准。

---

## 15.1 测试调用真实 Production Implementation

测试某 production function 时，必须调用真正 production implementation。

禁止为了测试再建立：

```python
calculate_x_for_test()
```

作为另一套算法实现。

---

## 15.2 禁止在测试中复制生产算法

错误：

Production：

```python
def calculate(a, b):
    return complex_formula(a, b)
```

Test：

```python
expected = complex_formula_rewritten_again(a, b)
actual = calculate(a, b)
```

如果两套实现共享同一个错误理解，测试仍会 false-green。

---

## 15.3 Expected Value 必须独立于被测实现

Expected SHOULD 来源于：

* 人工推导；
* fixed fixture；
* Golden Case；
* formal invariant；
* 已人工确认 snapshot；
* 明确业务合同。

禁止：

```python
expected = production_function(data)
actual = production_function(data)
```

---

## 15.4 Fixture 可以复用输入构造

允许共享：

```python
make_test_bars()
make_scope_members()
make_user_fixture()
```

这些负责构造测试输入。

但 fixture helper 不应偷偷重新实现 production business formula 来生成 expected。

---

## 15.5 Mock 不得遮蔽测试目标

如果测试目标就是某个 calculator / service 的真实行为，不能 mock 掉该行为然后宣称测试通过。

Mock 主要用于：

* 外部 network；
* clock；
* irreversible side effect；
* 当前测试范围以外 dependency。

---

## 15.6 测试必须 Deterministic

测试 SHOULD NOT 隐式依赖：

* 当前日期；
* 未固定随机数；
* 不稳定排序；
* 共享真实业务数据库现状；
* 外部网络状态。

除非这些就是被测试对象，并且已被明确控制。

---

## 15.7 测试验证行为，不绑定无关内部实现

优先：

```text
Input
→ Business Output
```

而不是：

```text
private helper 被调用三次
```

除非调用方式本身就是正式 contract。

---

# 16. Logging 与可观测性实现规范

---

## 16.1 正式运行路径不要使用 `print()` 作为日志机制

正式后端路径 SHOULD 使用项目统一 logging。

临时本地实验除外，但不得进入正式业务路径。

---

## 16.2 Logging 要有上下文

关键业务日志 SHOULD 根据场景包含：

```text
trade_date
symbol
scope
job_id
run_id
stage
source
duration_ms
status
```

避免只有：

```text
failed
```

但没有上下文。

---

## 16.3 Unknown Exception 保留 Traceback

捕获未知异常时 SHOULD 保留 traceback，避免日志只有 message 而无法定位根因。

---

## 16.4 性能敏感阶段应能够观察耗时

重要 pipeline SHOULD 能在需要调查时区分：

```text
fetch
compute
persist
external call
serialize
```

等阶段耗时。

本规则不要求为了普通局部任务建立新的 observability framework。

---

# 17. 配置实现规范

安全和 Secret 的正式规则由 `rules/30-*` 拥有。

本章只处理配置结构。

---

## 17.1 区分不同配置类型

实现时应区分：

```text
Environment Config
Runtime Config
Business Parameter
Algorithm Contract
Secret
```

不能为了方便把所有参数都塞入 `.env`。

---

## 17.2 深层业务逻辑避免直接读取环境变量

环境配置 SHOULD 在 application boundary 解析，再通过明确依赖传递到下层。

避免：

```python
def calculate():
    threshold = os.environ["..."]
```

---

## 17.3 关键配置缺失应 Fail Fast

关键启动配置缺失时，应尽早失败。

禁止使用可能导致错误业务行为的静默默认值。

---

# 18. Dependency 规范

新增 dependency 前 SHOULD 回答：

1. 标准库是否已经足够？
2. 项目现有 dependency 是否已有同样能力？
3. 新依赖解决什么真实问题？
4. 是否为几行简单逻辑引入重量级 package？
5. 是否形成两个库同时承担同一种能力？
6. 是否增加明显维护风险？

优先复用现有稳定依赖。

---

# 19. 文件与模块组织

---

## 19.1 `utils/helpers/common` 不是业务垃圾桶

以下文件名不是绝对禁止：

```text
utils.py
helpers.py
common.py
```

但一旦其中出现：

* 明确业务规则；
* 某 domain 专属计算；
* persistence ownership；
* feature-specific behavior；

应迁回对应业务 owner。

---

## 19.2 新代码优先进入真实业务模块

不要为了“方便找到”而建立新的全局共享模块。

---

## 19.3 文件名必须表达职责

正式代码避免：

```text
new.py
temp.py
final.py
final_v2.py
backup.py
test2.py
```

Git 已负责历史版本。

---

# 20. 命名规范

名称应尽量表达真实业务含义。

优先：

```python
valid_member_amount
target_trade_date
completed_bar_count
scope_observation
```

避免：

```python
x
tmp
data2
new_result
final_data
```

短变量适用于非常局部的数学、索引和简短循环上下文。

---

# 21. 注释与 Docstring

注释主要解释：

* WHY；
* BUSINESS CONTRACT；
* NON-OBVIOUS DECISION；
* PERFORMANCE TRADE-OFF；
* TEMPORARY COMPATIBILITY；
* IMPORTANT EXCEPTION。

低价值：

```python
# calculate total
total = amount.sum()
```

高价值：

```python
# Missing auction amount is excluded from the valid denominator.
# It is not equivalent to zero participation.
valid_total = amount.loc[valid_mask].sum()
```

---

# 22. 新旧实现与 Deprecated Path

本文件不维护永久禁止清单；永久禁止项以 `rules/90-deprecated-forbidden.md` 为准。

通用原则：

> 新实现成为正式 owner 后，不应无期限保留旧 production path。

如果 compatibility path 必须暂时存在，应明确：

* 为什么存在；
* 谁还在消费；
* 退出条件。

避免无期限：

```text
_old
_new
_v2
_legacy2
```

平行运行。

---

# 23. 自动化规则

能够低成本机器检查的规则 SHOULD 尽量机器化。

优先顺序：

```text
Static Pattern
→ Linter / Type Checker / Existing Checker

Behavior
→ Unit / Contract Test

Performance
→ Benchmark / Profiling

Business / Architecture Semantics
→ Code Review / Audit
```

当前已有工具应优先复用。

例如：

| 规则                      | 推荐验证方式                      |
| ----------------------- | --------------------------- |
| Python 基础质量             | Ruff                        |
| Python 类型               | Mypy                        |
| Python 行为               | Pytest                      |
| TypeScript 类型           | `tsc`                       |
| 前端 lint                 | ESLint                      |
| 前端格式                    | Prettier                    |
| Frontend contract       | 现有 contract tests           |
| Browser behavior        | Playwright                  |
| DataFrame 性能            | benchmark / profiling       |
| N+1                     | code review / profiling     |
| Canonical owner         | architecture / domain audit |
| 测试复制 production formula | code review                 |

### 23.1 自动化方向不等于治理修改授权

本节只规定自动化方向。

它**不构成**以下修改的授权：

* `AGENTS.md`
* `rules/`
* governance checker
* governance test
* protected governance files

新增或修改治理 checker 仍必须遵循 `rules/00-core-governance.md` 的治理授权门。

---

# 24. 例外规则

SHOULD / SHOULD NOT 规则存在合理例外。

例外应说明：

```text
RULE
CONTEXT
WHY DEFAULT DOES NOT APPLY
ALTERNATIVE
RISK
```

对于本文件自身的通用实现规则，确有充分技术理由时可以例外。

但本节 **不得用于覆盖**：

* `AGENTS.md` 基础安全边界；
* PRD 已确认业务合同；
* `rules/10-*` 产品域不变量；
* `rules/20-*` 数据与 Canonical 不变量；
* `rules/30-*` 安全与真实数据保护；
* `rules/40-*` 测试和证据纪律；
* `rules/50-*` Git 安全边界；
* `rules/60-*` Runtime / Frontend 闭环；
* `rules/70-*` Hardening；
* `rules/80-*` Deployment / Migration；
* `rules/90-*` 永久禁止项。

如需改变上述合同，应走对应正式治理和授权流程。

---

# 25. Implementation Complete

本文件只定义：

> **代码实现阶段什么时候算完成。**

至少满足：

1. 已确认现有 owner；
2. 未无意创建第二 production implementation；
3. 当前代码职责和依赖方向合理；
4. 新增/修改实现符合本规范；
5. 明显性能反模式已避免；
6. 对应静态检查可以运行；
7. 测试代码可以验证真实 production implementation；
8. 实现已准备进入 `rules/40-*` 和 `rules/60-*` 要求的验证阶段。

`Implementation Complete` 不等于整个任务 Done。

完整任务完成标准仍由：

* 当前 Goal / Hypothesis；
* PRD；
* `AGENTS.md`；
* `rules/40-*`；
* `rules/60-*`；
* 当前授权范围

共同决定。

---

# 26. 快速 Code Review 清单

## Python

```text
□ 是否复用了正确 owner？
□ executable script 是否有明确 main entry？
□ import 是否存在外部副作用？
□ function 是否混合太多职责？
□ 是否存在 mutable default？
□ 是否存在无意义 magic number？
□ 是否吞异常？
□ 是否存在不受控 resource lifecycle？
```

## Data / Compute

```text
□ 可向量化逻辑是否仍在逐行循环？
□ 是否使用 iterrows？
□ apply(axis=1) 是否被错误当成向量化？
□ 是否 loop 内 concat？
□ 是否重复扫描大型 DataFrame？
□ 是否存在无意义 copy？
□ dtype 是否反复转换？
□ missing/zero 是否被实现层错误合并？
□ 是否存在明显 O(N²)？
□ loop invariant 是否重复计算？
```

## Database

```text
□ 是否 N+1？
□ 是否逐行 commit？
□ 是否读取多余字段？
□ transaction owner 是否清晰？
□ calculation 与 persistence 是否混合？
□ 性能修改是否有 query/profile evidence？
```

## Async / IO

```text
□ async 内是否存在 blocking operation？
□ 独立 IO 是否无理由串行？
□ concurrency 是否有上限？
□ external call 是否有 timeout？
```

## Frontend

```text
□ Server State 是否被复制进 Zustand？
□ Local State 是否错误提升为 Global？
□ 是否重复请求同一 server fact？
□ 是否创建前端第二业务事实源？
□ 是否存在明显 request waterfall？
□ 是否机械 memoization？
□ 大列表是否考虑 rendering cost？
□ TypeScript 是否大量 any？
```

## Tests

```text
□ 是否调用真实 production implementation？
□ 是否在测试中复制 production formula？
□ Expected 是否独立产生？
□ Mock 是否遮蔽了测试目标？
□ 测试是否 deterministic？
□ 是否过度绑定 private implementation detail？
```

---

# 27. 最终原则

盘迹实现层长期遵循：

```text
先找 Owner，再新增代码

第一次问题先局部解决，不提前造框架

语义一致才能复用，不为形式 DRY

计算与副作用尽量分离

显式依赖优于隐藏上下文

能批量就不逐项

能向量化就不逐行

apply(axis=1) 不等于真正向量化

避免 N+1 和逐行数据库写入

并发必须有界

性能必须先测量再优化

Cache 不能代替根因分析

Server State、Shared Client State、Local State 分层管理

前端消费业务事实，不创造第二业务事实源

测试验证生产实现，不复制生产实现

Expected 不能由被测实现自己生成

失败状态不得在实现层静默丢失

当前任务只解决当前 Slice

历史工程债不自动扩大 Exploration Scope
```

本规范的目标不是让代码形式更复杂，而是让盘迹随着：

* 模块增加；
* 数据量扩大；
* 开发轮次增加；
* 执行主体增加；

仍然保持：

> **正确、清晰、高效、可测试、可追踪，并且不会因为快速迭代不断产生第二实现和隐性性能债。**
