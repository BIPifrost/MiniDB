"""Shared physical storage definitions for MiniDB specification v1.1.

This module performs no I/O and does not depend on storage implementations.
All multi-byte disk fields use little-endian encoding (format version 1).
"""

from dataclasses import dataclass, field
from typing import Final, TypeAlias
from uuid import UUID

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
# v2 独立格式边界；现有 v1 入口仍使用上方无前缀常量。
V2_FORMAT_VERSION: Final[int] = 2
V2_INDEX_CATALOG_ROOT_PAGE_ID: Final[int] = 2
V2_FIRST_ALLOCATABLE_PAGE_ID: Final[int] = 3
V2_MAX_FILE_SIZE: Final[int] = 64 * 1024 * 1024
V2_MAX_PAGE_COUNT: Final[int] = V2_MAX_FILE_SIZE // PAGE_SIZE

# This is a boundary, not an allocated page: the sentinel is allowed here.
MAX_NEXT_PAGE_ID: Final[int] = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """不可变整页副本及会话内版本；不改变磁盘格式。

    _owner 仅用于拒绝跨缓存误用，不是针对恶意 Python 调用者的安全机制。
    手工构造的快照不能提交，业务调用者应使用 get_snapshot。
    """
    page_id: int
    data: bytes
    revision: int
    _owner: object = field(default=None, kw_only=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.page_id) is not int or not 1 <= self.page_id <= MAX_PAGE_ID:
            raise ValueError('page_id must be an ordinary page id')
        if type(self.data) is not bytes or len(self.data) != PAGE_SIZE:
            raise ValueError('data must be 4096 bytes')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('revision must be a positive int')


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


@dataclass(frozen=True, slots=True)
class FileImageInfo:
    """主库镜像信息；事务 UUID 由上层日志管理者另行添加。"""
    original_length: int
    database_uuid: UUID
    payload_sha256: bytes

    def __post_init__(self) -> None:
        if (type(self.original_length) is not int or
                not 3 * PAGE_SIZE <= self.original_length <= V2_MAX_FILE_SIZE or
                self.original_length % PAGE_SIZE):
            raise ValueError('original_length must be a valid v2 file length')
        if not isinstance(self.database_uuid, UUID):
            raise TypeError('database_uuid must be UUID')
        if type(self.payload_sha256) is not bytes or len(self.payload_sha256) != 32:
            raise ValueError('payload_sha256 must contain 32 bytes')


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """文件镜像加事务身份；日志 sequence 单独保存，仅用于诊断。"""
    original_length: int
    database_uuid: UUID
    transaction_uuid: UUID
    payload_sha256: bytes

    def __post_init__(self) -> None:
        FileImageInfo(self.original_length, self.database_uuid, self.payload_sha256)
        if not isinstance(self.transaction_uuid, UUID):
            raise TypeError('transaction_uuid must be UUID')
