"""MiniDB 会话调度。

Session 是 CLI 与各层服务之间的装配边界：它不重新解析 SQL，也不实现
语义、计划或页存储逻辑，只按约定串联 Lexer、Parser、Semantic、Planner、
Optimizer 和 Executor，并负责存储生命周期。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from minidb.catalog.catalog_manager import CatalogManager
from minidb.compiler.lexer import Lexer
from minidb.compiler.optimizer import Optimizer
from minidb.compiler.parser import Parser
from minidb.compiler.planner import Planner
from minidb.compiler.semantic import Semantic
from minidb.core.diagnostics import SyntaxCheckResult
from minidb.core.errors import (
    DbError,
    ErrorStage,
    CLOSED,
    INPUT_INVALID_UTF8,
    INPUT_READ_FAILED,
    INTERNAL_ERROR,
    INVALID_ARGUMENT,
)
from minidb.core.result import QueryResult
from minidb.core.source import SourceText
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_manager import FileManager
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine


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
        storage: StorageEngine | None = None
        try:
            file_manager = FileManager.open(path)
            buffer_pool = BufferPool(file_manager, capacity=buffer_pages, policy=policy)
            storage = StorageEngine(buffer_pool, RowCodec(), file_manager)
            catalog = CatalogManager.bootstrap_or_load(storage, file_manager.is_new)
            return cls(catalog, storage, optimize=optimize)
        except BaseException as error:
            if storage is not None:
                _abort_quietly(storage, error)
            elif file_manager is not None:
                _close_file_quietly(file_manager, error)
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
    ) -> list[QueryResult]:
        """按语句惰性执行一段 SQL；遇到错误立即停止当前输入。"""
        self._ensure_open("execute_text")
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
                plan = self.optimizer.optimize(original_plan) if self.optimize else original_plan
                if sink is not None:
                    sink({
                        "statement_index": statement_index,
                        "stage": "OPTIMIZED_PLAN",
                        "data": {"enabled": self.optimize, "plan": plan},
                    })
                result = self.executor.execute(plan, ExecutionContext(self.catalog, self.storage))
                if result.affected_rows is not None:
                    # CREATE、INSERT、DELETE 的成功结果只有在 sync 成功后才可返回给 CLI。
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
            if error.stage is ErrorStage.STORAGE:
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
            _abort_quietly(self.storage, wrapped)
            self._closed = True
            raise wrapped from error

    def execute_file(self, path: str) -> list[QueryResult]:
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
        return self.execute_text(text, source_name=str(actual_path))

    def close(self) -> None:
        """正常同步并关闭会话。"""
        if self._closed:
            return
        try:
            self.storage.close()
        except BaseException as error:
            _abort_quietly(self.storage, error)
            self._closed = True
            raise
        self._closed = True

    def abort(self) -> None:
        """失败路径关闭会话，不刷新脏页。重复调用无副作用。"""
        if self._closed:
            return
        try:
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


__all__ = ["Session"]
