# 周升荣模块交接说明

本次提交是可运行的 WIP 合同草案，不代表 D2/M1 已验收。

## 已可使用

- `minidb.core.records`: `Value`/`Row`、`RowMovement`、`RowUpdate`、`UpdateBatch(items=...)`、`WriteKind`。
- `minidb.core.result`: `ResultCursor`、`CommandResult`、`StatementResult`。
- `minidb.engine.executor.Executor.execute_read`: 惰性 SELECT 游标；耗尽、显式关闭和异常都会关闭底层扫描。
- `Executor._collect_update_batch`: 只读收集 UPDATE 候选；所有赋值基于同一份旧行求值，返回 `UpdateBatch`。
- `minidb.engine.write_contract.PreparedWrite` 和 `validate_prepared`: 公共字段互斥及 token 身份合同。

## 接入顺序

1. Catalog 负责人落地正式 `IndexDef/PendingIndexDef`，再将其导入 `write_contract.py`；不要在 engine 中复制定义。
2. Validator/Session 负责人把 `_issue_validated_write_token` 替换为 Session 持有的私有工厂，并增加 token 登记、目录代际校验和一次性消费。
3. TransactionManager 接入后，Executor 的 prepare/apply 才能调用 StorageEngine 批量写；apply 必须处于 ACTIVE，失败统一回滚。
4. StorageEngine 批量 insert/update/delete 完成后，再把 `ResultCursor` 接入 Session.iter_results 和 CLI。

## 注意事项

- 当前正式 Schema/RowCodec 仍是 v1 的 INT/VARCHAR；`Value` 已按 v2 合同扩展，但不能据此声称 v2 编码已完成。
- `_issue_validated_write_token` 仅供合同测试和未来 Validator 过渡使用，Executor 和应用代码禁止调用。
- `PreparedWrite` 校验不包含主键、UNIQUE、DEFAULT、NULL、索引键和事务状态规则；这些不能复制到 engine。
- 旧 `Executor.execute` 仍返回物化 `QueryResult`，生产 CLI 尚未切到流式入口。
- 所有新增测试均为合同/执行器测试，不替代真实 v2 文件、索引和事务恢复验收。

## 验证命令

```text
python -m unittest discover -s tests -q
python -m compileall -q minidb tests
git diff --check
```
