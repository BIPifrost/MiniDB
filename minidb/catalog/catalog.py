"""表与索引的只读目录；每次变更先构造候选快照，再由管理器发布。"""
from dataclasses import dataclass, field
from types import MappingProxyType
from minidb.core._v2_contract import fail
from minidb.core.schema import (
    TableDef, TableRef, IndexDef, IndexOrigin, MAX_USER_TABLE_ID, MAX_USER_TABLES,
    SYSTEM_CATALOG_NAME, SYSTEM_CATALOG_SCHEMA, SYSTEM_INDEXES_NAME,
    SYSTEM_INDEXES_ID, SYSTEM_INDEXES_SCHEMA, _normalize_identifier, _invalid,
)

SYSTEM_CATALOG_TABLE = TableDef(TableRef(0, SYSTEM_CATALOG_NAME, 1), SYSTEM_CATALOG_SCHEMA)
SYSTEM_INDEXES_TABLE = TableDef(TableRef(SYSTEM_INDEXES_ID, SYSTEM_INDEXES_NAME, 2), SYSTEM_INDEXES_SCHEMA)


@dataclass(frozen=True, slots=True)
class Catalog:
    """事务内可暂存刚建表但尚未建自动索引的快照；提交前须 validate_integrity。"""
    tables: tuple[TableDef, ...] = ()
    indexes: tuple[IndexDef, ...] = ()
    _by_name: object = field(init=False, repr=False, compare=False)
    _indexes_by_name: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.tables) is not tuple or type(self.indexes) is not tuple:
            _invalid("Catalog", "collections", "tuple", (self.tables, self.indexes))
        if len(self.tables) > MAX_USER_TABLES:
            fail("RESOURCE_LIMIT", "用户表超过128张", stage="EXECUTION", limit=MAX_USER_TABLES)
        tables, ids, roots = {}, {}, set()
        for table in self.tables:
            if not isinstance(table, TableDef) or not 1 <= table.ref.table_id <= MAX_USER_TABLE_ID:
                _invalid("Catalog", "table", "用户TableDef", table)
            ref = table.ref
            if ref.name in tables or ref.table_id in ids or ref.root_page_id in roots:
                _invalid("Catalog", "table", "表名、表号和根页不重复", ref)
            tables[ref.name], ids[ref.table_id] = table, table
            roots.add(ref.root_page_id)
        names, index_ids = {}, set()
        for index in self.indexes:
            if not isinstance(index, IndexDef):
                _invalid("Catalog", "index", "IndexDef", index)
            table = ids.get(index.table_id)
            if (table is None or index.column_index >= len(table.schema.columns)
                    or index.name in names or index.index_id in index_ids or index.root_page_id in roots):
                _invalid("Catalog", "index", "引用有效且身份、锚点唯一", index)
            column = table.schema.columns[index.column_index]
            if index.origin is IndexOrigin.PRIMARY_KEY and not column.primary_key:
                _invalid("Catalog", "origin", "主键列的自动索引", index)
            if index.origin is IndexOrigin.UNIQUE_CONSTRAINT and (not column.unique or column.primary_key):
                _invalid("Catalog", "origin", "非主键唯一列的自动索引", index)
            names[index.name] = index
            index_ids.add(index.index_id)
            roots.add(index.root_page_id)
        object.__setattr__(self, "tables", tuple(sorted(self.tables, key=lambda t: t.ref.table_id)))
        object.__setattr__(self, "indexes", tuple(sorted(self.indexes, key=lambda i: i.index_id)))
        object.__setattr__(self, "_by_name", MappingProxyType(tables))
        object.__setattr__(self, "_indexes_by_name", MappingProxyType(names))

    def find_table(self, name):
        return self._by_name.get(_normalize_identifier(name, "Catalog.find_table"))

    def list_tables(self):
        return list(self.tables)

    def find_index(self, name):
        return self._indexes_by_name.get(_normalize_identifier(name, "Catalog.find_index"))

    def indexes_for_table(self, table_id):
        if type(table_id) is not int or not 1 <= table_id <= MAX_USER_TABLE_ID:
            _invalid("Catalog.indexes_for_table", "table_id", "用户表号", table_id)
        return tuple(index for index in self.indexes if index.table_id == table_id)

    def validate_integrity(self):
        """启动、重载和提交前，检查每个约束恰好有一个正式自动索引。"""
        for table in self.tables:
            indexes = self.indexes_for_table(table.ref.table_id)
            for position, column in enumerate(table.schema.columns):
                if column.unique:
                    origin = IndexOrigin.PRIMARY_KEY if column.primary_key else IndexOrigin.UNIQUE_CONSTRAINT
                    matches = [i for i in indexes if i.column_index == position and i.origin is origin]
                    if len(matches) != 1:
                        fail("CATALOG_CORRUPTED", "约束缺少唯一的自动索引", stage="STORAGE",
                             table_id=table.ref.table_id, column_index=position)
