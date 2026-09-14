"""v2 两张系统表的规范行转换；完整校验后才构造可发布目录。"""
from collections import defaultdict
from decimal import InvalidOperation
from minidb.catalog.catalog import Catalog
from minidb.core.errors import DbError
from minidb.core._v2_contract import fail
from minidb.core.schema import (
    TableDef, TableRef, Schema, ColumnDef, TypeSpec, DataType, IndexDef, IndexOrigin,
    SYSTEM_CATALOG_SCHEMA, SYSTEM_INDEXES_SCHEMA, MAX_USER_TABLES, MAX_USER_TABLE_ID,
)
from minidb.core.value_rules import default_text, normalize_value, parse_default


def table_to_catalog_rows(table):
    """每列一条15字段记录；不使用RowCodec，不触碰磁盘。"""
    # 系统表定义固定在代码中，不能被当作用户元数据写入目录。
    if not isinstance(table, TableDef) or not 1 <= table.ref.table_id <= MAX_USER_TABLE_ID:
        fail("INVALID_ARGUMENT", "目录转换需要用户TableDef", stage="STORAGE",
             operation="table_to_catalog_rows", field="table",
             expected="用户TableDef", actual=repr(table))
    result = []
    for position, column in enumerate(table.schema.columns):
        spec, default = column.type_spec, column.default
        kind = "NONE" if not default.has_default else ("NULL" if default.value is None else spec.kind.name)
        result.append((table.ref.table_id, table.ref.name, table.ref.root_page_id,
                       len(table.schema.columns), position, column.name, spec.kind.name,
                       spec.length if spec.length is not None else -1,
                       spec.precision if spec.precision is not None else -1,
                       spec.scale if spec.scale is not None else -1,
                       column.nullable, column.primary_key, column.unique, kind,
                       default_text(default.value, spec)))
    return tuple(result)


def index_to_catalog_row(index):
    if not isinstance(index, IndexDef):
        fail("INVALID_ARGUMENT", "索引目录转换需要IndexDef", stage="STORAGE",
             operation="index_to_catalog_row", field="index",
             expected="IndexDef", actual=repr(index))
    return (index.index_id, index.name, index.table_id, index.column_index,
            index.root_page_id, index.unique, index.origin.name)


def _validate_row(row, schema):
    if type(row) is not tuple or len(row) != len(schema.columns):
        raise ValueError("系统目录记录字段数错误")
    for value, column in zip(row, schema.columns):
        normalized = normalize_value(value, column.type_spec, nullable=False)
        if type(value) is not type(normalized) or value != normalized:
            raise ValueError("系统目录值不是规范类型")


def catalog_from_rows(rows, index_rows=()):
    """接受目录行流。禁止把v1七字段目录猜成v2；索引缺失也属于损坏。"""
    # 参数不能迭代和迭代过程的存储故障是两类错误；后者仍保留原异常。
    sources = []
    for field, source in (("rows", rows), ("index_rows", index_rows)):
        try:
            sources.append(iter(source))
        except TypeError:
            fail("INVALID_ARGUMENT", "目录输入必须可迭代", stage="STORAGE",
                 operation="catalog_from_rows", field=field,
                 expected="目录行迭代器", actual=type(source).__name__)
    rows, index_rows = sources
    grouped = defaultdict(dict)
    metadata, indexes = {}, []
    input_error = None
    location = {}

    def values(source):
        """区分输入读取失败和目录内容损坏，保留存储层的原异常。"""
        nonlocal input_error
        try:
            # 不用yield from：目录校验提前失败时，包装生成器的close不能
            # 继续关闭调用方拥有的输入流；正式RowScan由CatalogManager关闭。
            for row in source:
                yield row
        except Exception as error:
            input_error = error
            raise

    try:
        for row_index, row in enumerate(values(rows)):
            location = {"table_name": "_sys_catalog", "row_index": row_index}
            _validate_row(row, SYSTEM_CATALOG_SCHEMA)
            tid, name, root, count, position = row[:5]
            # 读取过程中立即限制目录规模，不能等整份输入进入内存再检查。
            if tid not in metadata and len(metadata) >= MAX_USER_TABLES:
                raise ValueError("用户表目录超过128张表")
            if not 1 <= count <= 64 or not 0 <= position < count:
                raise ValueError("列数或列序号越界")
            header = (name, root, count)
            if tid in metadata and metadata[tid] != header:
                raise ValueError("同表重复元数据不一致")
            if position in grouped[tid]:
                raise ValueError("列序号重复")
            if any(type(v) is not int or v < -1 for v in row[7:10]):
                raise ValueError("类型参数必须为有效整数或-1")
            spec = TypeSpec(DataType[row[6]], *(None if v == -1 else v for v in row[7:10]))
            # VARCHAR不能用缺省None隐式修补损坏目录，必须保存归一后的长度。
            if (spec.length, spec.precision, spec.scale) != tuple(None if v == -1 else v for v in row[7:10]):
                raise ValueError("类型参数不是规范表示")
            default = parse_default(row[13], row[14], spec, nullable=row[10])
            column = ColumnDef(row[5], spec, row[10], default, row[11], row[12])
            metadata[tid], grouped[tid][position] = header, column
        # 全表完整性错误不归咎于最后读取的一条合法记录。
        location = {}
        tables = []
        for tid, (name, root, count) in metadata.items():
            if len(grouped[tid]) != count:
                raise ValueError("系统目录缺列")
            tables.append(TableDef(TableRef(tid, name, root),
                                   Schema(tuple(grouped[tid][i] for i in range(count)))))
        for row_index, row in enumerate(values(index_rows)):
            location = {"table_name": "_sys_indexes", "row_index": row_index}
            if len(indexes) >= 16381:
                raise ValueError("索引目录超过v2文件的可用页数")
            _validate_row(row, SYSTEM_INDEXES_SCHEMA)
            indexes.append(IndexDef(*row[:6], IndexOrigin[row[6]]))
        location = {}
        catalog = Catalog(tuple(tables), tuple(indexes))
        catalog.validate_integrity()
        return catalog
    except (DbError, ValueError, KeyError, TypeError, InvalidOperation, NotImplementedError) as error:
        # 此处仅转换目录内容。缺少业务错误码也不能掩盖已经识别出的目录损坏。
        if error is input_error:
            raise
        if isinstance(error, DbError) and error.code == "CATALOG_CORRUPTED":
            raise
        fail("CATALOG_CORRUPTED", "系统目录内容不符合v2规范", stage="STORAGE",
             reason=str(error), **location)
