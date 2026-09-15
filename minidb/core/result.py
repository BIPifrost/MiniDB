"""Shared result types produced by the execution engine."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias

from .records import Row, RowId, _validate_row

if TYPE_CHECKING:
    from .schema import DataType


@dataclass(frozen=True, slots=True)
class ResultColumn:
    """Metadata for one output column; duplicate names are permitted."""

    name: str
    data_type: "DataType"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("result column name must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ExecRecord:
    """Internal row-flow item used between execution operators."""

    values: Row
    row_id: RowId | None

    def __post_init__(self) -> None:
        _validate_row(self.values, field_name="values")
        if self.row_id is not None and not isinstance(self.row_id, RowId):
            raise TypeError("row_id must be a RowId or None")


@dataclass(slots=True)
class QueryResult:
    """Materialized public result returned by ``Executor.execute``.

    The list containers are copied at construction time so they never alias
    mutable executor or storage state. Rows themselves are immutable tuples.
    """

    columns: list[ResultColumn] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    affected_rows: int | None = None
    message: str = ""

    def __post_init__(self) -> None:
        self.columns = list(self.columns)
        self.rows = list(self.rows)

        if any(not isinstance(column, ResultColumn) for column in self.columns):
            raise TypeError("columns must contain only ResultColumn values")
        for row in self.rows:
            _validate_row(row, field_name="rows")
            if len(row) != len(self.columns):
                raise ValueError("each result row must match the column count")

        if self.affected_rows is not None:
            if isinstance(self.affected_rows, bool) or not isinstance(
                self.affected_rows, int
            ):
                raise TypeError("affected_rows must be an int or None")
            if self.affected_rows < 0:
                raise ValueError("affected_rows must be non-negative")
        if not isinstance(self.message, str):
            raise TypeError("message must be a string")


class ResultCursor:
    """Closeable, single-pass stream of validated result rows.

    ``Session.iter_results`` yields this cursor for SELECT and the CLI consumes
    it row by row. Callers must explicitly exhaust or close it before starting
    another statement or closing the database; ``close`` is repeatable and
    retries whatever the previous failed attempt could not release.
    """

    __slots__ = ("_columns", "_rows", "_close", "_closed", "_rows_released", "_owner_released")

    def __init__(
        self,
        columns: tuple[ResultColumn, ...],
        rows: Iterator[Row],
        *,
        close: Callable[[], None] | None = None,
    ) -> None:
        if type(columns) is not tuple:
            raise TypeError("columns must be a tuple")
        if any(not isinstance(column, ResultColumn) for column in columns):
            raise TypeError("columns must contain only ResultColumn values")
        if not hasattr(rows, "__next__"):
            raise TypeError("rows must be an iterator")
        if close is not None and not callable(close):
            raise TypeError("close must be callable or None")
        self._columns = columns
        self._rows = rows
        self._close = close
        self._closed = False
        # 释放状态与“逻辑关闭”分开记录：关闭失败时保留未释放标记，
        # 让调用方可以再次 close() 重试，而不是永久丢失底层清理。
        self._rows_released = False
        self._owner_released = close is None

    @property
    def columns(self) -> tuple[ResultColumn, ...]:
        return self._columns

    @property
    def closed(self) -> bool:
        """底层行来源与宿主是否都已释放；关闭失败时仍为 False。"""
        return self._rows_released and self._owner_released

    def __iter__(self) -> "ResultCursor":
        return self

    def __next__(self) -> Row:
        if self._closed:
            raise StopIteration
        try:
            row = next(self._rows)
            _validate_row(row, field_name="result row")
            if len(row) != len(self.columns):
                raise ValueError("each result row must match the column count")
            return row
        except StopIteration:
            self.close()
            raise
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"closing result cursor also failed: {cleanup_error}")
            raise

    def close(self) -> None:
        """关闭行来源和可选宿主；可重复调用。

        再次调用只重试上一次没有成功释放的那一步，已经完成的清理不会
        重复执行。关闭失败时抛出首个错误，并保留未释放状态供调用方重试；
        一旦全部释放，后续调用直接返回。
        """
        self._closed = True
        first_error: BaseException | None = None

        if not self._rows_released:
            row_close = getattr(self._rows, "close", None)
            if callable(row_close):
                try:
                    row_close()
                except BaseException as error:
                    first_error = error
                else:
                    self._rows_released = True
            else:
                self._rows_released = True

        if not self._owner_released:
            try:
                self._close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
                else:
                    first_error.add_note(f"closing cursor owner also failed: {error}")
            else:
                self._owner_released = True

        if first_error is not None:
            raise first_error


CommandResult = QueryResult
StatementResult: TypeAlias = QueryResult | ResultCursor


__all__ = [
    "CommandResult",
    "ExecRecord",
    "QueryResult",
    "ResultColumn",
    "ResultCursor",
    "StatementResult",
]
