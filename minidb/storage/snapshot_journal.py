"""v2 整库快照日志：格式、发布和提交尾；不驱动 Session/事务状态转换。

必须在主库 DatabaseLock 下调用。inspect 只判定日志，不宣称主库已恢复。
临时日志从不提升为 ACTIVE；失败保留副文件，由恢复层决定清理。
"""
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from struct import Struct
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, V2_MAX_FILE_SIZE, SnapshotInfo
from minidb.core.transaction import TransactionState as S
from minidb.storage.file_lock import DatabaseLock
from minidb.storage import file_snapshot, page_v2

HEADER_SIZE = 128
TAIL_SIZE = 64
HEADER_PREFIX = Struct('<8sII16s16sQ32sQ')
TAIL_PREFIX = Struct('<8s16sQ')


@dataclass(frozen=True, slots=True)
class JournalInspection:
    snapshot: SnapshotInfo
    sequence: int
    committed: bool
    final_length: int | None
    tail_length: int


def _error(code, operation, **context):
    return errors.DbError(errors.ErrorStage.STORAGE, code, '快照日志操作失败',
                          context={'operation': operation, **context})


def _length(length):
    return type(length) is int and 3*PAGE_SIZE <= length <= V2_MAX_FILE_SIZE and length % PAGE_SIZE == 0


def encode_header(info: SnapshotInfo, sequence: int) -> bytes:
    if (not isinstance(info, SnapshotInfo) or type(sequence) is not int or
            not 0 <= sequence < 2**64):
        raise _error(errors.INVALID_ARGUMENT, 'encode_journal_header')
    prefix = HEADER_PREFIX.pack(b'MDB2SNAP', 1, HEADER_SIZE, info.database_uuid.bytes,
                                info.transaction_uuid.bytes, info.original_length,
                                info.payload_sha256, sequence)
    return prefix + hashlib.sha256(prefix).digest()


def decode_header(data: bytes) -> tuple[SnapshotInfo, int]:
    if type(data) is not bytes or len(data) != HEADER_SIZE:
        raise _error(errors.RECOVERY_FAILED, 'decode_journal_header', reason='incomplete header')
    if hashlib.sha256(data[:96]).digest() != data[96:]:
        raise _error(errors.RECOVERY_FAILED, 'decode_journal_header', reason='header hash mismatch')
    magic, version, size, db, txn, length, digest, sequence = HEADER_PREFIX.unpack(data[:96])
    if magic != b'MDB2SNAP' or version != 1 or size != HEADER_SIZE or not _length(length):
        raise _error(errors.RECOVERY_FAILED, 'decode_journal_header', reason='invalid header fields')
    return SnapshotInfo(length, UUID(bytes=db), UUID(bytes=txn), digest), sequence


def encode_commit_tail(header: bytes, final_length: int) -> bytes:
    info, _ = decode_header(header)
    if not _length(final_length):
        raise _error(errors.INVALID_ARGUMENT, 'encode_commit_tail', field='final_length')
    prefix = TAIL_PREFIX.pack(b'MDB2DONE', info.transaction_uuid.bytes, final_length)
    return prefix + hashlib.sha256(header + prefix).digest()


def inspect_stream(stream) -> JournalInspection:
    """检查从偏移零开始的完整日志；采用有界读取，不一次加载整个数据库。"""
    try:
        stream.seek(0, os.SEEK_END)
        total = stream.tell()
        stream.seek(0)
        header = file_snapshot.read_exact(stream, HEADER_SIZE)
        info, sequence = decode_header(header)
        tail_length = total - HEADER_SIZE - info.original_length
        if not 0 <= tail_length <= TAIL_SIZE:
            raise _error(errors.RECOVERY_FAILED, 'inspect_journal', reason='invalid payload or tail length')
        first_page = file_snapshot.read_exact(stream, PAGE_SIZE)
        try:
            file_header = page_v2.decode_file_header(first_page, file_size=info.original_length)
        except errors.DbError as exc:
            raise _error(errors.RECOVERY_FAILED, 'inspect_journal', cause=exc.code) from exc
        if file_header.database_uuid != info.database_uuid:
            raise _error(errors.RECOVERY_FAILED, 'inspect_journal', reason='database UUID mismatch')
        digest = hashlib.sha256(first_page)
        remaining = info.original_length - PAGE_SIZE
        while remaining:
            block = file_snapshot.read_exact(stream, min(remaining, file_snapshot.CHUNK_SIZE))
            digest.update(block)
            remaining -= len(block)
        if digest.digest() != info.payload_sha256:
            raise _error(errors.RECOVERY_FAILED, 'inspect_journal', reason='payload hash mismatch')
        tail = file_snapshot.read_exact(stream, tail_length)
        committed, final_length = False, None
        if (tail_length == TAIL_SIZE and tail[:8] == b'MDB2DONE' and
                hashlib.sha256(header + tail[:32]).digest() == tail[32:]):
            _, txn, final_length = TAIL_PREFIX.unpack(tail[:32])
            if txn != info.transaction_uuid.bytes or not _length(final_length):
                raise _error(errors.RECOVERY_FAILED, 'inspect_journal', reason='commit identity or length mismatch')
            committed = True
        return JournalInspection(info, sequence, committed, final_length, tail_length)
    except (OSError, ValueError) as exc:
        raise _error(errors.RECOVERY_FAILED, 'inspect_journal', cause=str(exc)) from exc


def _paths(lock):
    if not isinstance(lock, DatabaseLock):
        raise _error(errors.INVALID_ARGUMENT, 'journal_lock')
    if lock.handle.closed or lock.path_handle.closed:
        raise _error(errors.CLOSED, 'journal_lock')
    return Path(lock.path + '.mdb2-journal'), Path(lock.path + '.mdb2-journal.tmp')


def inspect_journal(lock: DatabaseLock) -> JournalInspection | None:
    """只打开正式日志；即使存在 tmp 也不把它视为活动事务。"""
    path, _ = _paths(lock)
    try:
        with path.open('rb') as stream:
            return inspect_stream(stream)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error(errors.RECOVERY_FAILED, 'inspect_journal', path=str(path), cause=str(exc)) from exc


def _sync(stream):
    stream.flush()
    os.fsync(stream.fileno())


def prepare(file_manager, transaction_uuid: UUID, sequence: int = 0) -> SnapshotInfo:
    """PREPARING：排他创建 tmp，持久写入后发布，再完整验证正式日志。"""
    op = 'prepare_journal'
    file_manager._require_snapshot_state(op, S.PREPARING)
    if not isinstance(transaction_uuid, UUID) or type(sequence) is not int or not 0 <= sequence < 2**64:
        raise _error(errors.INVALID_ARGUMENT, op)
    path, temporary = _paths(file_manager._lock)
    if path.exists() or temporary.exists():
        raise _error(errors.RECOVERY_FAILED, op, reason='previous journal requires recovery or cleanup')
    try:
        with temporary.open('x+b') as stream:
            file_snapshot.write_all(stream, bytes(HEADER_SIZE))
            image = file_manager.export_consistent_snapshot(stream)
            info = SnapshotInfo(image.original_length, image.database_uuid, transaction_uuid, image.payload_sha256)
            header = encode_header(info, sequence)
            stream.seek(0)
            file_snapshot.write_all(stream, header)
            _sync(stream)
        # 当前 DatabaseLock 仅支持 Windows，rename 在目标存在时拒绝覆盖。
        os.rename(temporary, path)
        inspection = inspect_journal(file_manager._lock)
        if inspection is None or inspection.snapshot != info or inspection.committed or inspection.tail_length:
            raise _error(errors.RECOVERY_FAILED, op, reason='published journal verification failed')
        return info
    except OSError as exc:
        raise _error(errors.IO_WRITE_FAILED, op, path=str(path), cause=str(exc)) from exc


def mark_committed(file_manager, transaction_uuid: UUID) -> JournalInspection:
    """COMMITTING：上层先完成缓存写回，本层同步主库后写入并同步提交尾。

    不删除日志；不转状态；提交写入/同步失败返回结果未知，禁止当作已回滚。
    """
    op = 'commit_journal'
    file_manager._require_snapshot_state(op, S.COMMITTING)
    path, _ = _paths(file_manager._lock)
    inspection = inspect_journal(file_manager._lock)
    if (inspection is None or inspection.committed or inspection.tail_length or
            inspection.snapshot.transaction_uuid != transaction_uuid or
            inspection.snapshot.database_uuid != file_manager.database_uuid):
        raise _error(errors.RECOVERY_FAILED, op, reason='journal cannot be committed')
    if any(pool.has_dirty_pages for pool in file_manager._buffer_pools):
        raise _error(errors.INVALID_TRANSACTION_STATE, op, reason='dirty cache before commit marker')
    try:
        file_manager.reload_metadata()
        final_length = file_manager._header.next_page_id * PAGE_SIZE
        file_manager.sync()
        with path.open('r+b') as stream:
            header = file_snapshot.read_exact(stream, HEADER_SIZE)
            stream.seek(HEADER_SIZE + inspection.snapshot.original_length)
            file_snapshot.write_all(stream, encode_commit_tail(header, final_length))
            _sync(stream)
    except (OSError, errors.DbError) as exc:
        raise _error(errors.COMMIT_OUTCOME_UNKNOWN, op,
                     txn_id=str(transaction_uuid), cause=str(exc)) from exc
    return JournalInspection(inspection.snapshot, inspection.sequence, True, final_length, TAIL_SIZE)
