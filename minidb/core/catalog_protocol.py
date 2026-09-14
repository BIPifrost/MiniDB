"""张振：Semantic 能使用的目录查询接口。"""

from typing import Protocol, runtime_checkable

from minidb.core.schema import TableDef, IndexDef


@runtime_checkable
class CatalogRead(Protocol):
    """语义和优化器只读接口；不允许通过此协议修改表或索引目录。"""

    def find_table(self, name: str) -> TableDef | None:
        """名称大小写不敏感，合法但不存在的表名返回 None。"""
        ...

    def list_tables(self) -> list[TableDef]:
        """按 table_id 升序返回用户表的新列表。"""
        ...

    def find_index(self, name: str) -> IndexDef | None:
        """按全库唯一索引名查询；不存在返回None。"""
        ...

    def indexes_for_table(self, table_id: int) -> tuple[IndexDef, ...]:
        """按index_id返回指定用户表的全部自动和用户索引。"""
        ...


# Protocol 是“需要哪些方法”的约定，不是存储实现。Catalog 和 CatalogManager
# 只要提供同名方法，就能作为 Semantic 的 catalog 参数；省略号表示这里只声明接口。
