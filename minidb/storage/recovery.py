"""持锁启动恢复，必须在 FileManager/Catalog 装配之前调用。

仅处理物理文件与快照日志；目录、索引和行的语义校验仍由各自模块完成。
失败保留正式日志并阻止此锁直接交给 FileManager，可重试恢复或关闭锁。
"""
import os
import tempfile
from dataclasses import dataclass
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE
from minidb.storage.file_lock import DatabaseLock
from minidb.storage import file_snapshot, page_v2, snapshot_journal as journal


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    action: str
    transaction_uuid: UUID | None
    restored_bytes: int


def _error(reason, **context):
    return errors.DbError(errors.ErrorStage.STORAGE, errors.RECOVERY_FAILED,
                          '启动恢复失败', context={'operation': 'inspect_and_recover',
                                                     'reason': reason, **context})


def _sync(handle):
    handle.flush()
    os.fsync(handle.fileno())


def _check_main_identity(handle, expected):
    """只有可解析的主库头才参与身份对比；半写头允许由已验证镜像恢复。

    用头内页数校验头结构，避免文件长度半写时跳过可识别的异库 UUID。
    """
    handle.seek(0)
    raw = handle.read(PAGE_SIZE)
    if raw[:8] == b'MINIDB01':
        raise _error('v2 journal cannot overwrite v1 file')
    if len(raw) != PAGE_SIZE:
        return
    boundary = int.from_bytes(raw[20:24], 'little')
    try:
        header = page_v2.decode_file_header(raw, file_size=boundary * PAGE_SIZE)
    except errors.DbError as exc:
        if exc.code in (errors.DB_FORMAT_MISMATCH, errors.DB_FILE_TRUNCATED):
            return
        raise
    if header.database_uuid != expected:
        raise _error('main file UUID mismatch', expected=str(expected), actual=str(header.database_uuid))


class RecoveryManager:
    @staticmethod
    def inspect_and_recover(path: str, lock_handle: DatabaseLock) -> RecoveryReport:
        if (type(path) is not str or not path or '\x00' in path or
                not isinstance(lock_handle, DatabaseLock)):
            raise _error('invalid path or lock')
        actual = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        if actual != lock_handle.path:
            raise _error('path does not belong to lock')
        if getattr(lock_handle, '_file_manager_owner', None) is not None:
            raise _error('recovery must precede FileManager construction')
        formal, temporary = journal._paths(lock_handle)
        handle = lock_handle.handle
        try:
            try:
                source = formal.open('rb')
            except FileNotFoundError:
                # tmp 无论是否完整都不能自动发布成正式日志。
                temporary.unlink(missing_ok=True)
                lock_handle._recovery_failure = None
                return RecoveryReport('NONE', None, 0)
            with source:
                report = journal.inspect_stream(source)
                info = report.snapshot
                if report.committed:
                    length = os.fstat(handle.fileno()).st_size
                    if length != report.final_length:
                        raise _error('committed main file length mismatch', expected=report.final_length, actual=length)
                    file_snapshot.validate_image(handle, length, info.database_uuid)
                    action, restored = 'COMMITTED_CLEANED', 0
                else:
                    _check_main_identity(handle, info.database_uuid)
                    # 先再次验证拷贝到临时流的内容，避免检查后源流变化影响主库。
                    with tempfile.SpooledTemporaryFile(max_size=1024*1024, mode='w+b') as staged:
                        source.seek(journal.HEADER_SIZE)
                        digest = file_snapshot.copy_payload(source, staged, info.original_length)
                        if digest != info.payload_sha256:
                            raise _error('snapshot changed after inspection')
                        file_snapshot.validate_image(staged, info.original_length, info.database_uuid)
                        handle.seek(0)
                        file_snapshot.copy_payload(staged, handle, info.original_length)
                        handle.truncate(info.original_length)
                        _sync(handle)
                        file_snapshot.validate_image(handle, info.original_length, info.database_uuid)
                    action, restored = 'ROLLED_BACK', info.original_length
            # 恢复完整同步并校验后才删除日志。恢复中再次终止可重复执行。
            formal.unlink()
            temporary.unlink(missing_ok=True)
            lock_handle._recovery_failure = None
            return RecoveryReport(action, info.transaction_uuid, restored)
        except BaseException as exc:
            failure = exc if isinstance(exc, errors.DbError) and exc.code == errors.RECOVERY_FAILED else _error(type(exc).__name__, cause=str(exc))
            lock_handle._recovery_failure = failure
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if failure is exc:
                raise
            raise failure from exc
