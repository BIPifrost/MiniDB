"""MiniDB 会话调度。

Session 是 CLI 与各层服务之间的装配边界：它不重新解析 SQL，也不实现
语义、计划或页存储逻辑，只按约定串联 Lexer、Parser、Semantic、Planner、
Optimizer 和 Executor，并负责存储生命周期。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

from minidb.catalog.catalog_manager import CatalogManager
from minidb.compiler.lexer import Lexer
from minidb.compiler.optimizer import Optimizer
from minidb.compiler.parser import Parser
from minidb.compiler.planner import Planner
from minidb.compiler.semantic import Semantic
from minidb.core.diagnostics import SyntaxCheckResult
from minidb.core.errors import (
    ACTIVE_SCAN,
    DbError,
    ErrorStage,
    CLOSED,
    INPUT_INVALID_UTF8,
    INPUT_READ_FAILED,
    INTERNAL_ERROR,
    INVALID_ARGUMENT,
    RESOURCE_LIMIT,
)
from minidb.core.result import QueryResult, ResultCursor, StatementResult
from minidb.core.records import StoredRow, _issue_validated_write_token
from minidb.core.source import SourceText
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_manager import FileManager
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.recovery import RecoveryManager
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from minidb.storage.index_manager import IndexManager
from minidb.storage.transaction import TransactionManager
from minidb.compiler.plan import DeletePlan, ExplainPlan, ProjectPlan, InsertPlan, UpdatePlan


# 兼容物化入口的上限（工作计划 7.4）：生产 CLI 只走 iter_results。
MATERIALIZE_MAX_ROWS = 10_000
MATERIALIZE_MAX_BYTES = 16 * 1024 * 1024


class Session:
    """一个数据库文件对应的单线程、单会话执行上下文。"""

    def __init__(
        self,
        catalog: CatalogManager,
        storage: StorageEngine,
        *,
        optimize: bool = False,
        lexer: Lexer | None = None,
        parser: Parser | None = None,
        semantic: Semantic | None = None,
        planner: Planner | None = None,
        optimizer: Optimizer | None = None,
        executor: Executor | None = None,
        trace_sink: Callable[[dict[str, object]], None] | None = None,
        file_manager: FileManager | None = None,
        buffer_pool: BufferPool | None = None,
        guard: TransactionGuard | None = None,
        index_manager: IndexManager | None = None,
        transaction_manager: TransactionManager | None = None,
    ) -> None:
        if type(optimize) is not bool:
            raise DbError(
                ErrorStage.EXECUTION,
                INVALID_ARGUMENT,
                "optimize 必须是 bool",
                context={
                    "operation": "Session.__init__",
                    "field": "optimize",
                    "expected": "bool",
                    "actual": type(optimize).__name__,
                },
            )
        self.catalog = catalog
        self.storage = storage
        self.optimize = optimize
        self.lexer = lexer or Lexer()
        self.parser = parser or Parser()
        self.semantic = semantic or Semantic()
        self.planner = planner or Planner()
        self.optimizer = optimizer or Optimizer()
        self.executor = executor or Executor()
        self.trace_sink = trace_sink
        self.file_manager = file_manager
        self.buffer_pool = buffer_pool
        self.guard = guard
        self.index_manager = index_manager
        self.transaction_manager = transaction_manager
        self.session_id = uuid4()
        self._validated_tokens: set[object] = set()
        self._active_cursor: ResultCursor | None = None
        self._current_prepared_id: UUID | None = None
        self._closed = False

    @classmethod
    def open(
        cls,
        path: str,
        *,
        buffer_pages: int = 16,
        policy: str = "lru",
        optimize: bool = False,
    ) -> "Session":
        """打开或创建数据库，并初始化真实目录与存储组件。"""
        file_manager: FileManager | None = None
        lock_handle: DatabaseLock | None = None
        storage: StorageEngine | None = None
        transaction_manager: TransactionManager | None = None
        try:
            actual = os.path.normcase(os.path.realpath(os.path.abspath(path)))
            # v2 空库必须通过无覆盖发布创建，不能让 DatabaseLock 先生成空文件。
            if not os.path.exists(actual):
                FileManager.create_v2(actual)
            lock_handle = DatabaseLock.acquire(actual)
            RecoveryManager.inspect_and_recover(actual, lock_handle)
            guard = TransactionGuard(TransactionState.READ_ONLY_STARTUP)
            file_manager = FileManager.open_locked(actual, lock_handle, guard=guard)
            buffer_pool = BufferPool(file_manager, capacity=buffer_pages, policy=policy)
            storage = StorageEngine(buffer_pool, RowCodec(), file_manager, guard)

            # CatalogManager 在读取目录时需要索引根校验，但 IndexManager 又依赖
            # 已加载目录。启动阶段先登记待校验项，目录加载完成后由 IndexManager
            # 统一严格复核，避免构造第二套目录对象。
            pending_indexes = []
            index_holder: dict[str, IndexManager | None] = {"manager": None}

            def validate_index_root(index, table):
                manager = index_holder["manager"]
                if manager is None:
                    pending_indexes.append((index, table))
                    return
                manager.validate_index_root(index, table)

            session_holder: dict[str, Session | None] = {"session": None}

            def write_catalog_rows(table, rows):
                session = session_holder["session"]
                if session is None:
                    raise DbError(
                        ErrorStage.STORAGE,
                        INTERNAL_ERROR,
                        "目录写入适配器尚未完成 Session 装配",
                        context={"operation": "Session.write_catalog_rows"},
                    )
                return session._write_catalog_rows(table, rows)

            storage.bind_catalog_services(
                write_catalog_rows=write_catalog_rows,
                validate_index_root=validate_index_root,
            )
            catalog = CatalogManager.bootstrap_or_load(storage, False)
            index_manager = IndexManager(buffer_pool, storage, catalog, guard)
            index_holder["manager"] = index_manager
            index_manager.reload()
            pending_indexes.clear()

            session = cls(
                catalog,
                storage,
                optimize=optimize,
                file_manager=file_manager,
                buffer_pool=buffer_pool,
                guard=guard,
                index_manager=index_manager,
            )
            session_holder["session"] = session
            storage.bind_write_authorizer(
                session_id=session.session_id,
                catalog_generation=lambda: catalog.generation,
                token_is_authorized=session._token_is_authorized,
            )
            transaction_manager = TransactionManager(
                file_manager,
                buffer_pool,
                storage,
                catalog,
                index_manager,
                lock_handle,
                guard,
                invalidate_prepared=session._invalidate_prepared,
            )
            session.transaction_manager = transaction_manager
            guard.transition_to(TransactionState.IDLE, operation="Session.open")
            return session
        except BaseException as error:
            if transaction_manager is not None:
                try:
                    transaction_manager.close()
                except BaseException as cleanup:
                    if isinstance(error, DbError):
                        error._update_context(cleanup_errors=[{"message": str(cleanup)}])
            elif storage is not None:
                _abort_quietly(storage, error)
            elif file_manager is not None:
                _close_file_quietly(file_manager, error)
            elif lock_handle is not None:
                try:
                    lock_handle.close()
                except BaseException:
                    pass
            raise

    @staticmethod
    def check_syntax(text: str, *, source_name: str = "<syntax-check>") -> SyntaxCheckResult:
        """只做词法和语法检查，不打开数据库、不访问目录。"""
        source = _source_text(text, source_name, "Session.check_syntax")
        return Parser().check_syntax(Lexer().scan(source))

    def execute_text(
        self,
        text: str,
        *,
        source_name: str = "<stdin>",
        trace_sink: Callable[[dict[str, object]], None] | None = None,
        materialize: bool = False,
    ) -> list[QueryResult]:
        """测试兼容入口：按语句执行并物化结果；遇到错误立即停止当前输入。

        生产 CLI 必须使用 ``iter_results``。SELECT 默认拒绝物化，只有显式
        ``materialize=True`` 才允许，并受 ``MATERIALIZE_MAX_ROWS`` /
        ``MATERIALIZE_MAX_BYTES`` 上限约束（工作计划 7.4）。
        """
        if type(materialize) is not bool:
            raise DbError(
                ErrorStage.EXECUTION,
                INVALID_ARGUMENT,
                "materialize 必须是 bool",
                context={
                    "operation": "Session.execute_text",
                    "field": "materialize",
                    "expected": "bool",
                    "actual": type(materialize).__name__,
                },
            )
        self._ensure_open("execute_text")
        self._ensure_no_active_cursor("execute_text")
        source = _source_text(text, source_name, "Session.execute_text")
        results: list[QueryResult] = []
        sink = trace_sink if trace_sink is not None else self.trace_sink
        consumed_tokens = []
        token_start = 0
        statement_index = 0

        def token_stream():
            for token in self.lexer.scan(source):
                consumed_tokens.append(token)
                yield token

        try:
            statements = self.parser.iter_statements(token_stream())
            for statement in statements:
                statement_index += 1
                if sink is not None:
                    sink({
                        "statement_index": statement_index,
                        "stage": "TOKEN",
                        # 当前语句已经消费到分号；不把整个输入后续 Token
                        # 或最终 EOF 混入本条 TOKEN 事件。
                        "data": tuple(consumed_tokens[token_start:]),
                    })
                    sink({
                        "statement_index": statement_index,
                        "stage": "AST",
                        "data": statement,
                    })
                token_start = len(consumed_tokens)
                bound = self.semantic.analyze(statement, self.catalog)
                if sink is not None:
                    sink({
                        "statement_index": statement_index,
                        "stage": "SEMANTIC",
                        "data": bound,
                    })
                original_plan = self.planner.build(bound)
                if sink is not None:
                    sink({
                        "statement_index": statement_index,
                        "stage": "PLAN",
                        "data": original_plan,
                    })
                indexes = self._indexes_for_plan(original_plan)
                plan = self.optimizer.optimize(original_plan, indexes) if self.optimize else original_plan
                if sink is not None:
                    sink({
                        "statement_index": statement_index,
                        "stage": "OPTIMIZED_PLAN",
                        "data": {"enabled": self.optimize, "plan": plan},
                    })
                context = ExecutionContext(self.catalog, self.storage, self.index_manager)
                in_transaction = self._begin_if_needed(plan)
                if isinstance(plan, ProjectPlan):
                    result = self._materialize_select(plan, context, materialize)
                else:
                    result = self._execute_plan(plan, context)
                    if in_transaction:
                        self.transaction_manager.commit()
                        self._current_prepared_id = None
                if result.affected_rows is not None:
                    # CREATE、INSERT、DELETE 的成功结果只有在 sync 成功后才可返回给 CLI。
                    if self.transaction_manager is None:
                        self.storage.sync()
                results.append(result)
            return results
        except DbError as error:
            if sink is not None:
                sink({
                    "statement_index": statement_index or 1,
                    "stage": "ERROR",
                    "data": error,
                })
            if self.transaction_manager is not None:
                self._rollback_quietly(error)
            elif error.stage is ErrorStage.STORAGE:
                _abort_quietly(self.storage, error)
                self._closed = True
            raise
        except BaseException as error:
            wrapped = DbError(
                ErrorStage.EXECUTION,
                INTERNAL_ERROR,
                "会话执行时发生未预期错误",
                getattr(error, "span", None),
                {
                    "operation": "Session.execute_text",
                    "exception_type": type(error).__name__,
                },
            )
            if sink is not None:
                sink({
                    "statement_index": statement_index or 1,
                    "stage": "ERROR",
                    "data": wrapped,
                })
            if self.transaction_manager is not None:
                self._rollback_quietly(wrapped)
            else:
                _abort_quietly(self.storage, wrapped)
                self._closed = True
            raise wrapped from error

    def execute_file(self, path: str, *, materialize: bool = False) -> list[QueryResult]:
        """以 UTF-8 读取 SQL 文件并执行；文件错误在打开数据库后仍不执行 SQL。"""
        self._ensure_open("execute_file")
        actual_path = os.path.abspath(path) if isinstance(path, str) else path
        try:
            with open(actual_path, "r", encoding="utf-8", newline="") as handle:
                text = handle.read()
        except UnicodeDecodeError as error:
            raise DbError(
                ErrorStage.LEXICAL,
                INPUT_INVALID_UTF8,
                "SQL 文件不是合法 UTF-8",
                context={
                    "operation": "Session.execute_file",
                    "source_name": str(actual_path),
                    "reason": str(error),
                },
            ) from error
        except (OSError, TypeError, ValueError) as error:
            raise DbError(
                ErrorStage.LEXICAL,
                INPUT_READ_FAILED,
                "无法读取 SQL 文件",
                context={
                    "operation": "Session.execute_file",
                    "source_name": str(actual_path),
                    "cause": str(error),
                },
            ) from error
        return self.execute_text(text, source_name=str(actual_path), materialize=materialize)

    def iter_results(
        self,
        text: str,
        *,
        source_name: str = "<stdin>",
        trace_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> Iterator[StatementResult]:
        """按语句返回 QueryResult 或惰性 ResultCursor。

        SELECT 不在 Session 中物化；游标耗尽或显式 close 后才能继续下一条
        语句。调用方若提前停止迭代，必须关闭已经取得的 ResultCursor。

        会话状态和活动游标在调用时即检查，避免同一个 Session 上出现两个
        并发结果流（工作计划 7.4）。
        """
        self._ensure_open("iter_results")
        self._ensure_no_active_cursor("iter_results")
        source = _source_text(text, source_name, "Session.iter_results")
        sink = trace_sink if trace_sink is not None else self.trace_sink
        return self._iter_statements(source, sink)

    def _iter_statements(
        self,
        source: SourceText,
        sink: Callable[[dict[str, object]], None] | None,
    ) -> Iterator[StatementResult]:
        """``iter_results`` 的惰性主体；每条语句独立提交或回滚。"""
        consumed_tokens = []
        token_start = 0
        statement_index = 0

        def token_stream():
            for token in self.lexer.scan(source):
                consumed_tokens.append(token)
                yield token

        statements = self.parser.iter_statements(token_stream())
        for statement in statements:
            statement_index += 1
            if sink is not None:
                sink({"statement_index": statement_index, "stage": "TOKEN",
                      "data": tuple(consumed_tokens[token_start:])})
                sink({"statement_index": statement_index, "stage": "AST", "data": statement})
            token_start = len(consumed_tokens)
            in_transaction = False
            try:
                bound = self.semantic.analyze(statement, self.catalog)
                if sink is not None:
                    sink({"statement_index": statement_index, "stage": "SEMANTIC", "data": bound})
                original_plan = self.planner.build(bound)
                indexes = self._indexes_for_plan(original_plan)
                plan = self.optimizer.optimize(original_plan, indexes) if self.optimize else original_plan
                if sink is not None:
                    sink({"statement_index": statement_index, "stage": "PLAN", "data": original_plan})
                    sink({"statement_index": statement_index, "stage": "OPTIMIZED_PLAN",
                          "data": {"enabled": self.optimize, "plan": plan}})
                context = ExecutionContext(self.catalog, self.storage, self.index_manager)
                if isinstance(plan, ProjectPlan):
                    cursor = self.executor.execute_read(plan, context)
                    self._active_cursor = cursor
                    try:
                        yield cursor
                    finally:
                        try:
                            if not cursor.closed:
                                cursor.close()
                        finally:
                            if self._active_cursor is cursor:
                                self._active_cursor = None
                else:
                    in_transaction = self._begin_if_needed(plan)
                    result = self._execute_plan(plan, context)
                    if in_transaction:
                        self.transaction_manager.commit()
                        self._current_prepared_id = None
                        in_transaction = False
                    if result.affected_rows is not None and self.transaction_manager is None:
                        self.storage.sync()
                    yield result
            except GeneratorExit:
                # 调用方关闭生成器时不能再包一层错误，否则 close() 会失败。
                raise
            except (DbError, KeyboardInterrupt, BrokenPipeError) as error:
                if in_transaction:
                    self._rollback_quietly(error)
                if sink is not None and isinstance(error, DbError):
                    sink({"statement_index": statement_index, "stage": "ERROR", "data": error})
                raise
            except BaseException as error:
                wrapped = DbError(
                    ErrorStage.EXECUTION,
                    INTERNAL_ERROR,
                    "会话执行时发生未预期错误",
                    getattr(error, "span", None),
                    {"operation": "Session.iter_results", "exception_type": type(error).__name__},
                )
                if in_transaction:
                    self._rollback_quietly(wrapped)
                if sink is not None:
                    sink({"statement_index": statement_index, "stage": "ERROR", "data": wrapped})
                raise wrapped from error

    def register_validated_token(self, token: object) -> None:
        """登记 ConstraintValidator 签发的当前会话 token。"""
        if getattr(token, "session_id", None) != self.session_id:
            raise DbError(ErrorStage.STORAGE, INVALID_ARGUMENT, "token 不属于当前 Session",
                          context={"operation": "Session.register_validated_token"})
        self._validated_tokens.add(token)

    def _token_is_authorized(self, token: object) -> bool:
        return token in self._validated_tokens

    def _invalidate_prepared(self) -> None:
        self._validated_tokens.clear()

    def _write_catalog_rows(self, table, rows) -> None:
        if self._current_prepared_id is None:
            raise DbError(ErrorStage.STORAGE, INVALID_ARGUMENT, "目录写入必须处于 ACTIVE 事务",
                          context={"operation": "Session._write_catalog_rows"})
        token = self._issue_token()
        for row in rows:
            self.storage.insert_row(table, row, token)

    def _issue_token(self):
        token = _issue_validated_write_token(
            self.session_id,
            self.catalog.generation,
            self._current_prepared_id,
        )
        self.register_validated_token(token)
        return token

    def _execute_plan(self, plan, context):
        # Executor 的旧 insert 分支尚未携带 v2 token；Session 在事务边界
        # 内适配这一条调用，其他计划仍交给 Executor 保持职责单一。
        if isinstance(plan, InsertPlan) and self.transaction_manager is not None:
            token = self._issue_token()
            self.storage.insert_row(plan.table, plan.row, token)
            return QueryResult(affected_rows=1, message="1 row inserted")
        if isinstance(plan, DeletePlan) and self.transaction_manager is not None:
            stream = self.executor._execute_stream(plan.child, context)
            try:
                expected = tuple(
                    StoredRow(record.row_id, record.values)
                    for record in stream
                    if record.row_id is not None
                )
            finally:
                stream.close()
            if not expected:
                return QueryResult(affected_rows=0, message="0 rows deleted")
            token = self._issue_token()
            movements = self.storage.delete_rows(plan.table, expected, token)
            return QueryResult(affected_rows=len(movements), message=f"{len(movements)} rows deleted")
        if isinstance(plan, UpdatePlan) and self.transaction_manager is not None:
            batch = self.executor._collect_update_batch(plan, context)
            if not batch.items:
                return QueryResult(affected_rows=0, message="0 rows updated")
            token = self._issue_token()
            movements = self.storage.update_rows(plan.table, batch, token)
            return QueryResult(affected_rows=len(movements), message=f"{len(movements)} rows updated")
        return self.executor.execute(plan, context)

    def _begin_if_needed(self, plan) -> bool:
        """Mutating plans run inside one TransactionManager statement."""
        if self.transaction_manager is None or isinstance(plan, ProjectPlan):
            return False
        self._ensure_no_active_cursor("Session._begin_if_needed")
        self._current_prepared_id = self.transaction_manager.begin_statement()
        return True

    def _materialize_select(
        self,
        plan: ProjectPlan,
        context: ExecutionContext,
        materialize: bool,
    ) -> QueryResult:
        """物化 SELECT，并强制 10000 行 / 16 MiB 上限（工作计划 7.4）。

        该入口只服务测试兼容；生产 CLI 走 ``iter_results``，因此这里在
        超限时关闭游标并报 ``RESOURCE_LIMIT``，不返回半截结果。
        """
        if not materialize:
            raise DbError(
                ErrorStage.EXECUTION,
                INVALID_ARGUMENT,
                "SELECT 必须使用 Session.iter_results 流式读取；"
                "测试兼容入口需显式传入 materialize=True",
                plan.span,
                {
                    "operation": "Session.execute_text",
                    "hint": "materialize=True",
                },
            )
        cursor = self.executor.execute_read(plan, context)
        rows: list = []
        total_bytes = 0
        try:
            for row in cursor:
                if len(rows) + 1 > MATERIALIZE_MAX_ROWS:
                    raise DbError(
                        ErrorStage.EXECUTION,
                        RESOURCE_LIMIT,
                        "物化结果超过行数上限",
                        plan.span,
                        {
                            "operation": "Session.execute_text",
                            "kind": "rows",
                            "limit": MATERIALIZE_MAX_ROWS,
                            "actual": len(rows) + 1,
                        },
                    )
                total_bytes += _row_bytes(row)
                if total_bytes > MATERIALIZE_MAX_BYTES:
                    raise DbError(
                        ErrorStage.EXECUTION,
                        RESOURCE_LIMIT,
                        "物化结果超过字节上限",
                        plan.span,
                        {
                            "operation": "Session.execute_text",
                            "kind": "bytes",
                            "limit": MATERIALIZE_MAX_BYTES,
                            "actual": total_bytes,
                        },
                    )
                rows.append(row)
        finally:
            cursor.close()
        return QueryResult(
            columns=list(cursor.columns),
            rows=rows,
            message=f"{len(rows)} rows selected",
        )

    def _rollback_quietly(self, original: BaseException) -> None:
        manager = self.transaction_manager
        if manager is None:
            return
        try:
            if self.guard is not None and self.guard.state is TransactionState.ACTIVE:
                manager.rollback()
        except BaseException as cleanup:
            if isinstance(original, DbError):
                original._update_context(cleanup_errors=[{"message": str(cleanup)}])
        finally:
            self._current_prepared_id = None

    def _indexes_for_plan(self, plan) -> tuple:
        table = getattr(plan, "table", None)
        if table is None and hasattr(plan, "child"):
            return self._indexes_for_plan(plan.child)
        ref = getattr(table, "ref", None)
        table_id = getattr(ref, "table_id", None)
        if isinstance(table_id, int):
            return tuple(self.catalog.indexes_for_table(table_id))
        return ()

    def close(self) -> None:
        """正常同步并关闭会话。"""
        if self._closed:
            return
        if self._active_cursor is not None and not self._active_cursor.closed:
            raise DbError(
                ErrorStage.STORAGE,
                ACTIVE_SCAN,
                "结果游标关闭前不能关闭 Session",
                context={"operation": "Session.close"},
            )
        try:
            if self.transaction_manager is not None:
                self.transaction_manager.close()
            else:
                self.storage.close()
        except BaseException as error:
            if self.transaction_manager is not None:
                try:
                    self.transaction_manager.close()
                except BaseException:
                    pass
            else:
                _abort_quietly(self.storage, error)
            self._closed = True
            raise
        self._closed = True

    def abort(self) -> None:
        """失败路径关闭会话，不刷新脏页。重复调用无副作用。"""
        if self._closed:
            return
        try:
            if self._active_cursor is not None and not self._active_cursor.closed:
                self._active_cursor.close()
            if self.transaction_manager is not None:
                self.transaction_manager.close()
            else:
                self.storage.abort()
        finally:
            self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _ensure_open(self, operation: str) -> None:
        if self._closed:
            raise DbError(
                ErrorStage.STORAGE,
                CLOSED,
                "Session 已关闭",
                context={
                    "operation": operation,
                    "resource": "Session",
                    "state": "closed",
                },
            )

    def _ensure_no_active_cursor(self, operation: str) -> None:
        """活动游标期间禁止下一条语句、写事务和正常关闭（工作计划 7.4）。"""
        cursor = self._active_cursor
        if cursor is not None and not cursor.closed:
            raise DbError(
                ErrorStage.STORAGE,
                ACTIVE_SCAN,
                "必须先关闭上一个结果游标",
                context={
                    "operation": operation,
                    "active_cursor_columns": len(cursor.columns),
                },
            )


def _row_bytes(row) -> int:
    """估算一行结果占用的字节数，用于物化入口的资源上限。

    这里只需要稳定、可重复的近似值：字符串按 UTF-8 长度计，INT/BOOL/
    NULL 用固定宽度，其余类型退化为 ``repr`` 长度。
    """
    total = 0
    for value in row:
        if value is None or type(value) is bool:
            total += 1
        elif type(value) is int:
            total += 8
        elif isinstance(value, str):
            total += len(value.encode("utf-8"))
        else:
            total += len(repr(value).encode("utf-8"))
    return total


def _source_text(text: str, source_name: str, operation: str) -> SourceText:
    if type(text) is not str:
        raise DbError(
            ErrorStage.EXECUTION,
            INVALID_ARGUMENT,
            "SQL 输入必须是字符串",
            context={
                "operation": operation,
                "field": "text",
                "expected": "str",
                "actual": type(text).__name__,
            },
        )
    if type(source_name) is not str:
        raise DbError(
            ErrorStage.EXECUTION,
            INVALID_ARGUMENT,
            "source_name 必须是字符串",
            context={
                "operation": operation,
                "field": "source_name",
                "expected": "str",
                "actual": type(source_name).__name__,
            },
        )
    # 文件输入按约定移除 UTF-8 BOM；其他源码位置保持原始字符不变。
    return SourceText(source_name, text.removeprefix("\ufeff"))


def _abort_quietly(storage: StorageEngine, original: BaseException) -> None:
    try:
        storage.abort()
    except BaseException as cleanup:
        if isinstance(original, DbError):
            original._update_context(cleanup_errors=[{
                "exception_type": type(cleanup).__name__,
                "message": str(cleanup),
            }])
        else:
            original.add_note(f"abort 失败：{cleanup}")


def _close_file_quietly(file_manager: FileManager, original: BaseException) -> None:
    try:
        file_manager.close()
    except BaseException as cleanup:
        if isinstance(original, DbError):
            original._update_context(cleanup_errors=[{
                "exception_type": type(cleanup).__name__,
                "message": str(cleanup),
            }])
        else:
            original.add_note(f"关闭文件失败：{cleanup}")


__all__ = ["MATERIALIZE_MAX_BYTES", "MATERIALIZE_MAX_ROWS", "Session"]
