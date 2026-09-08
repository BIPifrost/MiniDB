"""张振：Semantic 能使用的目录查询接口。"""

from typing import Protocol, runtime_checkable

from minidb.core.schema import TableDef


@runtime_checkable
class CatalogRead(Protocol):
    """只约定查表和列举用户表；Protocol 不提供运行时权限隔离。"""

    def find_table(self, name: str) -> TableDef | None:
        """名称大小写不敏感，合法但不存在的表名返回 None。"""
        ...

    def list_tables(self) -> list[TableDef]:
        """按 table_id 升序返回用户表的新列表。"""
        ...


# Protocol 是“需要哪些方法”的约定，不是存储实现。Catalog 和 CatalogManager
# 只要提供同名方法，就能作为 Semantic 的 catalog 参数；省略号表示这里只声明接口。
