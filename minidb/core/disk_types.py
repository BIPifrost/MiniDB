"""Shared physical storage definitions for MiniDB specification v1.1.

This module performs no I/O and does not depend on storage implementations.
All multi-byte disk fields use little-endian encoding (format version 1).
"""

from dataclasses import dataclass, field
from typing import Final, TypeAlias

PageId: TypeAlias = int

PAGE_SIZE: Final[int] = 4096
FORMAT_VERSION: Final[int] = 1
INVALID_PAGE_ID: Final[int] = 0xFFFFFFFF
MIN_PAGE_ID: Final[int] = 0
MAX_PAGE_ID: Final[int] = 0xFFFFFFFE
HEADER_PAGE_ID: Final[int] = 0
CATALOG_ROOT_PAGE_ID: Final[int] = 1
FIRST_ALLOCATABLE_PAGE_ID: Final[int] = 2
INITIAL_NEXT_PAGE_ID: Final[int] = 2
# This is a boundary, not an allocated page: the sentinel is allowed here.
MAX_NEXT_PAGE_ID: Final[int] = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class BufferStats:
    """Immutable snapshot of one BufferPool's lifetime counters.

    Only validated get_page calls count as requests; failed disk reads still
    count as misses. Explicit frees are not evictions. Successful dirty-page
    writes count as writebacks. Reading this snapshot is not a page access.

    hit_rate is derived and included in dataclasses.asdict for trace output.
    Constructor errors indicate an internal programming error, not a storage
    operation failure; public storage APIs use the shared DbError separately.
    """

    requests: int = 0
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    writebacks: int = 0
    hit_rate: float = field(init=False)

    def __post_init__(self) -> None:
        for name in ("requests", "hits", "misses", "evictions", "writebacks"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int (not bool)")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.requests != self.hits + self.misses:
            raise ValueError("requests must equal hits + misses")
        rate = self.hits / self.requests if self.requests else 0.0
        object.__setattr__(self, "hit_rate", rate)
