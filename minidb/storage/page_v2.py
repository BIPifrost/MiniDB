"""独立的 v2 文件页编码；不打开文件，不升级 v1，也不初始化目录。

UUID 使用 UUID.bytes 的原始 16 字节，其余整数均为小端。
这里只验证单个空闲页；整条空闲链的环检查由 FileManager 负责。
"""
from dataclasses import dataclass
from struct import Struct
from uuid import UUID, RFC_4122

from minidb.core import errors
from minidb.core.disk_types import (
    PAGE_SIZE, INVALID_PAGE_ID, V2_FORMAT_VERSION, V2_FIRST_ALLOCATABLE_PAGE_ID,
    V2_INDEX_CATALOG_ROOT_PAGE_ID, V2_MAX_FILE_SIZE, V2_MAX_PAGE_COUNT,
)

FILE_MAGIC = b'MINIDB02'
FILE_HEADER_STRUCT = Struct('<8sIIIII16sI')
FILE_RESERVED_OFFSET = 48
FREE_NEXT_STRUCT = Struct('<I')


@dataclass(frozen=True, slots=True)
class FileHeaderV2:
    database_uuid: UUID
    next_page_id: int = V2_FIRST_ALLOCATABLE_PAGE_ID
    free_head: int = INVALID_PAGE_ID


def _fail(code, operation, field, actual, **context):
    raise errors.DbError(errors.ErrorStage.STORAGE, code,
                         f'v2 文件页字段无效：{field}',
                         context={'operation': operation, 'field': field,
                                  'actual': str(actual), **context})


def _boundary(value, op, code=errors.INVALID_ARGUMENT):
    if type(value) is not int or not V2_FIRST_ALLOCATABLE_PAGE_ID <= value <= V2_MAX_PAGE_COUNT:
        _fail(code, op, 'next_page_id', value)


def _valid_link(value, boundary):
    return type(value) is int and (value == INVALID_PAGE_ID or
                                  V2_FIRST_ALLOCATABLE_PAGE_ID <= value < boundary)


def _uuid(value, op, code):
    if not isinstance(value, UUID) or value.version != 4 or value.variant != RFC_4122:
        _fail(code, op, 'database_uuid', value)


def _bytes(data, op):
    if type(data) is not bytes:
        _fail(errors.INVALID_ARGUMENT, op, 'data', type(data).__name__)
    if len(data) != PAGE_SIZE:
        code = errors.DB_FILE_TRUNCATED if len(data) < PAGE_SIZE else errors.DB_FORMAT_MISMATCH
        _fail(code, op, 'page_size', len(data))


def encode_file_header(header: FileHeaderV2) -> bytes:
    op = 'encode_file_header_v2'
    if not isinstance(header, FileHeaderV2):
        _fail(errors.INVALID_ARGUMENT, op, 'header', type(header).__name__)
    _uuid(header.database_uuid, op, errors.INVALID_ARGUMENT)
    _boundary(header.next_page_id, op)
    if not _valid_link(header.free_head, header.next_page_id):
        _fail(errors.INVALID_ARGUMENT, op, 'free_head', header.free_head)
    return FILE_HEADER_STRUCT.pack(FILE_MAGIC, V2_FORMAT_VERSION, PAGE_SIZE, 1,
                                   header.next_page_id, header.free_head,
                                   header.database_uuid.bytes,
                                   V2_INDEX_CATALOG_ROOT_PAGE_ID) + bytes(PAGE_SIZE - FILE_RESERVED_OFFSET)


def decode_file_header(data: bytes, *, file_size: int) -> FileHeaderV2:
    op = 'decode_file_header_v2'
    _bytes(data, op)
    if type(file_size) is not int or file_size < 0:
        _fail(errors.INVALID_ARGUMENT, op, 'file_size', file_size)
    magic, version, size, root, boundary, head, raw_uuid, index_root = FILE_HEADER_STRUCT.unpack_from(data)
    for field, actual, expected in (
        ('magic', magic, FILE_MAGIC), ('format_version', version, V2_FORMAT_VERSION),
        ('page_size', size, PAGE_SIZE), ('catalog_root', root, 1),
        ('index_catalog_root', index_root, V2_INDEX_CATALOG_ROOT_PAGE_ID),
    ):
        if actual != expected:
            _fail(errors.DB_FORMAT_MISMATCH, op, field, actual, expected=str(expected))
    _boundary(boundary, op, errors.DB_FORMAT_MISMATCH)
    identity = UUID(bytes=raw_uuid)
    _uuid(identity, op, errors.DB_FORMAT_MISMATCH)
    if not _valid_link(head, boundary):
        _fail(errors.DB_FORMAT_MISMATCH, op, 'free_head', head)
    if any(data[FILE_RESERVED_OFFSET:]):
        _fail(errors.DB_FORMAT_MISMATCH, op, 'reserved', 'nonzero')
    expected_size = boundary * PAGE_SIZE
    if file_size != expected_size:
        code = errors.DB_FILE_TRUNCATED if file_size < expected_size else errors.DB_FORMAT_MISMATCH
        _fail(code, op, 'file_size', file_size, expected=expected_size, limit=V2_MAX_FILE_SIZE)
    return FileHeaderV2(identity, boundary, head)


def encode_free_page(next_free_page_id: int = INVALID_PAGE_ID, *, next_page_id: int) -> bytes:
    op = 'encode_free_page_v2'
    _boundary(next_page_id, op)
    if not _valid_link(next_free_page_id, next_page_id):
        _fail(errors.INVALID_ARGUMENT, op, 'next_free_page_id', next_free_page_id)
    return FREE_NEXT_STRUCT.pack(next_free_page_id) + bytes(PAGE_SIZE - 4)


def decode_free_page(data: bytes, *, page_id: int, next_page_id: int) -> int:
    op = 'decode_free_page_v2'
    _boundary(next_page_id, op)
    if not _valid_link(page_id, next_page_id) or page_id == INVALID_PAGE_ID:
        _fail(errors.INVALID_ARGUMENT, op, 'page_id', page_id)
    _bytes(data, op)
    successor = FREE_NEXT_STRUCT.unpack_from(data)[0]
    if not _valid_link(successor, next_page_id) or successor == page_id:
        _fail(errors.DB_FORMAT_MISMATCH, op, 'next_free_page_id', successor, page_id=page_id)
    if any(data[4:]):
        _fail(errors.DB_FORMAT_MISMATCH, op, 'reserved', 'nonzero', page_id=page_id)
    return successor
