"""TEMPORARY TEST-ONLY schema shapes.

Zhang Zhen owns the official definitions in ``minidb/core/schema.py``.  These
classes exist only to exercise StorageEngine's structural expectations before
that module lands.  Production code must never import from ``tests.fakes``.

Replace uses of ``FakeTableDef`` with the real ``TableDef`` once available;
do not copy any validation or behavior from this deliberately small fixture.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FakeColumnDef:
    name: str
    data_type: str


@dataclass(frozen=True, slots=True)
class FakeSchema:
    columns: tuple[FakeColumnDef, ...]


@dataclass(frozen=True, slots=True)
class FakeTableRef:
    table_id: int
    name: str
    root_page_id: int


@dataclass(frozen=True, slots=True)
class FakeTableDef:
    ref: FakeTableRef
    schema: FakeSchema


def make_table(
    table_id: int,
    root_page_id: int,
    name: str = "student",
    columns: tuple[tuple[str, str], ...] = (
        ("id", "INT"),
        ("name", "VARCHAR"),
        ("age", "INT"),
    ),
) -> FakeTableDef:
    return FakeTableDef(
        ref=FakeTableRef(table_id, name, root_page_id),
        schema=FakeSchema(tuple(FakeColumnDef(*column) for column in columns)),
    )
