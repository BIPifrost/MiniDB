"""Shared result types produced by the execution engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
