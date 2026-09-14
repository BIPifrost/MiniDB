"""Physical file I/O and page allocation; explicit v1 and v2 entry points.

Ordinary business page access must go through the BufferPool. This
layer owns page 0 and free-list metadata. It does not validate DataPage slots.
Errors use the shared core contract, also consumed by Schema and Catalog.
"""

import os
import io
import tempfile
import weakref
from dataclasses import replace
from typing import BinaryIO

from minidb.core.disk_types import PAGE_SIZE, MAX_PAGE_ID, INVALID_PAGE_ID
from minidb.storage.page import (
    FileHeader, initial_file_header_page, decode_file_header, decode_free_page,
    encode_file_header, encode_free_page,
)
# 使用已移到公共目录的同一套错误定义，目录层可以直接识别底层异常。
from minidb.core import errors
from minidb.storage.file_lock import DatabaseLock
from minidb.storage import page as page_v1, page_v2
from minidb.core.disk_types import V2_MAX_PAGE_COUNT, V2_MAX_FILE_SIZE, FileImageInfo
from minidb.storage import file_snapshot
from minidb.core.transaction import TransactionGuard, TransactionState



class FileManager:
    def __init__(self, path: str, handle: BinaryIO, *, is_new: bool, lock: DatabaseLock | None = None) -> None:
        # Internal construction only; use open() for initialization/validation.
        self._path = path
        self._handle = handle
        self._lock = lock
        self._is_new = is_new
        self._closed = False
        self._header = FileHeader()
        self._free_pages: set[int] = set()
        self._page_codec = page_v1
        self._first_allocatable = 2
        self._guard: TransactionGuard | None = None
        self._buffer_pools = weakref.WeakSet()
        self._restore_failure: errors.DbError | None = None

    @property
    def is_new(self) -> bool:
        return self._is_new

    def _error(self, code: str, operation: str, **context) -> errors.DbError:
        return errors.DbError(
            errors.ErrorStage.STORAGE, code, f'{operation} failed',
            context={'path': self._path, 'operation': operation, **context},
        )

    @classmethod
    def open(cls, path: str) -> 'FileManager':
        if type(path) is not str or not path or '\x00' in path:
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.INVALID_ARGUMENT,
                                 'path must be a nonempty string without NUL',
                                 context={'operation': 'open', 'field': 'path',
                                          'expected': 'nonempty path str', 'actual': repr(path)})
        lock = DatabaseLock.acquire(path)
        manager = cls(lock.path, lock.handle, is_new=lock.is_new, lock=lock)
        try:
            if manager.is_new:
                manager._write_raw(0, initial_file_header_page())
                manager._write_raw(1, bytes(PAGE_SIZE))
            manager._load_metadata()
            lock._file_manager_owner = manager
            return manager
        except BaseException as original:
            try:
                manager.close()
            except errors.DbError as cleanup:
                if isinstance(original, errors.DbError):
                    original._update_context(cleanup_errors=[*original.context.get('cleanup_errors', ()), {
                        'stage': cleanup.stage.name, 'code': cleanup.code,
                        'message': cleanup.message, 'context': cleanup.context,
                    }])
                else:
                    original.add_note(str(cleanup))
            raise

    @classmethod
    def create_v2(cls, path: str):
        """创建并发布完整 v2 空库，返回 UUID；随后按已有库 open_locked。

        返回时不持锁，不返回尚未初始化的 FileManager。目录页只初始化一次。
        """
        from minidb.storage.database_creation import create_v2_database
        return create_v2_database(path)

    @classmethod
    def open_locked(cls, path: str, lock_handle: DatabaseLock, *,
                    guard: TransactionGuard | None = None) -> 'FileManager':
        """在调用方已有的锁上打开 v2；不新建、不重开、不原地升级。

        成功后由 FileManager.close 释放锁；失败时锁仍归调用方，供恢复
        或清理使用。未注入 guard 时只允许读取；可在装配时 bind_guard。
        这里只校验物理文件与空闲链，目录页内容由 Catalog 校验。
        """
        if (type(path) is not str or not path or '\x00' in path or
                not isinstance(lock_handle, DatabaseLock)):
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.INVALID_ARGUMENT,
                                 'open_locked 要求有效路径和 DatabaseLock',
                                 context={'operation': 'open_locked'})
        actual = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        if actual != lock_handle.path:
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.INVALID_ARGUMENT,
                                 '路径与锁句柄不属于同一主文件',
                                 context={'operation': 'open_locked', 'path': actual})
        failure = getattr(lock_handle, '_recovery_failure', None)
        if failure is not None:
            raise failure
        manager = cls(actual, lock_handle.handle, is_new=lock_handle.is_new, lock=lock_handle)
        if lock_handle.handle.closed or lock_handle.path_handle.closed:
            raise manager._error(errors.CLOSED, 'open_locked', resource='DatabaseLock')
        if getattr(lock_handle, '_file_manager_owner', None) is not None:
            raise manager._error(errors.INVALID_ARGUMENT, 'open_locked', reason='lock already owned')
        if guard is not None and not isinstance(guard, TransactionGuard):
            raise manager._error(errors.INVALID_ARGUMENT, 'open_locked', field='guard')
        manager._page_codec = page_v2
        manager._first_allocatable = 3
        manager._guard = guard
        manager._load_metadata()
        lock_handle._file_manager_owner = manager
        return manager

    def bind_guard(self, guard: TransactionGuard) -> None:
        """装配时绑定一次状态守卫，禁止替换为另一个会话的 guard。"""
        self._ensure_open('bind_guard')
        if self._page_codec is not page_v2 or not isinstance(guard, TransactionGuard):
            raise self._error(errors.INVALID_ARGUMENT, 'bind_guard', field='guard')
        if self._guard is not None and self._guard is not guard:
            raise self._error(errors.INVALID_ARGUMENT, 'bind_guard', reason='guard already bound')
        self._guard = guard

    @property
    def database_uuid(self):
        """v2 的持久 UUID；v1 无此字段，返回 None。"""
        self._ensure_open('database_uuid')
        return getattr(self._header, 'database_uuid', None)

    def _require_write(self, operation: str) -> None:
        self._ensure_open(operation)
        if self._page_codec is page_v2:
            if self._guard is None:
                raise self._error(errors.TRANSACTION_REQUIRED, operation)
            self._guard.require(TransactionState.ACTIVE, TransactionState.BOOTSTRAP,
                                TransactionState.RECOVERY, operation=operation)

    def _ensure_open(self, operation: str) -> None:
        if self._closed or self._handle.closed is True:
            raise self._error(errors.CLOSED, operation, resource='FileManager')
        if self._restore_failure is not None:
            raise self._restore_failure

    def _read_raw(self, page_id: int) -> bytes:
        try:
            self._handle.seek(page_id * PAGE_SIZE)
            data = self._handle.read(PAGE_SIZE)
        except OSError as exc:
            raise self._error(errors.IO_READ_FAILED, 'read_page', page_id=page_id,
                              cause=str(exc)) from exc
        if data is None or len(data) != PAGE_SIZE:
            raise self._error(errors.DB_FILE_TRUNCATED, 'read_page', page_id=page_id,
                              expected=PAGE_SIZE, actual=0 if data is None else len(data))
        return data

    def _write_raw(self, page_id: int, data: bytes) -> None:
        try:
            self._handle.seek(page_id * PAGE_SIZE)
            written = 0
            while written < PAGE_SIZE:
                count = self._handle.write(data[written:])
                if type(count) is not int or count <= 0 or count > PAGE_SIZE - written:
                    raise self._error(errors.IO_WRITE_FAILED, 'write_page', page_id=page_id,
                                      expected=PAGE_SIZE, actual=written, reason='short write made no progress')
                written += count
        except OSError as exc:
            raise self._error(errors.IO_WRITE_FAILED, 'write_page', page_id=page_id,
                              cause=str(exc)) from exc

    def _load_metadata(self) -> None:
        data = self._read_raw(0)
        try:
            size = os.fstat(self._handle.fileno()).st_size
        except OSError as exc:
            raise self._error(errors.IO_READ_FAILED, 'file_size', cause=str(exc)) from exc
        if self._page_codec is page_v2:
            if data[:8] != page_v2.FILE_MAGIC or int.from_bytes(data[8:12], 'little') != 2:
                raise self._error(errors.FORMAT_VERSION_UNSUPPORTED, 'load_metadata',
                                  expected=2, actual=int.from_bytes(data[8:12], 'little'))
            header = page_v2.decode_file_header(data, file_size=size)
            if isinstance(self._header, page_v2.FileHeaderV2) and header.database_uuid != self._header.database_uuid:
                raise self._error(errors.DB_FORMAT_MISMATCH, 'load_metadata', field='database_uuid',
                                  expected=str(self._header.database_uuid), actual=str(header.database_uuid))
        else:
            header = decode_file_header(data, file_size=size, path=self._path)
        current = header.free_head
        free_pages: set[int] = set()
        while current != INVALID_PAGE_ID:
            if current in free_pages:
                raise self._error(errors.DB_FORMAT_MISMATCH, 'load_free_list',
                                  field='free_head', page_id=current,
                                  expected='acyclic free list', actual=current,
                                  reason='repeated page in free list')
            free_pages.add(current)
            try:
                current = self._page_codec.decode_free_page(self._read_raw(current), page_id=current,
                                           next_page_id=header.next_page_id)
            except errors.DbError as exc:
                exc._update_context(path=self._path)
                raise
        # 完整校验成功后一起发布，避免新文件头与旧空闲链混用。
        self._header = header
        self._free_pages = free_pages

    def reload_metadata(self) -> None:
        """从当前持锁句柄重新加载元数据；不写盘，也不刷新缓存。

        恢复协调方须先丢弃 BufferPool 缓存，恢复文件后再调用本接口。
        校验失败时保留原内存元数据；调用方必须中止本次恢复。
        """
        self._ensure_open('reload_metadata')
        self._load_metadata()

    def _register_buffer_pool(self, pool) -> None:
        self._buffer_pools.add(pool)

    def _require_snapshot_state(self, operation, *states):
        self._ensure_open(operation)
        if self._page_codec is not page_v2:
            raise self._error(errors.FORMAT_VERSION_UNSUPPORTED, operation, expected=2, actual=1)
        if self._lock is None or self._lock.path_handle.closed or self._lock.handle is not self._handle:
            raise self._error(errors.CLOSED, operation, resource='DatabaseLock')
        if self._guard is None:
            raise self._error(errors.TRANSACTION_REQUIRED, operation)
        self._guard.require(*states, operation=operation)

    def _check_snapshot_stream(self, stream, method, operation):
        if not callable(getattr(stream, method, None)):
            raise self._error(errors.INVALID_ARGUMENT, operation, field='stream')
        # 不允许把主库自身或路径锁载体当成快照流。
        if stream is self._handle or stream is self._lock.path_handle:
            raise self._error(errors.INVALID_ARGUMENT, operation, reason='snapshot aliases database lock')
        try:
            stat = os.fstat(stream.fileno())
        except (AttributeError, io.UnsupportedOperation):
            return  # BytesIO 等无文件描述符的二进制流。
        except (OSError, ValueError) as exc:
            raise self._error(errors.INVALID_ARGUMENT, operation, cause=str(exc)) from exc
        for owned in (self._handle, self._lock.path_handle):
            own = os.fstat(owned.fileno())
            if (stat.st_dev, stat.st_ino) == (own.st_dev, own.st_ino):
                raise self._error(errors.INVALID_ARGUMENT, operation, reason='snapshot aliases database lock')

    def export_consistent_snapshot(self, destination_handle) -> FileImageInfo:
        """PREPARING 下复制已落盘主库；不替调用方 flush/fsync 快照日志。

        从目标流当前位置写入恰好 original_length 字节，允许日志预留头。
        有脏缓存则拒绝，不偷偷写回后把新旧事务混入同一镜像。
        """
        op = 'export_consistent_snapshot'
        self._require_snapshot_state(op, TransactionState.PREPARING)
        self._check_snapshot_stream(destination_handle, 'write', op)
        if any(pool.has_dirty_pages for pool in self._buffer_pools):
            raise self._error(errors.INVALID_TRANSACTION_STATE, op, reason='dirty buffer before snapshot')
        self._load_metadata()
        length = self._header.next_page_id * PAGE_SIZE
        try:
            self._handle.seek(0)
            digest = file_snapshot.copy_payload(self._handle, destination_handle, length)
        except OSError as exc:
            raise self._error(errors.IO_READ_FAILED, op, cause=str(exc)) from exc
        return FileImageInfo(length, self._header.database_uuid, digest)

    def restore_snapshot(self, source_handle, original_length: int, *,
                         expected_sha256: bytes | None = None) -> None:
        """从流的当前位置恢复原始主库；不消费后面的日志提交尾。

        正式日志调用方须先验证日志身份及摘要；也可传 expected_sha256
        在这里复核摘要。不传摘要时只验证文件头、UUID、长度和空闲链，
        不能据此声称任意业务页字节损坏均可检测。
        校验期间暂存到有界临时流，完整校验通过前不改主库或缓存。
        开始恢复后失败则封锁本实例；保留调用方日志供重启重做。
        """
        op = 'restore_snapshot'
        self._require_snapshot_state(op, TransactionState.RECOVERY, TransactionState.ROLLING_BACK)
        self._check_snapshot_stream(source_handle, 'read', op)
        if (type(original_length) is not int or original_length % PAGE_SIZE or
                not 3 * PAGE_SIZE <= original_length <= V2_MAX_FILE_SIZE):
            raise self._error(errors.INVALID_ARGUMENT, op, field='original_length')
        if expected_sha256 is not None and (type(expected_sha256) is not bytes or len(expected_sha256) != 32):
            raise self._error(errors.INVALID_ARGUMENT, op, field='expected_sha256')
        started = False
        try:
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b') as staged:
                digest = file_snapshot.copy_payload(source_handle, staged, original_length)
                if expected_sha256 is not None and digest != expected_sha256:
                    raise self._error(errors.RECOVERY_FAILED, op, reason='payload SHA-256 mismatch')
                file_snapshot.validate_image(staged, original_length, self._header.database_uuid)
                # 单线程同步路径；先丢弃全部旧版本和脏页，再开始恢复。
                for pool in self._buffer_pools:
                    pool._discard_for_restore()
                started = True
                self._handle.seek(0)
                file_snapshot.copy_payload(staged, self._handle, original_length)
                self._handle.truncate(original_length)
                self.sync()
                self._load_metadata()
        except (OSError, ValueError) as exc:
            failure = self._error(errors.RECOVERY_FAILED, op, cause=str(exc))
            if started:
                self._restore_failure = failure
                self._guard.fail(operation=op)
            raise failure from exc
        except errors.DbError as exc:
            if started:
                self._restore_failure = exc
                self._guard.fail(operation=op)
            raise
        except BaseException as exc:
            # KeyboardInterrupt/SystemExit 仍向上传播，但不能留下可继续访问的半恢复实例。
            if started:
                self._restore_failure = self._error(errors.RECOVERY_FAILED, op,
                                                   cause=type(exc).__name__)
                self._guard.fail(operation=op)
            raise

    def _check_page_number(self, page_id: int, operation: str) -> None:
        if type(page_id) is not int or not 0 <= page_id <= MAX_PAGE_ID:
            raise self._error(errors.PAGE_ID_INVALID, operation, value_repr=repr(page_id),
                              min_value=0, max_value=MAX_PAGE_ID)

    def validate_page_id(self, page_id: int, *, allow_header: bool = False,
                         for_release: bool = False) -> None:
        self._ensure_open('validate_page_id')
        self._check_page_number(page_id, 'validate_page_id')
        if type(allow_header) is not bool or type(for_release) is not bool or (allow_header and for_release):
            raise self._error(errors.INVALID_ARGUMENT, 'validate_page_id', field='flags',
                              expected='bool flags, not both True',
                              actual=[repr(allow_header), repr(for_release)])
        if (page_id == 0 and not allow_header) or (for_release and page_id < self._first_allocatable):
            raise self._error(errors.RESERVED_PAGE, 'validate_page_id', page_id=page_id)
        if page_id >= self._header.next_page_id:
            raise self._error(errors.PAGE_NOT_ALLOCATED, 'validate_page_id', page_id=page_id)
        if page_id in self._free_pages:
            code = errors.PAGE_ALREADY_FREE if for_release else errors.PAGE_NOT_ALLOCATED
            raise self._error(code, 'validate_page_id', page_id=page_id)

    def read_page(self, page_id: int) -> bytes:
        self.validate_page_id(page_id, allow_header=True)
        return self._read_raw(page_id)

    def write_page(self, page_id: int, data: bytes) -> None:
        self._require_write('write_page')
        self._check_page_number(page_id, 'write_page')
        if type(data) is not bytes or len(data) != PAGE_SIZE:
            raise self._error(errors.INVALID_ARGUMENT, 'write_page', field='data',
                              expected='4096 bytes', actual=(len(data) if type(data) is bytes else type(data).__name__))
        self.validate_page_id(page_id)
        self._write_raw(page_id, data)

    def _writeback_page(self, page_id: int, data: bytes) -> None:
        """仅供缓存写回；COMMITTING 可落盘已有脏页，普通 write_page 仍拒绝。"""
        if self._page_codec is not page_v2:
            return self.write_page(page_id, data)
        self._ensure_open('writeback_page')
        if self._guard is None:
            raise self._error(errors.TRANSACTION_REQUIRED, 'writeback_page')
        self._guard.require(TransactionState.ACTIVE, TransactionState.BOOTSTRAP,
                            TransactionState.RECOVERY, TransactionState.COMMITTING,
                            operation='writeback_page')
        self.validate_page_id(page_id)
        if type(data) is not bytes or len(data) != PAGE_SIZE:
            raise self._error(errors.INVALID_ARGUMENT, 'writeback_page', field='data')
        self._write_raw(page_id, data)

    def allocate_page(self) -> int:
        """复用空闲链头，否则追加全零页；成功后发布新的内存元数据。

        不自动 fsync，由上层在完整写语句完成后同步。任何 I/O 失败都
        必须由上层终止会话；本期不保证多页写入失败时的原子回滚。
        """
        self._require_write('allocate_page')
        old = self._header
        if old.free_head != INVALID_PAGE_ID:
            page_id = old.free_head
            if page_id not in self._free_pages:
                raise self._error(errors.DB_FORMAT_MISMATCH, 'allocate_page',
                                  field='free_head', page_id=page_id,
                                  expected='member of free page set', actual=page_id)
            try:
                successor = self._page_codec.decode_free_page(self._read_raw(page_id), page_id=page_id,
                                             next_page_id=old.next_page_id)
            except errors.DbError as exc:
                exc._update_context(path=self._path)
                raise
            if successor != INVALID_PAGE_ID and successor not in self._free_pages:
                raise self._error(errors.DB_FORMAT_MISMATCH, 'allocate_page',
                                  field='next_free_page_id', page_id=page_id,
                                  expected='free page or sentinel', actual=successor)
            updated = replace(old, free_head=successor)
        else:
            page_id = old.next_page_id
            if self._page_codec is page_v2 and page_id >= V2_MAX_PAGE_COUNT:
                raise errors.DbError(errors.ErrorStage.EXECUTION, errors.RESOURCE_LIMIT,
                                     'v2 主库超过 64 MiB 上限',
                                     context={'operation': 'allocate_page', 'kind': 'database_pages',
                                              'limit': V2_MAX_PAGE_COUNT, 'actual': page_id + 1})
            if page_id > MAX_PAGE_ID:
                raise self._error(errors.ID_EXHAUSTED, 'allocate_page',
                                  id_kind='page', limit=MAX_PAGE_ID)
            updated = replace(old, next_page_id=page_id + 1, free_head=INVALID_PAGE_ID)
        header_bytes = self._page_codec.encode_file_header(updated)
        self._write_raw(page_id, bytes(PAGE_SIZE))
        self._write_raw(0, header_bytes)
        self._header = updated
        self._free_pages.discard(page_id)
        return page_id

    def release_page(self, page_id: int) -> None:
        """将页加入空闲链头，不截短文件；上层须先摘链并失效缓存。"""
        self._require_write('release_page')
        self.validate_page_id(page_id, for_release=True)
        old = self._header
        free_bytes = self._page_codec.encode_free_page(old.free_head, next_page_id=old.next_page_id)
        updated = replace(old, free_head=page_id)
        header_bytes = self._page_codec.encode_file_header(updated)
        self._write_raw(page_id, free_bytes)
        self._write_raw(0, header_bytes)
        self._header = updated
        self._free_pages.add(page_id)

    def sync(self) -> None:
        self._ensure_open('sync')
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as exc:
            raise self._error(errors.IO_SYNC_FAILED, 'sync', cause=str(exc)) from exc

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._handle.close()
            if self._lock is not None:
                self._lock.close()
        except OSError as exc:
            # Do not falsely mark an unreleased handle as closed.
            raise self._error(errors.IO_CLOSE_FAILED, 'close', cause=str(exc)) from exc
        self._closed = True
