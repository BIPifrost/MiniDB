"""张振：不可变的内存用户目录，以及系统目录的唯一固定表定义。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from minidb.core.disk_types import CATALOG_ROOT_PAGE_ID
from minidb.core.schema import (
    SYSTEM_CATALOG_NAME,
    SYSTEM_CATALOG_SCHEMA,
    TableDef,
    TableRef,
    _invalid,
    _normalize_identifier,
)


# 启动阶段直接引用此对象。它描述目录的结构，不初始化或读取 page 1。
SYSTEM_CATALOG_TABLE = TableDef(
    ref=TableRef(table_id=0, name=SYSTEM_CATALOG_NAME, root_page_id=CATALOG_ROOT_PAGE_ID),
    schema=SYSTEM_CATALOG_SCHEMA,
)


@dataclass(frozen=True, slots=True)
class Catalog:
    """已知用户表定义的只读快照，满足 CatalogRead 协议。

    通过 Catalog((table1, table2)) 构造；检查表名、表号、根页号唯一。
    后续 CatalogManager 在持久化成功后发布新快照，不原地修改此对象。
    """

    tables: tuple[TableDef, ...] = ()
    # 内部查询索引由 __post_init__ 生成，不由构造者传入，也不参与表定义比较。
    _by_name: Mapping[str, TableDef] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """检查所有表的身份唯一，再建立按表号排序的元组和按表名查询的只读映射。"""
        if not isinstance(self.tables, tuple):
            _invalid("Catalog", "tables", "tuple[TableDef, ...]", self.tables)

        by_name: dict[str, TableDef] = {}
        table_ids: set[int] = set()
        root_page_ids: set[int] = set()
        for index, table in enumerate(self.tables):
            if not isinstance(table, TableDef):
                _invalid("Catalog", f"tables[{index}]", "TableDef", table)
            ref = table.ref
            if ref.table_id == 0:
                _invalid("Catalog", f"tables[{index}]", "普通用户表，系统目录使用固定定义", ref.name)
            if ref.name in by_name:
                _invalid("Catalog", f"tables[{index}].ref.name", "不重复的表名", ref.name)
            if ref.table_id in table_ids:
                _invalid("Catalog", f"tables[{index}].ref.table_id", "不重复的表号", ref.table_id)
            if ref.root_page_id in root_page_ids:
                _invalid("Catalog", f"tables[{index}].ref.root_page_id", "不重复的根页号", ref.root_page_id)
            by_name[ref.name] = table
            table_ids.add(ref.table_id)
            root_page_ids.add(ref.root_page_id)

        # 三个集合/映射分别防止表名、表号、根页号冲突，不能只按表名去重。
        ordered = tuple(sorted(self.tables, key=lambda table: table.ref.table_id))
        # frozen 对象只能在构造阶段用这种方式设置内部字段。
        # MappingProxyType 给字典套上只读视图，调用者不能修改名字索引。
        object.__setattr__(self, "tables", ordered)
        object.__setattr__(self, "_by_name", MappingProxyType(by_name))

    def find_table(self, name: str) -> TableDef | None:
        """按归一化名称查字典，找到返回原 TableDef，找不到返回 None。"""
        normalized = _normalize_identifier(name, "Catalog.find_table")
        return self._by_name.get(normalized)

    def list_tables(self) -> list[TableDef]:
        """从不可变元组复制出一个列表，调用者修改列表不会影响目录。"""
        return list(self.tables)
