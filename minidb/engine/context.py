"""STABLE P0 CONTRACT: dependencies assembled for one executor session.

Other modules may depend on ``ExecutionContext`` now.  Its field names are
fixed by the work plan.  The referenced concrete classes are teammate-owned:
Zhang Zhen supplies ``CatalogManager`` and Zhou Shengrong supplies the
``StorageEngine`` contract and final implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

# These imports are intentionally type-checking-only.  The teammate-owned
# modules may not exist in an early checkout, and importing context at runtime
# must not initialize a catalog or open a database file.
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
