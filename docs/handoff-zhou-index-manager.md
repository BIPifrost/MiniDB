# 周升荣 D3 索引模块接线说明

基线：`1200247` 之后的未提交工作。上位合同为
`data/MiniDB优化方案与分工计划.md`。

## 已提供接口

```python
IndexManager(buffer_pool, storage, catalog, guard)
IndexManager.reserve_anchor() -> int
IndexManager.create(index, entries) -> None
IndexManager.search(index, bounds) -> IndexCursor
IndexManager.probe(index, key) -> IndexCursor
IndexManager.apply_movements(indexes, movements) -> None
IndexManager.validate(index) -> IndexCheckReport
IndexManager.validate_index_root(index, table) -> None
IndexManager.check_indexes(table=None) -> tuple[IndexCheckReport, ...]
IndexManager.reload() -> None
```

`reserve_anchor()` 只在 ACTIVE 中申请稳定锚页号。Catalog 生成包含该页号的
`IndexDef` 后，再调用 `create()` 初始化锚页、空叶根并写入已排序候选。

`search()` 和 `probe()` 返回可关闭游标。调用方提前停止时必须 `close()`；游标会
登记到 StorageEngine，未关闭时 `TransactionManager.begin_statement()` 和存储写入
都会因活动扫描而拒绝。

`apply_movements()` 接受 StorageEngine 返回的正式 `RowMovement`。它先检查整个
批次，再统一先插入 new 侧、后删除 old 侧；调用方仍须保证它和表页修改处于同一
ACTIVE 文件快照事务。

## Session 需要完成的接线

1. 按总计划创建 `StorageEngine -> CatalogManager -> IndexManager -> TransactionManager`。
2. `ExecutionContext` 第三个参数传同一个 `IndexManager`。
3. `StorageEngine.bind_catalog_services(..., validate_index_root=indexes.validate_index_root)`。
4. ConstraintValidator 的 `ConstraintLookup` 直接使用 `indexes.probe`。
5. 表页批量写返回 movements 后、提交前调用 `indexes.apply_movements(...)`。
6. rollback 继续由 TransactionManager 依次 reload Catalog 和 IndexManager。

## 仍未完成

- Session 的 v2 新库创建、token 一次性消费和正式 prepare/apply。
- CREATE TABLE 自动主键/UNIQUE 索引以及 CREATE INDEX 的事务编排。
- 使用真实 FileManager/BufferPool 的索引提交、回滚、关闭重开端到端测试。
- 共享 fixture 仍使用用户 page 2，导致全量测试 26 个既有集成错误。

索引页字节格式仍由 `minidb/storage/index_page.py` 唯一维护；不要在接线层复制
IndexPage、IndexKeyCodec、ConstraintValidator、Catalog 或 TransactionManager。
