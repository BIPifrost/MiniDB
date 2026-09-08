"""Physical file I/O for MiniDB format 1 (allocation/release not implemented yet).

Ordinary business page access must go through the future BufferPool. This
layer owns page 0 and free-list metadata. It does not validate DataPage slots.
Errors use the shared core contract, also consumed by Schema and Catalog.
"""

import os
from pathlib import Path
from typing import BinaryIO

from minidb.core.disk_types import PAGE_SIZE, MAX_PAGE_ID, INVALID_PAGE_ID
from minidb.storage.page import (
    FileHeader, initial_file_header_page, decode_file_header, decode_free_page,
)
# 使用已移到公共目录的同一套错误定义，目录层可以直接识别底层异常。
from minidb.core import errors


class FileManager:
    def __init__(self, path: str, handle: BinaryIO, *, is_new: bool) -> None:
        # Internal construction only; use open() for initialization/validation.
        self._path = path
        self._handle = handle
        self._is_new = is_new
        self._closed = False
        self._header = FileHeader()
        self._free_pages: set[int] = set()

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
        actual_path = os.path.abspath(path)
        handle = None
        is_new = False
        try:
            try:
                handle = open(actual_path, 'r+b', buffering=0)
            except FileNotFoundError:
                Path(actual_path).parent.mkdir(parents=True, exist_ok=True)
                try:
                    handle = open(actual_path, 'x+b', buffering=0)
                    is_new = True
                except FileExistsError:
                    # Another creator won the race; never truncate its file.
                    handle = open(actual_path, 'r+b', buffering=0)
        except OSError as exc:
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.IO_OPEN_FAILED,
                                 'Cannot open database file',
                                 context={'path': actual_path, 'operation': 'open',
                                          'cause': str(exc)}) from exc
        manager = cls(actual_path, handle, is_new=is_new)
        try:
            if is_new:
                manager._write_raw(0, initial_file_header_page())
                manager._write_raw(1, bytes(PAGE_SIZE))
            manager._load_metadata()
            return manager
        except BaseException as original:
            try:
                manager.close()
            except errors.DbError as cleanup:
                if isinstance(original, errors.DbError):
                    original.context.setdefault('cleanup_errors', []).append({
                        'stage': cleanup.stage.name, 'code': cleanup.code,
                        'message': cleanup.message, 'span': None, 'context': cleanup.context,
                    })
                else:
                    original.add_note(str(cleanup))
            raise

    def _ensure_open(self, operation: str) -> None:
        if self._closed:
            raise self._error(errors.CLOSED, operation, resource='FileManager')

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
        self._header = decode_file_header(data, file_size=size, path=self._path)
        current = self._header.free_head
        free_pages: set[int] = set()
        while current != INVALID_PAGE_ID:
            if current in free_pages:
                raise self._error(errors.DB_FORMAT_MISMATCH, 'load_free_list',
                                  field='free_head', page_id=current,
                                  expected='acyclic free list', actual=current,
                                  reason='repeated page in free list')
            free_pages.add(current)
            try:
                current = decode_free_page(self._read_raw(current), page_id=current,
                                           next_page_id=self._header.next_page_id)
            except errors.DbError as exc:
                exc.context.setdefault('path', self._path)
                raise
        self._free_pages = free_pages

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
        if (page_id == 0 and not allow_header) or (for_release and page_id <= 1):
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
        self._ensure_open('write_page')
        self._check_page_number(page_id, 'write_page')
        if type(data) is not bytes or len(data) != PAGE_SIZE:
            raise self._error(errors.INVALID_ARGUMENT, 'write_page', field='data',
                              expected='4096 bytes', actual=(len(data) if type(data) is bytes else type(data).__name__))
        self.validate_page_id(page_id)
        self._write_raw(page_id, data)

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
        except OSError as exc:
            # Do not falsely mark an unreleased handle as closed.
            raise self._error(errors.IO_CLOSE_FAILED, 'close', cause=str(exc)) from exc
        self._closed = True
