"""有界的原始文件镜像 I/O；不负责日志发布、提交标记或事务 ID。"""
import hashlib
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, INVALID_PAGE_ID
from minidb.storage import page_v2

CHUNK_SIZE = 64 * 1024


def error(code, operation, **context):
    return errors.DbError(errors.ErrorStage.STORAGE, code, '文件快照操作失败',
                          context={'operation': operation, **context})


def read_exact(stream, count):
    parts = []
    remaining = count
    while remaining:
        try:
            data = stream.read(remaining)
        except (OSError, ValueError) as exc:
            raise error(errors.IO_READ_FAILED, 'snapshot_read', cause=str(exc)) from exc
        if type(data) is not bytes or not data or len(data) > remaining:
            raise error(errors.RECOVERY_FAILED, 'snapshot_read', expected=count,
                        actual=count-remaining, reason='incomplete binary payload')
        parts.append(data)
        remaining -= len(data)
    return b''.join(parts)


def write_all(stream, data):
    offset = 0
    while offset < len(data):
        try:
            count = stream.write(data[offset:])
        except (OSError, ValueError) as exc:
            raise error(errors.IO_WRITE_FAILED, 'snapshot_write', cause=str(exc)) from exc
        if type(count) is not int or not 0 < count <= len(data)-offset:
            raise error(errors.IO_WRITE_FAILED, 'snapshot_write', reason='write made no progress')
        offset += count


def copy_payload(source, destination, length):
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        data = read_exact(source, min(remaining, CHUNK_SIZE))
        write_all(destination, data)
        digest.update(data)
        remaining -= len(data)
    return digest.digest()


def validate_image(stream, length: int, identity: UUID):
    """校验临时镜像的文件头和完整空闲链；业务页字节完整性由 SHA-256 验证。"""
    stream.seek(0)
    try:
        header = page_v2.decode_file_header(read_exact(stream, PAGE_SIZE), file_size=length)
        if header.database_uuid != identity:
            raise error(errors.RECOVERY_FAILED, 'validate_snapshot', field='database_uuid',
                        expected=str(identity), actual=str(header.database_uuid))
        current = header.free_head
        visited = set()
        while current != INVALID_PAGE_ID:
            if current in visited:
                raise error(errors.RECOVERY_FAILED, 'validate_snapshot', reason='cyclic free list')
            visited.add(current)
            stream.seek(current * PAGE_SIZE)
            current = page_v2.decode_free_page(read_exact(stream, PAGE_SIZE), page_id=current,
                                               next_page_id=header.next_page_id)
    except errors.DbError as exc:
        if exc.code in (errors.DB_FORMAT_MISMATCH, errors.DB_FILE_TRUNCATED):
            raise error(errors.RECOVERY_FAILED, 'validate_snapshot', cause=exc.code) from exc
        raise
    stream.seek(0)
