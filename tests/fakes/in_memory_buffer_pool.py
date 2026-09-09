"""TEST-ONLY BufferPool and FileManager implementations.

The method signatures match the production BufferPool. This test double reuses
the official page constants, BufferStats value object and replacement ordering,
but keeps all pages in memory so StorageEngine tests can inject precise faults.

This fake models caching behavior but not a real file, short I/O, file-header
serialization or restart persistence. Production code must not import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from minidb.core.disk_types import BufferStats, PAGE_SIZE
from minidb.storage.replacement import ReplacementPolicy


class FileManagerLike(Protocol):
    """Only the production methods used by this isolated test double."""

    def validate_page_id(
        self,
        page_id: int,
        *,
        allow_header: bool = False,
        for_release: bool = False,
    ) -> None: ...

    def read_page(self, page_id: int) -> bytes: ...

    def write_page(self, page_id: int, data: bytes) -> None: ...

    def allocate_page(self) -> int: ...

    def release_page(self, page_id: int) -> None: ...

    def sync(self) -> None: ...

    def close(self) -> None: ...


class InMemoryFileManager:
    """Minimal page allocator used below the temporary BufferPool.

    Page 0 and page 1 are reserved on construction, matching a newly created
    database file. Page bytes live in a dictionary instead of on disk.
    """

    def __init__(self) -> None:
        self._pages: dict[int, bytes] = {
            0: bytes(PAGE_SIZE),
            1: bytes(PAGE_SIZE),
        }
        self._free_pages: set[int] = set()
        self._next_page_id = 2
        self._closed = False
        # 模拟 FileManager.open 创建了一个全新文件。StorageEngine 只有在
        # 这个标记为 True 且 page 1 全零时才允许初始化系统目录。
        self._is_new = True
        self.sync_count = 0
        self.read_log: list[int] = []
        self.write_log: list[int] = []

    @property
    def is_new(self) -> bool:
        """与正式 FileManager 的只读启动状态保持同名。"""

        return self._is_new

    @property
    def is_closed(self) -> bool:
        """测试清理路径时观察底层资源是否已经关闭。"""

        return self._closed

    def validate_page_id(
        self,
        page_id: int,
        *,
        allow_header: bool = False,
        for_release: bool = False,
    ) -> None:
        self._require_open()
        if not isinstance(allow_header, bool) or not isinstance(for_release, bool):
            raise TypeError("page validation flags must be bool")
        if allow_header and for_release:
            raise ValueError("allow_header and for_release cannot both be true")
        if isinstance(page_id, bool) or not isinstance(page_id, int):
            raise TypeError("page_id must be an int")
        if not 0 <= page_id <= 0xFFFFFFFE:
            raise ValueError("page_id is outside the supported range")
        if page_id == 0 and not allow_header:
            raise ValueError("page 0 is reserved for the file header")
        if for_release and page_id in (0, 1):
            raise ValueError("page 0 and page 1 cannot be released")
        if page_id in self._free_pages:
            if for_release:
                raise ValueError("page is already free")
            raise KeyError("page is not allocated")
        if page_id not in self._pages:
            raise KeyError("page is not allocated")

    def read_page(self, page_id: int) -> bytes:
        self.validate_page_id(page_id, allow_header=page_id == 0)
        self.read_log.append(page_id)
        return bytes(bytearray(self._pages[page_id]))

    def write_page(self, page_id: int, data: bytes) -> None:
        self.validate_page_id(page_id, allow_header=page_id == 0)
        if not isinstance(data, bytes) or len(data) != PAGE_SIZE:
            raise ValueError(f"data must be exactly {PAGE_SIZE} bytes")
        self._pages[page_id] = bytes(bytearray(data))
        self.write_log.append(page_id)

    def allocate_page(self) -> int:
        self._require_open()
        if self._free_pages:
            page_id = min(self._free_pages)
            self._free_pages.remove(page_id)
        else:
            if self._next_page_id > 0xFFFFFFFE:
                raise OverflowError("page id limit reached")
            page_id = self._next_page_id
            self._next_page_id += 1
        self._pages[page_id] = bytes(PAGE_SIZE)
        return page_id

    def release_page(self, page_id: int) -> None:
        self.validate_page_id(page_id, for_release=True)
        self._pages[page_id] = bytes(PAGE_SIZE)
        self._free_pages.add(page_id)

    def sync(self) -> None:
        self._require_open()
        self.sync_count += 1

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("file manager is closed")


@dataclass(slots=True)
class _Frame:
    data: bytes
    dirty: bool


class InMemoryBufferPool:
    """Behavioral BufferPool test double with LRU and FIFO replacement."""

    def __init__(
        self,
        file_manager: FileManagerLike,
        capacity: int = 16,
        policy: str = "lru",
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be an int of at least 1")
        if policy not in ("lru", "fifo"):
            raise ValueError("policy must be 'lru' or 'fifo'")

        self._file_manager = file_manager
        self._capacity = capacity
        self._policy = policy
        self._frames: dict[int, _Frame] = {}
        self._replacement = ReplacementPolicy(policy)
        self._requests = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._writebacks = 0

    @property
    def file_manager(self) -> FileManagerLike:
        """Return the exact object passed to the constructor."""

        return self._file_manager

    @property
    def cached_page_ids(self) -> tuple[int, ...]:
        """Test-only replacement-order snapshot, oldest candidate first."""

        return self._replacement.snapshot()

    def new_page(self) -> int:
        page_id = self._file_manager.allocate_page()
        self._make_room()
        self._frames[page_id] = _Frame(bytes(PAGE_SIZE), dirty=True)
        self._replacement.record_access(page_id)
        return page_id

    def get_page(self, page_id: int) -> bytes:
        self._file_manager.validate_page_id(page_id)
        self._requests += 1

        frame = self._frames.get(page_id)
        if frame is not None:
            self._hits += 1
            self._record_access(page_id)
            return bytes(bytearray(frame.data))

        self._misses += 1
        # Read before eviction: a failed read must leave existing cache state.
        data = self._file_manager.read_page(page_id)
        self._make_room()
        self._frames[page_id] = _Frame(bytes(bytearray(data)), dirty=False)
        self._replacement.record_access(page_id)
        return bytes(bytearray(data))

    def write_page(self, page_id: int, data: bytes) -> None:
        self._file_manager.validate_page_id(page_id)
        if not isinstance(data, bytes) or len(data) != PAGE_SIZE:
            raise ValueError(f"data must be bytes of length {PAGE_SIZE}")

        copied = bytes(bytearray(data))
        frame = self._frames.get(page_id)
        if frame is None:
            self._make_room()
            self._frames[page_id] = _Frame(copied, dirty=True)
            self._replacement.record_access(page_id)
            return

        frame.data = copied
        frame.dirty = True
        self._record_access(page_id)

    def free_page(self, page_id: int) -> None:
        self._file_manager.validate_page_id(page_id, for_release=True)
        # Discard dirty cached content without writing it back; callers must
        # detach the page from their table chain before invoking this method.
        self._frames.pop(page_id, None)
        self._replacement.remove(page_id)
        self._file_manager.release_page(page_id)

    def flush_page(self, page_id: int) -> None:
        self._file_manager.validate_page_id(page_id)
        frame = self._frames.get(page_id)
        if frame is None or not frame.dirty:
            return
        self._file_manager.write_page(page_id, frame.data)
        frame.dirty = False
        self._writebacks += 1

    def flush_all(self) -> None:
        for page_id in sorted(self._frames):
            self.flush_page(page_id)

    def stats(self) -> BufferStats:
        return BufferStats(
            requests=self._requests,
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
            writebacks=self._writebacks,
        )

    def _make_room(self) -> None:
        if len(self._frames) < self._capacity:
            return

        victim_page_id = self._replacement.victim()
        if victim_page_id is None:
            raise RuntimeError("replacement state is inconsistent with the cache")
        victim = self._frames[victim_page_id]
        if victim.dirty:
            # Keep the dirty victim cached if its write fails.
            self._file_manager.write_page(victim_page_id, victim.data)
            victim.dirty = False
            self._writebacks += 1
        del self._frames[victim_page_id]
        self._replacement.remove(victim_page_id)
        self._evictions += 1

    def _record_access(self, page_id: int) -> None:
        self._replacement.record_access(page_id)
