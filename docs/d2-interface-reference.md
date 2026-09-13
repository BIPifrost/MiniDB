# MiniDB D2 接口参考

版本：2026-09-13
适用范围：`StorageEngine`、`BufferPool`、执行器和后续索引管理模块

这份文档冻结 D2 开始使用的公共数据结构和调用顺序。接口只描述职责和
失败语义，不把事务回滚、索引页格式或 RowCodec 规则复制到调用方。

## 1. 页快照

```python
@dataclass(frozen=True, slots=True)
class PageSnapshot:
    page_id: int
    data: bytes       # 恰好 4096 字节，不可变
    revision: int     # 进程内版本号，不写入磁盘

BufferPool.get_snapshot(page_id: int) -> PageSnapshot
BufferPool.write_if_current(snapshot: PageSnapshot, data: bytes) -> None
BufferPool.invalidate_all() -> None
```

规则：

- `get_snapshot` 只产生一次真实页读取统计，并返回该时刻的不可变副本。
- `write_if_current` 先比较 `snapshot.revision`；版本不一致时报
  `STALE_PAGE`，不淘汰页面、不写盘、不改变缓存统计。
- 成功版本写会产生新 revision。页面释放、重新分配和
  `invalidate_all` 都会使旧快照失效。
- `invalidate_all` 只丢弃缓存，不刷新脏页，供恢复路径使用。
- 普通业务读改写不得组合 `get_page` 和 `write_page` 绕过版本检查；
  `write_page` 只用于初始化和兼容的受控路径。

## 2. 事务状态守卫

```python
class TransactionState(Enum):
    BOOTSTRAP
    READ_ONLY_STARTUP
    IDLE
    ACTIVE
    COMMITTING
    ROLLING_BACK
    FAILED
    CLOSED

TransactionGuard.require(*allowed, operation: str) -> None
TransactionGuard.transition(expected, target, operation: str) -> None
TransactionGuard.fail(operation: str) -> None
TransactionGuard.close(operation: str = ...) -> None
```

`TransactionGuard` 只负责状态和代际，不执行页写入或回滚。推荐状态流：

```text
BOOTSTRAP/READ_ONLY_STARTUP -> IDLE -> ACTIVE
ACTIVE -> COMMITTING -> IDLE
ACTIVE -> ROLLING_BACK -> IDLE
任意可继续状态 -> FAILED -> CLOSED
```

写页、目录登记、索引同步必须在 `ACTIVE`（新库初始化例外为
`BOOTSTRAP`）；`FAILED`、`CLOSED` 不允许继续业务操作。真正的
`TransactionManager` 可以替换状态转换实现，但必须保留这些状态名和
`generation` 递增规则。

## 3. 行身份和批量变更

```python
class WriteKind(Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

@dataclass(frozen=True, slots=True)
class RowMovement:
    kind: WriteKind
    old: RowId | None
    new: RowId | None

@dataclass(frozen=True, slots=True)
class RowUpdate:
    old: StoredRow
    new_values: Row

@dataclass(frozen=True, slots=True)
class UpdateBatch:
    updates: tuple[RowUpdate, ...]
```

`RowMovement` 是表页和索引之间唯一的物理变化输入：INSERT 只有 `new`，
DELETE 只有 `old`，UPDATE 两者都有。`RowUpdate.old` 必须来自同一准备阶段，
应用阶段要再次核对 generation 和旧值。空 `UpdateBatch` 合法，必须返回空
movement 且不产生页写入。

## 4. UPDATE 计划

```python
@dataclass(frozen=True, slots=True)
class BoundAssignment:
    column_index: int
    value: BoundExpr
    span: SourceSpan

@dataclass(frozen=True, slots=True)
class UpdatePlan:
    table: TableDef
    child: SeqScanPlan | FilterPlan
    assignments: tuple[BoundAssignment, ...]
    span: SourceSpan
```

计划校验要求：`child` 与 `table` 内容一致；赋值集合非空；目标列索引在
Schema 范围内且不重复；赋值表达式已经绑定。执行器必须对每一条旧行先计算
完整的新行，再统一交给约束校验和 `StorageEngine.update_rows`，因此
`SET a=b, b=a` 使用同一份旧行，可以正确交换。

## 5. 后续待接入接口

以下接口已在计划中冻结语义，但当前实现仍需接入对应模块：

```python
StorageEngine.update_rows(
    table: TableDef,
    batch: UpdateBatch,
    token: ValidatedWriteToken,
) -> tuple[RowMovement, ...]

StorageEngine.delete_rows(
    table: TableDef,
    expected: tuple[StoredRow, ...],
    token: ValidatedWriteToken,
) -> tuple[RowMovement, ...]

StorageEngine.flush_for_commit() -> None
StorageEngine.close_after_commit() -> None
StorageEngine.abort_resources() -> None
```

这些方法必须使用 `PageSnapshot/write_if_current`，不能在没有事务 token 的
情况下修改正式表。`ValidatedWriteToken` 的签发和约束检查由
`ConstraintValidator`/`TransactionManager` 负责，StorageEngine 只核对 token
身份、目录代际、旧行和物理空间条件。

## 6. 错误合同

- 过期页快照：`STALE_PAGE`。
- 过期或已删除行身份：`STALE_ROW`。
- 不属于目标表的页或非法计划：`INVALID_ARGUMENT` / `INVALID_PLAN`。
- 所有错误都必须在发生副作用前抛出；错误后的会话由上层转入 `FAILED`，
  不再报告成功同步。

## 7. 当前实现状态

已落地：`PageSnapshot`、版本写、缓存失效、`TransactionGuard`、
`RowMovement`、`RowUpdate`、`UpdateBatch`、`BoundAssignment` 和
`UpdatePlan` 结构校验。

尚未落地：真正的 TransactionManager、ValidatedWriteToken 签发、批量
UPDATE/DELETE 的页修改、RowMovement 索引同步。这些必须在事务管理和目录
代际接口接入后实现，不能用无版本读改写代替。
