# 周升荣模块交接说明

本文记录周升荣模块的当前可接入边界，不代表 D2/M1 已验收。

## 已可使用

- `minidb.core.records`: `Value`/`Row`、`RowMovement`、`RowUpdate`、`UpdateBatch(items=...)`、`WriteKind`。
- `minidb.core.result`: `ResultCursor`、`CommandResult`、`StatementResult`。
- `minidb.engine.executor.Executor.execute_read`: 惰性 SELECT 游标；耗尽、显式关闭和异常都会关闭底层扫描。
- `Executor._collect_update_batch`: 只读收集 UPDATE 候选；所有赋值基于同一份旧行求值，返回 `UpdateBatch`。
- `minidb.engine.write_contract.PreparedWrite` 和 `validate_prepared`: 公共字段互斥、正式 `IndexDef/PendingIndexDef` 及 token 身份合同。
- `ValidatedWriteToken` 只在 `minidb.core.records` 定义；`ConstraintValidator` 直接使用该类型签发，不再保留第二个同名类。
- v2 `StorageEngine` 可注入同一个 `TransactionGuard`，初始化 page 1/page 2，用户页从 page 3 分配，并提供 TransactionManager 所需的对象身份及资源关闭接口。
- `StorageEngine.insert_row/update_rows/delete_rows` 在 ACTIVE 下要求 token；整批目标、旧值、generation、编码大小和页容量预检后才提交页面副本，返回实际 `RowMovement`。变长记录可迁移并回收空页。

## 接入顺序

1. Session 创建 v2 文件、guard、StorageEngine 后，调用 `bind_write_authorizer` 和 `bind_catalog_services`；两个绑定都只能发生一次。
2. Session 在 apply 入口核对并一次性消费 PreparedWrite/token，在 apply 生命周期内让 StorageEngine 的对象身份授权回调返回 True，结束后立即撤销。
3. Catalog 的 `write_catalog_rows` 适配器必须使用当前目录 token 调用 StorageEngine；`validate_index_root` 由正式 IndexManager 提供，StorageEngine 不复制索引校验。
4. TransactionManager 接入后，Executor 的 prepare/apply 才能调用批量写；apply 必须处于 ACTIVE，任一物理写或索引写失败统一回滚。
5. M1 通过后再实现 B+ 树/IndexManager，随后把 `ResultCursor` 接入 Session.iter_results 和 CLI。

## 注意事项

- 当前正式 RowCodec 仍是 v1 的 INT/VARCHAR；`Value`/Schema 已按 v2 合同扩展，但不能据此声称 v2 行编码已完成。
- `_issue_validated_write_token` 是 `core.records` 的包内签发钩子，仅供 ConstraintValidator 和合同测试使用，Executor 和应用代码禁止调用。
- `PreparedWrite` 校验不包含主键、UNIQUE、DEFAULT、NULL、索引键和事务状态规则；这些不能复制到 engine。
- 旧 `Executor.execute` 仍返回物化 `QueryResult`，生产 CLI 尚未切到流式入口。
- 旧 Session 仍调用 v1 `FileManager.open`、三参数 StorageEngine 和直接 `storage.sync()`；不能把 StorageEngine 已有 v2 能力误写为正式 CLI 已接通。
- 当前 FileManager 只有“打开既有 v2 文件”的 `open_locked`，还没有由 Session 使用的全新 v2 文件发布流程；专项测试是显式创建固定三页镜像后再打开。
- ConstraintValidator 目前只为 INSERT/UPDATE 签发 token；DELETE、CREATE TABLE、CREATE INDEX 的统一签发入口仍需接口负责人补齐。
- IndexManager/IndexPage 尚未形成可装配的生产对象，因此 `validate_index_root`、ConstraintLookup 和 `apply_movements` 仍然阻塞。
- 共享 `tests/fixtures/contracts.py` 仍用 page 2 作为用户根页；修复时必须同步迁移旧 v1 测试，不能只改一个数字。
- 新增测试覆盖真实 v2 文件上的保留页、guard、token、批量更新迁移和删除回收，但不替代 Session、索引和事务失败回滚验收。

## 验证命令

```text
python -m unittest discover -s tests -q
python -m compileall -q minidb tests
git diff --check
```
