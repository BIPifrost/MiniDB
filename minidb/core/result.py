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

    Handoff note: Executor can open this cursor today. Session.iter_results
    and CLI integration are still pending; callers must explicitly exhaust
    or close it before starting another statement or closing the database.
    """

    __slots__ = ("_columns", "_rows", "_close", "_closed")

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

    @property
    def columns(self) -> tuple[ResultColumn, ...]:
        return self._columns

    @property
    def closed(self) -> bool:
        return self._closed

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
        """Close the row source and optional owner; repeated calls do nothing."""
        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None
        row_close = getattr(self._rows, "close", None)
        if callable(row_close):
            try:
                row_close()
            except BaseException as error:
                first_error = error
        if self._close is not None:
            try:
                self._close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
                else:
                    first_error.add_note(f"closing cursor owner also failed: {error}")
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
