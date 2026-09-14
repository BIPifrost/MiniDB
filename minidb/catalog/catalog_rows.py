"""v2 两张系统表的规范行转换；完整校验后才构造可发布目录。"""
from collections import defaultdict
from decimal import InvalidOperation
from minidb.catalog.catalog import Catalog
from minidb.core.errors import DbError
from minidb.core._v2_contract import fail
from minidb.core.schema import (
    TableDef, TableRef, Schema, ColumnDef, TypeSpec, DataType, IndexDef, IndexOrigin,
    SYSTEM_CATALOG_SCHEMA, SYSTEM_INDEXES_SCHEMA, MAX_USER_TABLES,
)
from minidb.core.value_rules import default_text, normalize_value, parse_default


def table_to_catalog_rows(table):
    """每列一条15字段记录；不使用RowCodec，不触碰磁盘。"""
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
    grouped = defaultdict(dict)
    metadata, indexes = {}, []
    input_error = None

    def values(source):
        """区分输入读取失败和目录内容损坏，保留存储层的原异常。"""
        nonlocal input_error
        try:
            yield from source
        except Exception as error:
            input_error = error
            raise

    try:
        for row in values(rows):
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
        tables = []
        for tid, (name, root, count) in metadata.items():
            if len(grouped[tid]) != count:
                raise ValueError("系统目录缺列")
            tables.append(TableDef(TableRef(tid, name, root),
                                   Schema(tuple(grouped[tid][i] for i in range(count)))))
        for row in values(index_rows):
            if len(indexes) >= 16381:
                raise ValueError("索引目录超过v2文件的可用页数")
            _validate_row(row, SYSTEM_INDEXES_SCHEMA)
            indexes.append(IndexDef(*row[:6], IndexOrigin[row[6]]))
        catalog = Catalog(tuple(tables), tuple(indexes))
        catalog.validate_integrity()
        return catalog
    except (DbError, ValueError, KeyError, TypeError, InvalidOperation, NotImplementedError) as error:
        # 此处仅转换目录内容。缺少业务错误码也不能掩盖已经识别出的目录损坏。
        if error is input_error:
            raise
        if isinstance(error, DbError) and error.code == "CATALOG_CORRUPTED":
            raise
        fail("CATALOG_CORRUPTED", "系统目录内容不符合v2规范", stage="STORAGE", reason=str(error))
