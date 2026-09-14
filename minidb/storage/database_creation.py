"""在路径锁下创建完整 v2 空库，然后无覆盖发布（优化方案 12 节）。

返回 UUID 时文件已同步并关闭；Session 随后按已有 v2 文件获取锁、恢复、
open_locked 和加载目录。不要再次以 is_new=True 初始化两个目录根页。
崩溃遗留的随机临时文件不是正式数据库，不自动提升或覆盖目标。
"""
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE
from minidb.storage.file_lock import DatabaseLock, _open_exclusive
from minidb.storage.file_manager import FileManager
from minidb.storage.page_v2 import FileHeaderV2, encode_file_header
from minidb.storage.file_snapshot import write_all
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine


def _error(code, path, reason):
    return errors.DbError(errors.ErrorStage.STORAGE, code, 'v2 数据库创建失败',
                          context={'operation': 'create_v2', 'path': path, 'reason': str(reason)})


def create_v2_database(path: str):
    """只创建不存在的目标；同步/初始化/发布失败时不覆盖任何已有文件。

    Windows 的 os.rename 在目标存在时失败，不使用会覆盖目标的 os.replace。
    创建流程没有业务写，不生成空文件事务快照，不装配 Session/IndexManager。
    """
    if type(path) is not str or not path or '\x00' in path:
        raise _error(errors.INVALID_ARGUMENT, repr(path), 'path 必须是非空字符串')
    if os.name != 'nt':
        raise _error(errors.IO_OPEN_FAILED, path, '当前排他锁和无覆盖发布仅支持 Windows')
    actual = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    path_lock = temp_lock = manager = None
    temporary = None
    published = False
    original = None
    guard = TransactionGuard(S.BOOTSTRAP)
    try:
        Path(actual).parent.mkdir(parents=True, exist_ok=True)
        path_lock, _ = _open_exclusive(actual + '.mdb2-lock')
        if os.path.lexists(actual):
            raise _error(errors.INVALID_ARGUMENT, actual, '目标已经存在，禁止覆盖')
        if any(os.path.lexists(actual + suffix) for suffix in ('.mdb2-journal', '.mdb2-journal.tmp')):
            raise _error(errors.RECOVERY_FAILED, actual, '存在恢复日志，必须先检查恢复，不能新建')
        fd, temporary = tempfile.mkstemp(prefix=Path(actual).name+'.creating-', suffix='.tmp',
                                         dir=Path(actual).parent)
        os.close(fd)
        temp_lock = DatabaseLock.acquire(temporary)
        identity = uuid4()
        write_all(temp_lock.handle, encode_file_header(FileHeaderV2(identity)) + bytes(2*PAGE_SIZE))
        manager = FileManager.open_locked(temporary, temp_lock, guard=guard)
        pool = BufferPool(manager, capacity=2)
        storage = StorageEngine(pool, RowCodec(), manager, guard)
        for table in (SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE):
            storage.initialize_reserved_heap(table)
            storage.validate_table_root(table)
        pool.flush_all()
        manager.reload_metadata()
        manager.sync()
        guard.transition_to(S.IDLE)
        manager.close()
        manager = None
        # 两个句柄均已关闭；发布前的任何失败都只有临时文件。
        os.rename(temporary, actual)
        published = True
        return identity
    except BaseException as exc:
        if isinstance(exc, OSError):
            code = errors.DATABASE_BUSY if getattr(exc, 'winerror', None) in (32, 33) else errors.IO_WRITE_FAILED
            original = _error(code, actual, exc)
        else:
            original = exc
        if guard.state not in (S.FAILED, S.CLOSED):
            guard.transition_to(S.FAILED)
        raise original
    finally:
        cleanup_errors = []
        # 先关闭临时主文件，再清理本次随机临时路径，最后释放正式路径锁。
        for resource in (manager, temp_lock):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
        if temporary is not None:
            for candidate in ([temporary] if not published else []) + [temporary+'.mdb2-lock']:
                try:
                    Path(candidate).unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(exc)
        if path_lock is not None:
            try:
                path_lock.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            if original is not None:
                for exc in cleanup_errors:
                    original.add_note(f'创建清理失败：{exc}')
            else:
                # 已发布的完整文件保留；不能为清理临时锁失败删除主库。
                raise _error(errors.IO_CLOSE_FAILED, actual,
                             f'published={published}; cleanup={cleanup_errors}')
