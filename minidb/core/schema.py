"""表、列、索引的唯一元数据定义；v2 合同第4、10节。"""
from dataclasses import dataclass
from enum import Enum
import re

from minidb.core._v2_contract import fail

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
MAX_USER_TABLE_ID = 0xFFFFFFFD
MAX_USER_TABLES = 128
SYSTEM_CATALOG_NAME = "_sys_catalog"
SYSTEM_INDEXES_NAME = "_sys_indexes"
SYSTEM_INDEXES_ID = 0xFFFFFFFE


class DataType(Enum):
    INT = "INT"
    VARCHAR = "VARCHAR"
    BOOL = "BOOL"
    DATE = "DATE"
    DECIMAL = "DECIMAL"


def _invalid(operation, field, expected, actual):
    fail("INVALID_ARGUMENT", f"{operation} 的 {field} 不合法",
         operation=operation, field=field, expected=expected, actual=repr(actual))


def _normalize_identifier(name, operation):
    if type(name) is not str or _IDENTIFIER.fullmatch(name) is None:
        _invalid(operation, "name", "1至64字符的 ASCII 标识符", name)
    return name.lower()


@dataclass(frozen=True, slots=True)
class TypeSpec:
    """所有类型参数只保存在这里；VARCHAR 的缺省长度归一为1024。"""
    kind: DataType
    length: int | None = None
    precision: int | None = None
    scale: int | None = None

    def __post_init__(self):
        if not isinstance(self.kind, DataType):
            _invalid("TypeSpec", "kind", "DataType", self.kind)
        if self.kind is DataType.VARCHAR and self.length is None:
            object.__setattr__(self, "length", 1024)
        if self.kind is DataType.VARCHAR:
            valid = type(self.length) is int and 1 <= self.length <= 1024
            valid = valid and self.precision is None and self.scale is None
        elif self.kind is DataType.DECIMAL:
            valid = (self.length is None and type(self.precision) is int
                     and type(self.scale) is int and 1 <= self.precision <= 18
                     and 0 <= self.scale <= self.precision)
        else:
            valid = self.length is self.precision is self.scale is None
        if not valid:
            fail("INVALID_TYPE_PARAMETER", "类型参数组合或范围错误", kind=self.kind.name,
                 field="length/precision/scale", expected="VARCHAR(1..1024) 或 DECIMAL(1..18,0..p)",
                 actual=repr((self.length, self.precision, self.scale)))


def as_type_spec(value, *, operation="type_spec"):
    """仅供旧调用的过渡：DataType 转为完整 TypeSpec，不保留两份类型真值。"""
    if isinstance(value, DataType):
        return TypeSpec(value)
    if not isinstance(value, TypeSpec):
        _invalid(operation, "type_spec", "TypeSpec", value)
    return value


@dataclass(frozen=True, slots=True)
class DefaultSpec:
    has_default: bool
    value: object

    def __post_init__(self):
        if type(self.has_default) is not bool or (not self.has_default and self.value is not None):
            _invalid("DefaultSpec", "value", "无默认值固定为 DefaultSpec(False,None)", self.value)


NO_DEFAULT = DefaultSpec(False, None)


@dataclass(frozen=True, slots=True)
class ColumnDef:
    """SQL 约束已归一化；主键隐含非空及唯一，普通列缺省可空。"""
    name: str
    type_spec: TypeSpec
    nullable: bool = True
    default: DefaultSpec = NO_DEFAULT
    primary_key: bool = False
    unique: bool = False

    def __post_init__(self):
        if _normalize_identifier(self.name, "ColumnDef") != self.name:
            _invalid("ColumnDef", "name", "小写名称", self.name)
        object.__setattr__(self, "type_spec", as_type_spec(self.type_spec, operation="ColumnDef"))
        for field in ("nullable", "primary_key", "unique"):
            if type(getattr(self, field)) is not bool:
                _invalid("ColumnDef", field, "bool", getattr(self, field))
        if self.primary_key and (self.nullable or not self.unique):
            fail("CONFLICTING_CONSTRAINT", "主键定义必须已经归一为非空且唯一", column=self.name)
        if self.unique and self.type_spec.kind not in (DataType.INT, DataType.VARCHAR):
            fail("UNSUPPORTED_FEATURE", "列级唯一约束仅支持 INT/VARCHAR", column=self.name)
        if not isinstance(self.default, DefaultSpec):
            _invalid("ColumnDef", "default", "DefaultSpec", self.default)
        if self.default.has_default:
            from minidb.core.value_rules import normalize_value, default_text
            value = normalize_value(self.default.value, self.type_spec, nullable=self.nullable)
            object.__setattr__(self, "default", DefaultSpec(True, value))
            size = len(default_text(value, self.type_spec).encode("utf-8"))
            if size > 256:
                fail("VALUE_TOO_LONG", "DEFAULT 的 UTF-8 负载超过256字节", column=self.name,
                     actual=size, limit=256)

    @property
    def data_type(self):
        """供尚未升级的读取方过渡使用；唯一数据源仍为 type_spec。"""
        return self.type_spec.kind


@dataclass(frozen=True, slots=True)
class Schema:
    columns: tuple[ColumnDef, ...]

    def __post_init__(self):
        if type(self.columns) is not tuple or not 1 <= len(self.columns) <= 64:
            _invalid("Schema", "columns", "1至64列的 tuple", self.columns)
        if any(not isinstance(column, ColumnDef) for column in self.columns):
            _invalid("Schema", "columns", "ColumnDef", self.columns)
        if len({column.name for column in self.columns}) != len(self.columns):
            _invalid("Schema", "columns", "不重复的列名", tuple(column.name for column in self.columns))
        if sum(column.primary_key for column in self.columns) > 1:
            fail("CONFLICTING_CONSTRAINT", "每表最多一个单列主键")

    def find_column(self, name):
        normalized = _normalize_identifier(name, "Schema.find_column")
        return next(((index, column) for index, column in enumerate(self.columns)
                     if column.name == normalized), None)


@dataclass(frozen=True, slots=True)
class TableRef:
    table_id: int
    name: str
    root_page_id: int

    def __post_init__(self):
        if _normalize_identifier(self.name, "TableRef") != self.name:
            _invalid("TableRef", "name", "小写名称", self.name)
        if type(self.table_id) is not int or type(self.root_page_id) is not int:
            _invalid("TableRef", "id", "int", (self.table_id, self.root_page_id))
        reserved = {0: (SYSTEM_CATALOG_NAME, 1), SYSTEM_INDEXES_ID: (SYSTEM_INDEXES_NAME, 2)}
        if self.table_id in reserved:
            if (self.name, self.root_page_id) != reserved[self.table_id]:
                _invalid("TableRef", "reserved", "固定系统表身份", self.name)
        elif not (1 <= self.table_id <= MAX_USER_TABLE_ID and 3 <= self.root_page_id < 16384
                  and not self.name.startswith("_sys_")):
            _invalid("TableRef", "identity", "v2用户表号、非保留根页3..16383", self)


def _system_column(name, kind, length=None):
    return ColumnDef(name, TypeSpec(kind, length), nullable=False)


SYSTEM_CATALOG_SCHEMA = Schema(tuple(
    _system_column(name, kind, length) for name, kind, length in (
        ("table_id", DataType.INT, None), ("table_name", DataType.VARCHAR, 64),
        ("root_page_id", DataType.INT, None), ("column_count", DataType.INT, None),
        ("column_index", DataType.INT, None), ("column_name", DataType.VARCHAR, 64),
        ("type_name", DataType.VARCHAR, 16), ("type_length", DataType.INT, None),
        ("type_precision", DataType.INT, None), ("type_scale", DataType.INT, None),
        ("nullable", DataType.BOOL, None), ("primary_key", DataType.BOOL, None),
        ("unique", DataType.BOOL, None), ("default_kind", DataType.VARCHAR, 16),
        ("default_text", DataType.VARCHAR, 256),
    )
))
SYSTEM_INDEXES_SCHEMA = Schema(tuple(
    _system_column(name, kind, length) for name, kind, length in (
        ("index_id", DataType.INT, None), ("index_name", DataType.VARCHAR, 64),
        ("table_id", DataType.INT, None), ("column_index", DataType.INT, None),
        ("root_page_id", DataType.INT, None), ("unique", DataType.BOOL, None),
        ("origin", DataType.VARCHAR, 24),
    )
))


@dataclass(frozen=True, slots=True)
class TableDef:
    ref: TableRef
    schema: Schema

    def __post_init__(self):
        if not isinstance(self.ref, TableRef) or not isinstance(self.schema, Schema):
            _invalid("TableDef", "fields", "TableRef、Schema", (self.ref, self.schema))
        expected = {0: SYSTEM_CATALOG_SCHEMA, SYSTEM_INDEXES_ID: SYSTEM_INDEXES_SCHEMA}.get(self.ref.table_id)
        if expected is not None and self.schema != expected:
            _invalid("TableDef", "schema", "系统表固定 Schema", self.schema)


class IndexOrigin(Enum):
    PRIMARY_KEY = "PRIMARY_KEY"
    UNIQUE_CONSTRAINT = "UNIQUE_CONSTRAINT"
    USER = "USER"


@dataclass(frozen=True, slots=True)
class PendingIndexDef:
    name: str | None
    column_index: int
    unique: bool
    origin: IndexOrigin

    def __post_init__(self):
        if type(self.column_index) is not int or not 0 <= self.column_index < 64:
            _invalid("PendingIndexDef", "column_index", "0..63", self.column_index)
        if type(self.unique) is not bool or not isinstance(self.origin, IndexOrigin):
            _invalid("PendingIndexDef", "flags", "bool和IndexOrigin", self.origin)
        if self.origin is IndexOrigin.USER:
            if _normalize_identifier(self.name, "PendingIndexDef") != self.name or self.name.startswith("_sys_"):
                _invalid("PendingIndexDef", "name", "用户索引小写名称", self.name)
        elif self.name is not None or not self.unique:
            _invalid("PendingIndexDef", "automatic", "name=None且unique=True", self.name)


@dataclass(frozen=True, slots=True)
class IndexDef:
    index_id: int
    name: str
    table_id: int
    column_index: int
    root_page_id: int
    unique: bool
    origin: IndexOrigin

    def __post_init__(self):
        for field, lower, upper in (("index_id", 1, 0xFFFFFFFD), ("table_id", 1, MAX_USER_TABLE_ID),
                                    ("column_index", 0, 63), ("root_page_id", 3, 16383)):
            value = getattr(self, field)
            if type(value) is not int or not lower <= value <= upper:
                _invalid("IndexDef", field, f"{lower}..{upper}", value)
        if _normalize_identifier(self.name, "IndexDef") != self.name:
            _invalid("IndexDef", "name", "小写名称", self.name)
        if not isinstance(self.origin, IndexOrigin) or type(self.unique) is not bool:
            _invalid("IndexDef", "flags", "bool和IndexOrigin", self.origin)
        if self.origin is IndexOrigin.USER:
            PendingIndexDef(self.name, self.column_index, self.unique, self.origin)
        else:
            prefix = "pk" if self.origin is IndexOrigin.PRIMARY_KEY else "uq"
            if not self.unique or self.name != f"_sys_{prefix}_{self.table_id}_{self.column_index}":
                _invalid("IndexDef", "automatic", "固定自动名称且unique=True", self.name)


@dataclass(frozen=True, slots=True)
class IndexBounds:
    has_lower: bool
    lower: object | None
    lower_inclusive: bool
    has_upper: bool
    upper: object | None
    upper_inclusive: bool
    null_only: bool

    def __post_init__(self):
        for field in ("has_lower", "lower_inclusive", "has_upper", "upper_inclusive", "null_only"):
            if type(getattr(self, field)) is not bool:
                _invalid("IndexBounds", field, "bool", getattr(self, field))
        for present, value, inclusive in ((self.has_lower, self.lower, self.lower_inclusive),
                                          (self.has_upper, self.upper, self.upper_inclusive)):
            if (present and value is None) or (not present and (value is not None or inclusive)):
                _invalid("IndexBounds", "boundary", "边界值和has/inclusive一致", value)
        if self.null_only and (self.has_lower or self.has_upper):
            _invalid("IndexBounds", "null_only", "NULL模式不能附带普通边界", self)
