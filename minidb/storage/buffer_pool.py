"""单线程页缓存：副本读写、LRU/FIFO、脏页刷新和生命周期统计。

业务页必须经由同一个 BufferPool 访问；page 0 仅由 FileManager 管理。
本模块不执行文件 fsync，也不关闭文件，生命周期由 StorageEngine 管理。
"""
import json
import logging
from dataclasses import dataclass
from typing import Callable, TypeVar

from minidb.core import errors
from minidb.core.disk_types import BufferStats, PAGE_SIZE, MAX_PAGE_ID
from minidb.storage.file_manager import FileManager
from minidb.storage.replacement import ReplacementPolicy

_LOG = logging.getLogger(__name__)
_T = TypeVar('_T')


@dataclass(slots=True)
class _Frame:
    data: bytes
    dirty: bool


class BufferPool:
    def __init__(self, file_manager: FileManager, capacity: int = 16,
                 policy: str = 'lru') -> None:
        for field, valid, expected, actual in (
            ('capacity', type(capacity) is int and capacity >= 1, 'int >= 1', repr(capacity)),
            ('policy', type(policy) is str and policy in ('lru', 'fifo'), 'lru or fifo', repr(policy)),
            ('file_manager', isinstance(file_manager, FileManager), 'FileManager', type(file_manager).__name__),
        ):
            if not valid:
                raise errors.DbError(errors.ErrorStage.STORAGE, errors.INVALID_ARGUMENT,
                                     '缓存构造参数无效', context={'operation': 'BufferPool',
                                     'field': field, 'expected': expected, 'actual': actual})
        self._file_manager = file_manager
        self._capacity = capacity
        self._replacement = ReplacementPolicy(policy)
        self._frames: dict[int, _Frame] = {}
        self._requests = self._hits = self._misses = 0
        self._evictions = self._writebacks = 0
        self._failure: errors.DbError | None = None

    @property
    def file_manager(self) -> FileManager:
        return self._file_manager

    def _ready(self) -> None:
        if self._failure is not None:
            raise self._failure
        # 无 I/O 的检查，同时使空缓存 flush 和缓存命中也拒绝已关闭文件。
        self._file_manager.validate_page_id(1)

    def _disk(self, fn: Callable[..., _T], *args) -> _T:
        try:
            return fn(*args)
        except errors.DbError as exc:
            if exc.code != errors.ID_EXHAUSTED:
                self._failure = exc
            raise

    def _event(self, event_type: str, page_id: int, dirty: bool, reason: str) -> None:
        event = {'kind': 'STORAGE_EVENT', 'event_type': event_type,
                 'page_id': page_id, 'dirty_before': dirty,
                 'policy': self._replacement.policy, 'reason': reason}
        # CLI 后续为该 logger 配置 stderr handler；此处不配置全局日志。
        _LOG.debug(json.dumps(event, ensure_ascii=False, sort_keys=True),
                   extra={'storage_event': event})

    def _writeback(self, page_id: int, reason: str) -> None:
        frame = self._frames[page_id]
        if frame.dirty:
            self._disk(self._file_manager.write_page, page_id, frame.data)
            frame.dirty = False
            self._writebacks += 1
            self._event('WRITEBACK', page_id, True, reason)

    def _make_room(self, reason: str) -> None:
        if len(self._frames) < self._capacity:
            return
        victim = self._replacement.victim()
        if victim is None or victim not in self._frames:
            self._failure = errors.DbError(
                errors.ErrorStage.EXECUTION, errors.INTERNAL_ERROR,
                '缓存与替换队列状态不一致', context={'operation': reason})
            raise self._failure
        self._writeback(victim, reason)
        # EVICT 发生在 WRITEBACK 后，此时该页已经是干净页。
        dirty_before = self._frames[victim].dirty
        del self._frames[victim]
        self._replacement.remove(victim)
        self._evictions += 1
        self._event('EVICT', victim, dirty_before, reason)

    def new_page(self) -> int:
        self._ready()
        # 先分配，页号耗尽时不会无谓淘汰缓存。后续失败须终止会话，不承诺回滚。
        page_id = self._disk(self._file_manager.allocate_page)
        self._make_room('new_page')
        self._frames[page_id] = _Frame(bytes(PAGE_SIZE), True)
        self._replacement.record_access(page_id)
        return page_id

    def get_page(self, page_id: int) -> bytes:
        self._ready()
        self._file_manager.validate_page_id(page_id)
        self._requests += 1
        if page_id in self._frames:
            self._hits += 1
            frame = self._frames[page_id]
            self._replacement.record_access(page_id)
            self._event('HIT', page_id, frame.dirty, 'get_page')
        else:
            self._misses += 1
            self._event('MISS', page_id, False, 'get_page')
            data = self._disk(self._file_manager.read_page, page_id)
            self._make_room('get_page')
            frame = _Frame(data, False)
            self._frames[page_id] = frame
            self._replacement.record_access(page_id)
            self._event('LOAD', page_id, False, 'get_page')
        # 数据为不可变 bytes，不暴露 frame 或可修改缓冲区。
        return memoryview(frame.data).tobytes()

    def write_page(self, page_id: int, data: bytes) -> None:
        self._ready()
        # 参数先按顺序校验类型/范围，随后才检查保留页和分配状态。
        if type(page_id) is not int or not 0 <= page_id <= MAX_PAGE_ID:
            self._file_manager.validate_page_id(page_id)
        if type(data) is not bytes or len(data) != PAGE_SIZE:
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.INVALID_ARGUMENT,
                                 '写缓存必须提交完整的 4096 字节 bytes',
                                 context={'operation': 'write_page', 'field': 'data',
                                 'expected': '4096 bytes', 'actual': repr(type(data).__name__) if type(data) is not bytes else len(data)})
        self._file_manager.validate_page_id(page_id)
        if page_id not in self._frames:
            self._make_room('write_page')
        self._frames[page_id] = _Frame(memoryview(data).tobytes(), True)
        self._replacement.record_access(page_id)

    def free_page(self, page_id: int) -> None:
        self._ready()
        self._file_manager.validate_page_id(page_id, for_release=True)
        frame = self._frames.pop(page_id, None)
        self._replacement.remove(page_id)
        self._disk(self._file_manager.release_page, page_id)
        self._event('FREE', page_id, frame.dirty if frame else False, 'free_page')

    def flush_page(self, page_id: int) -> None:
        self._ready()
        self._file_manager.validate_page_id(page_id)
        if page_id in self._frames:
            self._writeback(page_id, 'flush_page')

    def flush_all(self) -> None:
        self._ready()
        for page_id in sorted(self._frames):
            self._writeback(page_id, 'flush_all')

    def stats(self) -> BufferStats:
        # 失败后仍可取统计证据，不触发磁盘操作或重新尝试写入。
        return BufferStats(self._requests, self._hits, self._misses,
                           self._evictions, self._writebacks)
