"""张振：系统目录记录的转换与逻辑完整性检查。

这里只处理 StorageEngine 解码后的 Row 值，不负责二进制编码、页访问或
扫描资源的关闭。CatalogManager 后续负责这些调用的装配和根页物理校验。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn

from minidb.catalog.catalog import Catalog
from minidb.core.disk_types import FIRST_ALLOCATABLE_PAGE_ID, MAX_PAGE_ID
from minidb.core.schema import (
    SYSTEM_CATALOG_SCHEMA,
    ColumnDef,
    DataType,
    Schema,
    TableDef,
    TableRef,
    _IDENTIFIER,
)

if TYPE_CHECKING:
    # Row 的正式定义已提供；这里只用作类型注解，无需增加运行时导入。
    from minidb.core.records import Row


def table_to_catalog_rows(table: TableDef) -> tuple[Row, ...]:
    """每个用户表的每一列产生一条七字段目录记录，保持 Schema 列序。"""
    if not isinstance(table, TableDef):
        _invalid_argument("table_to_catalog_rows", "table", "TableDef", table)
    if table.ref.table_id == 0:
        _invalid_argument("table_to_catalog_rows", "table", "普通用户表", table.ref.name)

    ref = table.ref
    column_count = len(table.schema.columns)
    return tuple(
        (
            ref.table_id,
            ref.name,
            ref.root_page_id,
            column_count,
            index,
            column.name,
            column.data_type.value,
        )
        for index, column in enumerate(table.schema.columns)
    )


@dataclass(slots=True)
class _TableGroup:
    """只在一次加载过程中存在的分组状态，不向调用者发布。"""

    ref: TableRef
    column_count: int
    # default_factory 每次新建一个字典/集合，避免不同表共用同一个容器。
    columns: dict[int, ColumnDef] = field(default_factory=dict)
    column_names: set[str] = field(default_factory=set)


def catalog_from_rows(rows: Iterable[Row]) -> Catalog:
    """完整消费目录行后构造新快照；允许行乱序、不同表的行交错出现。

    缺失、冲突的元数据产生 CATALOG_CORRUPTED，不发布部分目录。
    输入迭代器产生的底层异常原样向上传播，不用空目录掩盖读取失败。
    本函数不接管迭代器的 close；调用者须在成功或失败后关闭 RowScan。
    """
    try:
        iterator = iter(rows)
    except TypeError:
        _invalid_argument("catalog_from_rows", "rows", "Iterable[Row]", rows)

    # groups 把同一表的记录收集到一起；names/roots 检测跨表身份冲突。
    groups: dict[int, _TableGroup] = {}
    names: dict[str, int] = {}
    roots: dict[int, int] = {}
    for row_number, row in enumerate(iterator):
        _validate_row(row, row_number)
        # 七字段顺序来自 SYSTEM_CATALOG_SCHEMA，解包后用名字代替难懂的 row[数字]。
        table_id, table_name, root_page_id, count, index, column_name, column_type = row
        group = groups.get(table_id)

        if group is None:
            if table_name in names:
                _corrupted(
                    f"rows[{row_number}].table_name", "不同表号使用了相同表名",
                    "唯一表名", table_name, table_id=table_id,
                )
            if root_page_id in roots:
                _corrupted(
                    f"rows[{row_number}].root_page_id", "不同用户表共用了根页",
                    "唯一根页号", root_page_id, table_id=table_id,
                )
            group = _TableGroup(TableRef(table_id, table_name, root_page_id), count)
            groups[table_id] = group
            names[table_name] = table_id
            roots[root_page_id] = table_id
        else:
            header_fields = (
                ("table_name", group.ref.name, table_name),
                ("root_page_id", group.ref.root_page_id, root_page_id),
                ("column_count", group.column_count, count),
            )
            # 一张表的每条列记录都会重复保存这些头字段，必须彼此一致。
            for name, expected, actual in header_fields:
                if actual != expected:
                    _corrupted(
                        f"rows[{row_number}].{name}", "同一表的目录记录相互冲突",
                        expected, actual, table_id=table_id,
                    )

        if index in group.columns:
            _corrupted(
                f"rows[{row_number}].column_index", "同一表出现重复列序号",
                "每个列序号恰好出现一次", index, table_id=table_id,
            )
        if column_name in group.column_names:
            _corrupted(
                f"rows[{row_number}].column_name", "同一表出现重复列名",
                "唯一列名", column_name, table_id=table_id,
            )
        group.columns[index] = ColumnDef(column_name, DataType(column_type))
        group.column_names.add(column_name)

    tables: list[TableDef] = []
    for table_id in sorted(groups):
        group = groups[table_id]
        # 只检查已有序号连续还不够：0、1 连续，但 count=3 时仍缺最后一列。
        expected_indexes = list(range(group.column_count))
        actual_indexes = sorted(group.columns)
        if actual_indexes != expected_indexes:
            _corrupted(
                "column_index", "目录列定义不完整，实际列序号未覆盖 column_count",
                expected_indexes, actual_indexes, table_id=table_id,
            )
        schema = Schema(tuple(group.columns[index] for index in expected_indexes))
        tables.append(TableDef(group.ref, schema))
    return Catalog(tuple(tables))


def _validate_row(row: Row, row_number: int) -> None:
    """逐字段检查七列目录记录的类型、范围和名字；它还不知道其他目录行的内容。"""
    fields = SYSTEM_CATALOG_SCHEMA.columns
    table_id = row[0] if isinstance(row, tuple) and row and type(row[0]) is int else None
    prefix = f"rows[{row_number}]"
    if not isinstance(row, tuple) or len(row) != len(fields):
        _corrupted(prefix, "目录记录必须是固定的七字段元组", "七字段 Row", row, table_id=table_id)

    # 复用系统 Schema 的字段类型；不能让 bool 通过 Python 的 int 子类判断。
    for value, column in zip(row, fields):
        expected_type = int if column.data_type is DataType.INT else str
        if type(value) is not expected_type:
            _corrupted(
                f"{prefix}.{column.name}", "目录字段的 Python 类型不匹配",
                expected_type.__name__, type(value).__name__, table_id=table_id,
            )

    ranges = (
        ("table_id", row[0], 1, 0xFFFFFFFE),
        ("root_page_id", row[2], FIRST_ALLOCATABLE_PAGE_ID, MAX_PAGE_ID),
        ("column_count", row[3], 1, 64),
        ("column_index", row[4], 0, row[3] - 1),
    )
    for name, value, minimum, maximum in ranges:
        if not minimum <= value <= maximum:
            _corrupted(
                f"{prefix}.{name}", "目录字段超出允许范围",
                [minimum, maximum], value, table_id=table_id,
            )

    for name, value in (("table_name", row[1]), ("column_name", row[5])):
        if _IDENTIFIER.fullmatch(value) is None or value != value.lower():
            _corrupted(
                f"{prefix}.{name}", "目录名称必须已归一化，不能自动修复损坏数据",
                "1 至 64 字符的小写 ASCII 标识符", value, table_id=table_id,
            )
    if row[1].startswith("_sys_"):
        _corrupted(
            f"{prefix}.table_name", "用户目录记录不能占用系统表前缀",
            "不使用 _sys_ 前缀的用户表名", row[1], table_id=table_id,
        )
    if row[6] not in (DataType.INT.value, DataType.VARCHAR.value):
        _corrupted(
            f"{prefix}.column_type", "目录记录包含未开放的列类型",
            ["INT", "VARCHAR"], row[6], table_id=table_id,
        )


def _diagnostic_value(value: object) -> object:
    """错误上下文只保存 JSON 可表示的数据，未知对象记录为文本。"""
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, (tuple, list)):
        # Row 是平坦元组；对不合法的嵌套对象用 repr，避免循环容器递归。
        return [item if item is None or type(item) in (str, int, bool) else repr(item) for item in value]
    return repr(value)


def _corrupted(
    field: str, reason: str, expected: object, actual: object, *, table_id: int | None = None
) -> NoReturn:
    # 直接使用公共错误类型，与文件读取和页校验的异常保持一致。
    """报告目录记录损坏，附具体字段和已知表号，保留 JSON 可表示的上下文。"""
    from minidb.core.errors import CATALOG_CORRUPTED, DbError, ErrorStage

    context = {
        "operation": "catalog_from_rows",
        "field": field,
        "reason": reason,
        "expected": _diagnostic_value(expected),
    }
    actual_value = _diagnostic_value(actual)
    if actual_value is not None:
        context["actual"] = actual_value
    if table_id is not None:
        context["table_id"] = table_id
    raise DbError(ErrorStage.STORAGE, CATALOG_CORRUPTED, reason, None, context)


def _invalid_argument(operation: str, field: str, expected: str, actual: object) -> NoReturn:
    """报告目录转换函数的调用参数错误，区别于磁盘元数据损坏。"""
    from minidb.core.errors import INVALID_ARGUMENT, DbError, ErrorStage

    context = {
        "operation": operation,
        "field": field,
        "expected": expected,
    }
    actual_value = _diagnostic_value(actual)
    if actual_value is not None:
        context["actual"] = actual_value
    raise DbError(
        ErrorStage.STORAGE,
        INVALID_ARGUMENT,
        f"{operation} 的 {field} 参数不合法",
        None,
        context,
    )
