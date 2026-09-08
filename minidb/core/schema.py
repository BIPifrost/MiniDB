"""张振：列定义与表结构。Schema 的顺序就是后续 Row 的值顺序。"""

import re
from dataclasses import dataclass
from enum import Enum

from minidb.core.disk_types import CATALOG_ROOT_PAGE_ID, FIRST_ALLOCATABLE_PAGE_ID, MAX_PAGE_ID


class DataType(Enum):
    """项目中的数据类型：INT 为整数，VARCHAR 为字符串，BOOL 仅用于条件结果。"""
    INT = "INT"
    VARCHAR = "VARCHAR"
    BOOL = "BOOL"


# 正则规则：第一个字符只能是字母或下划线，其后最多 63 个字母/数字/下划线。
# fullmatch 会检查整个字符串，防止只匹配前半段而漏掉空格或非法后缀。
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", flags=re.ASCII)


def _invalid(operation: str, field: str, expected: str, actual: object) -> None:
    # 与目录和存储共用 core.errors 中已有的错误定义。
    """使用公共 DbError 报告构造或查列参数不符合约定。"""
    from minidb.core.errors import INVALID_ARGUMENT, DbError, ErrorStage

    raise DbError(
        stage=ErrorStage.SEMANTIC,
        code=INVALID_ARGUMENT,
        message=f"{operation} 的 {field} 参数不合法",
        span=None,
        context={
            "operation": operation,
            "field": field,
            "expected": expected,
            "actual": repr(actual),
        },
    )


def _normalize_identifier(name: str, operation: str) -> str:
    """检查名称的字符形式，不擅自删除空格或改变字符串字面量。

    SQL 关键字分类由 Lexer/Parser 完成；手工 AST 的名字、关键字和
    保留表名前缀由后续 Semantic 复核，不在这里另建一份关键字表。
    """
    if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None:
        _invalid(operation, "name", "1 至 64 字符的 ASCII 标识符", name)
    return name.lower()


# dataclass 自动生成构造、比较等方法；frozen 禁止改字段；slots 禁止随意加新字段。
# 下面的 Schema、TableRef、TableDef 使用同样设置，避免其他模块悄悄修改元数据。
@dataclass(frozen=True, slots=True)
class ColumnDef:
    """检查后的列定义：名字必须已经归一为小写，BOOL 不能作为表字段。"""

    name: str
    data_type: DataType

    def __post_init__(self) -> None:
        """数据类构造后自动执行：确认列名已经小写，字段类型在开放范围内。"""
        normalized = _normalize_identifier(self.name, "ColumnDef")
        if normalized != self.name:
            _invalid("ColumnDef", "name", "已转为小写的标识符", self.name)
        if not isinstance(self.data_type, DataType) or self.data_type not in (
            DataType.INT,
            DataType.VARCHAR,
        ):
            _invalid("ColumnDef", "data_type", "DataType.INT 或 DataType.VARCHAR", self.data_type)


@dataclass(frozen=True, slots=True)
class Schema:
    """1 至 64 个不重名的列，构造后不可修改或重排。"""

    columns: tuple[ColumnDef, ...]

    def __post_init__(self) -> None:
        """确认列集合为元组、数量合法且没有重名列；seen 记录已出现的名称。"""
        if not isinstance(self.columns, tuple):
            _invalid("Schema", "columns", "tuple[ColumnDef, ...]", self.columns)
        if not 1 <= len(self.columns) <= 64:
            _invalid("Schema", "columns", "1 至 64 列", len(self.columns))
        seen: set[str] = set()
        for index, column in enumerate(self.columns):
            if not isinstance(column, ColumnDef):
                _invalid("Schema", f"columns[{index}]", "ColumnDef", column)
            if column.name in seen:
                _invalid("Schema", f"columns[{index}].name", "不重复的列名", column.name)
            seen.add(column.name)

    def find_column(self, name: str) -> tuple[int, ColumnDef] | None:
        """返回从 0 开始的列序号和定义，合法但不存在的名字返回 None。

        本接口不产生 COLUMN_NOT_FOUND；Semantic 拿到 SQL 位置后再报错。
        表最多 64 列，直接按定义顺序查找即可，不另存一份可变的列索引。
        """
        normalized = _normalize_identifier(name, "Schema.find_column")
        for index, column in enumerate(self.columns):
            if column.name == normalized:
                return index, column
        return None


# 系统目录的列结构只有这一份。放在 core，使 TableDef 可以验证它，
# 无需反向导入 catalog。完整的系统 TableDef 由 catalog/catalog.py 定义。
SYSTEM_CATALOG_NAME = "_sys_catalog"
SYSTEM_CATALOG_SCHEMA = Schema((
    ColumnDef("table_id", DataType.INT),
    ColumnDef("table_name", DataType.VARCHAR),
    ColumnDef("root_page_id", DataType.INT),
    ColumnDef("column_count", DataType.INT),
    ColumnDef("column_index", DataType.INT),
    ColumnDef("column_name", DataType.VARCHAR),
    ColumnDef("column_type", DataType.VARCHAR),
))


@dataclass(frozen=True, slots=True)
class TableRef:
    """表的身份和存储入口；这里只校验编号，不读取根页。"""

    table_id: int       # 数据库内部识别表的编号，不是用户表中的普通 id 列。
    name: str          # 已转为小写的表名。
    root_page_id: int   # 该表记录页链的入口，只记编号，构造对象时不读磁盘。

    def __post_init__(self) -> None:
        """校验表号、根页号和名称，并区分用户表与固定系统目录的保留编号。"""
        if type(self.table_id) is not int or not 0 <= self.table_id <= 0xFFFFFFFE:
            _invalid("TableRef", "table_id", "0 至 0xFFFFFFFE 的 int，不接受 bool", self.table_id)
        normalized = _normalize_identifier(self.name, "TableRef")
        if self.name != normalized:
            _invalid("TableRef", "name", "已转为小写的标识符", self.name)
        # 根页边界取自廖杰提供的磁盘契约；表号仍使用自己的编号范围。
        if type(self.root_page_id) is not int or not CATALOG_ROOT_PAGE_ID <= self.root_page_id <= MAX_PAGE_ID:
            _invalid("TableRef", "root_page_id", "1 至 0xFFFFFFFE 的 int，不接受 bool", self.root_page_id)

        if self.table_id == 0:
            if self.name != SYSTEM_CATALOG_NAME:
                _invalid("TableRef", "name", SYSTEM_CATALOG_NAME, self.name)
            if self.root_page_id != CATALOG_ROOT_PAGE_ID:
                _invalid("TableRef", "root_page_id", "系统目录固定为 1", self.root_page_id)
        else:
            if self.name.startswith("_sys_"):
                _invalid("TableRef", "name", "用户表名不能使用 _sys_ 前缀", self.name)
            if self.root_page_id < FIRST_ALLOCATABLE_PAGE_ID:
                _invalid("TableRef", "root_page_id", "用户表根页号至少为 2", self.root_page_id)


@dataclass(frozen=True, slots=True)
class TableDef:
    """不可变的完整表定义。根页是否真实存在由 StorageEngine 检查。"""

    ref: TableRef
    schema: Schema

    def __post_init__(self) -> None:
        """确认身份和列结构来自正式类型，系统目录的七列结构必须保持固定。"""
        if not isinstance(self.ref, TableRef):
            _invalid("TableDef", "ref", "TableRef", self.ref)
        if not isinstance(self.schema, Schema):
            _invalid("TableDef", "schema", "Schema", self.schema)
        if self.ref.table_id == 0 and self.schema != SYSTEM_CATALOG_SCHEMA:
            _invalid("TableDef", "schema", "系统目录固定的七列 Schema", self.schema)
