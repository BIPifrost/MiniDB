"""File-header and free-page layout for MiniDB disk format 1.

This module does not describe DataPage record slots (owned by data_page.py).
Pure byte encoding and decoding use the temporary storage-local DbError.
File I/O, allocation state and free-chain traversal belong to FileManager.
No database file is opened or modified by these helpers.
"""

from struct import Struct
from typing import Final

from minidb.core.disk_types import (
    CATALOG_ROOT_PAGE_ID, FORMAT_VERSION, INITIAL_NEXT_PAGE_ID,
    INVALID_PAGE_ID, PAGE_SIZE,
)

FILE_MAGIC: Final[bytes] = b"MINIDB01"
FILE_MAGIC_OFFSET: Final[int] = 0
FILE_VERSION_OFFSET: Final[int] = 8
FILE_PAGE_SIZE_OFFSET: Final[int] = 12
FILE_CATALOG_ROOT_OFFSET: Final[int] = 16
FILE_NEXT_PAGE_ID_OFFSET: Final[int] = 20
FILE_FREE_HEAD_OFFSET: Final[int] = 24
FILE_RESERVED_OFFSET: Final[int] = 28
FILE_RESERVED_SIZE: Final[int] = PAGE_SIZE - FILE_RESERVED_OFFSET

FREE_NEXT_PAGE_ID_OFFSET: Final[int] = 0
FREE_RESERVED_OFFSET: Final[int] = 4
FREE_RESERVED_SIZE: Final[int] = PAGE_SIZE - FREE_RESERVED_OFFSET

# Explicit '<' prevents native alignment and fixes little-endian byte order.
FILE_HEADER_STRUCT: Final[Struct] = Struct("<8sIIIII")
FREE_NEXT_STRUCT: Final[Struct] = Struct("<I")


def initial_file_header_page() -> bytes:
    """Return page 0 for a new file with pages 0 and 1 reserved.

    next_page_id is 2 and the free list is empty. The caller must also create
    an all-zero page 1; Catalog initialization is a later, separate operation.
    """
    prefix = FILE_HEADER_STRUCT.pack(
        FILE_MAGIC, FORMAT_VERSION, PAGE_SIZE, CATALOG_ROOT_PAGE_ID,
        INITIAL_NEXT_PAGE_ID, INVALID_PAGE_ID,
    )
    return prefix + bytes(FILE_RESERVED_SIZE)


def terminal_free_page() -> bytes:
    """Return the layout of a free page whose successor is INVALID_PAGE_ID.

    This only constructs bytes; it does not release a page or update the file
    header. FileManager will own allocation state and the full free-list logic.
    """
    return FREE_NEXT_STRUCT.pack(INVALID_PAGE_ID) + bytes(FREE_RESERVED_SIZE)


# Temporary import boundary; replace with minidb.core.errors after team review.
from dataclasses import dataclass
from minidb.storage._errors import (
    DbError, ErrorStage, INVALID_ARGUMENT, PAGE_ID_INVALID, RESERVED_PAGE,
    PAGE_NOT_ALLOCATED, DB_FORMAT_MISMATCH, DB_FILE_TRUNCATED,
)
from minidb.core.disk_types import MAX_PAGE_ID, MAX_NEXT_PAGE_ID


@dataclass(frozen=True, slots=True)
class FileHeader:
    """Mutable-on-disk fields decoded as an immutable in-memory value."""
    next_page_id: int = INITIAL_NEXT_PAGE_ID
    free_head: int = INVALID_PAGE_ID


def _fail(code: str, operation: str, field: str, expected, actual,
          **context) -> None:
    raise DbError(
        stage=ErrorStage.STORAGE, code=code,
        message=f"Invalid {field} during {operation}", span=None,
        context=dict(operation=operation, field=field, expected=expected,
                     actual=actual, **context),
    )


def _boundary(value: int, operation: str) -> None:
    if type(value) is not int or not 2 <= value <= MAX_NEXT_PAGE_ID:
        _fail(INVALID_ARGUMENT, operation, 'next_page_id',
              [2, MAX_NEXT_PAGE_ID], repr(value))


def _link(value: int, boundary: int, operation: str) -> None:
    if type(value) is not int or not 0 <= value <= INVALID_PAGE_ID:
        _fail(PAGE_ID_INVALID, operation, 'page_id',
              [0, MAX_PAGE_ID], repr(value))
    if value == INVALID_PAGE_ID:
        return  # A free-chain pointer may use the sentinel.
    if value < 2:
        _fail(RESERVED_PAGE, operation, 'page_id', 'page >= 2', value,
              page_id=value)
    if value >= boundary:
        _fail(PAGE_NOT_ALLOCATED, operation, 'page_id',
              f'page < {boundary}', value, page_id=value)


def _page_bytes(data: bytes, operation: str, *, page_id=None, path=None) -> dict:
    context = {}
    if page_id is not None:
        context['page_id'] = page_id
    if path is not None:
        context['path'] = path
    if type(data) is not bytes:
        _fail(INVALID_ARGUMENT, operation, 'data', 'bytes', type(data).__name__, **context)
    if len(data) != PAGE_SIZE:
        code = DB_FILE_TRUNCATED if len(data) < PAGE_SIZE else DB_FORMAT_MISMATCH
        _fail(code, operation, 'page_size', PAGE_SIZE, len(data), **context)
    return context


def encode_file_header(header: FileHeader) -> bytes:
    """Encode a complete page 0; invalid caller input has structured errors."""
    op = 'encode_file_header'
    if not isinstance(header, FileHeader):
        _fail(INVALID_ARGUMENT, op, 'header', 'FileHeader', type(header).__name__)
    _boundary(header.next_page_id, op)
    _link(header.free_head, header.next_page_id, op)
    return FILE_HEADER_STRUCT.pack(
        FILE_MAGIC, FORMAT_VERSION, PAGE_SIZE, CATALOG_ROOT_PAGE_ID,
        header.next_page_id, header.free_head,
    ) + bytes(FILE_RESERVED_SIZE)


def decode_file_header(data: bytes, *, file_size: int, path: str | None = None) -> FileHeader:
    """Validate page 0 and the supplied actual file length, without file I/O."""
    op = 'decode_file_header'
    if type(file_size) is not int or file_size < 0:
        _fail(INVALID_ARGUMENT, op, 'file_size', 'non-negative int', repr(file_size))
    if path is not None and type(path) is not str:
        _fail(INVALID_ARGUMENT, op, 'path', 'str or None', type(path).__name__)
    context = _page_bytes(data, op, page_id=0, path=path)
    magic, version, size, root, boundary, head = FILE_HEADER_STRUCT.unpack_from(data)
    for field, actual, expected in (
        ('magic', magic.hex(), FILE_MAGIC.hex()), ('format_version', version, FORMAT_VERSION),
        ('page_size', size, PAGE_SIZE), ('catalog_root_page_id', root, CATALOG_ROOT_PAGE_ID),
    ):
        if actual != expected:
            _fail(DB_FORMAT_MISMATCH, op, field, expected, actual, **context)
    if not 2 <= boundary <= MAX_NEXT_PAGE_ID:
        _fail(DB_FORMAT_MISMATCH, op, 'next_page_id', [2, MAX_NEXT_PAGE_ID], boundary, **context)
    if head != INVALID_PAGE_ID and not 2 <= head < boundary:
        _fail(DB_FORMAT_MISMATCH, op, 'free_head', 'sentinel or allocated non-reserved page', head, **context)
    if any(data[FILE_RESERVED_OFFSET:]):
        _fail(DB_FORMAT_MISMATCH, op, 'reserved', 'all zero', 'nonzero bytes', **context)
    expected_size = boundary * PAGE_SIZE
    if file_size != expected_size:
        code = DB_FILE_TRUNCATED if file_size < expected_size else DB_FORMAT_MISMATCH
        _fail(code, op, 'file_size', expected_size, file_size, **context)
    return FileHeader(boundary, head)


def encode_free_page(next_free_page_id: int = INVALID_PAGE_ID, *, next_page_id: int) -> bytes:
    """Encode a free-page link; does not release any page or change a file."""
    op = 'encode_free_page'
    _boundary(next_page_id, op)
    _link(next_free_page_id, next_page_id, op)
    return FREE_NEXT_STRUCT.pack(next_free_page_id) + bytes(FREE_RESERVED_SIZE)


def decode_free_page(data: bytes, *, page_id: int, next_page_id: int) -> int:
    """Validate one free page. FileManager must also detect multi-page cycles."""
    op = 'decode_free_page'
    _boundary(next_page_id, op)
    if type(page_id) is not int or not 0 <= page_id <= MAX_PAGE_ID:
        _fail(PAGE_ID_INVALID, op, 'page_id', [0, MAX_PAGE_ID], repr(page_id))
    _link(page_id, next_page_id, op)
    context = _page_bytes(data, op, page_id=page_id)
    successor = FREE_NEXT_STRUCT.unpack_from(data)[0]
    if successor != INVALID_PAGE_ID and (not 2 <= successor < next_page_id or successor == page_id):
        _fail(DB_FORMAT_MISMATCH, op, 'next_free_page_id',
              'sentinel or another allocated non-reserved page', successor, **context)
    if any(data[FREE_RESERVED_OFFSET:]):
        _fail(DB_FORMAT_MISMATCH, op, 'reserved', 'all zero', 'nonzero bytes', **context)
    return successor
