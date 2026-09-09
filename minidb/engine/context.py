"""同一次执行会话使用的目录与存储依赖。

``ExecutionContext`` 只是一个装配容器：Session 创建它，Executor 使用它。
这里不打开数据库、不初始化目录，也不在构造时执行同步或关闭操作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

# 类型只用于静态检查，运行时不导入具体模块，避免仅导入 context 就触发
# Catalog 或 Storage 模块的额外初始化，也可以减少不必要的循环导入。
if TYPE_CHECKING:
    from minidb.catalog.catalog_manager import CatalogManager
    from minidb.storage.storage_engine import StorageEngine


@dataclass(slots=True)
class ExecutionContext:
    """Long-lived services required while executing a logical plan.

    Session code constructs this object; Executor consumes it.  Do not add
    startup, sync or close side effects to this data container.
    """

    catalog: "CatalogManager"
    storage: "StorageEngine"
