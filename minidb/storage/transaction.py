"""物理事务协调器；生产 Session/Catalog/IndexManager 尚需按参与方合同接入。

参与方必须提供真实重载/失效操作，不提供默认空实现。测试替身放 tests/fakes。
提交仅在提交尾同步成功后返回；恢复日志的启动处理由 RecoveryManager 负责。
"""
from dataclasses import dataclass
from typing import Callable
from uuid import UUID, uuid4

from minidb.core import errors
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage.file_manager import FileManager
from minidb.storage.buffer_pool import BufferPool
from minidb.storage import snapshot_journal as journal


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    committed: bool
    transaction_uuid: UUID
    cleanup_warning: str | None = None


class TransactionManager:
    def __init__(self, file_manager, buffer_pool, storage, catalog,
                 index_manager, lock_handle, guard, *,
                 invalidate_prepared: Callable[[], None]):
        """Session 提供 invalidate_prepared，清除该会话的已准备写入和 token。

        storage: file_manager/buffer_pool/guard/active_scan_count 属性，
                 close_scans()、abort_resources()（只释放资源，不写盘）。
        catalog: 沿用远程 _storage/CatalogServices.guard 身份链，
                 reload_from_storage() 重读目录并提升目录代际。
        index_manager: buffer_pool/storage/catalog/guard 属性、reload()。
        """
        if (not isinstance(file_manager, FileManager) or not isinstance(buffer_pool, BufferPool)
                or not isinstance(guard, TransactionGuard) or not callable(invalidate_prepared)):
            raise self._error(errors.INVALID_ARGUMENT, 'construct_transaction', reason='invalid participant')
        identities = (
            buffer_pool.file_manager is file_manager,
            file_manager._lock is lock_handle,
            file_manager._guard is guard,
            getattr(storage, 'file_manager', None) is file_manager,
            getattr(storage, 'buffer_pool', None) is buffer_pool,
            getattr(storage, 'guard', None) is guard,
            getattr(catalog, '_storage', None) is storage,
            getattr(getattr(catalog, '_services', None), 'guard', None) is guard,
            getattr(index_manager, 'buffer_pool', None) is buffer_pool,
            getattr(index_manager, 'storage', None) is storage,
            getattr(index_manager, 'catalog', None) is catalog,
            getattr(index_manager, 'guard', None) is guard,
        )
        methods = ((storage, 'close_scans'), (storage, 'abort_resources'),
                   (catalog, 'reload_from_storage'), (index_manager, 'reload'))
        if not all(identities) or any(not callable(getattr(obj, name, None)) for obj, name in methods):
            raise self._error(errors.INVALID_ARGUMENT, 'construct_transaction', reason='participant identity or interface mismatch')
        if getattr(file_manager, '_transaction_manager', None) is not None:
            raise self._error(errors.INVALID_ARGUMENT, 'construct_transaction', reason='manager already bound')
        if file_manager.database_uuid is None:
            raise self._error(errors.FORMAT_VERSION_UNSUPPORTED, 'construct_transaction', expected=2, actual=1)
        self.fm, self.pool, self.storage = file_manager, buffer_pool, storage
        self.catalog, self.indexes, self.lock, self.guard = catalog, index_manager, lock_handle, guard
        self.invalidate_prepared = invalidate_prepared
        self._info = None
        self._sequence = 0
        file_manager._transaction_manager = self

    @staticmethod
    def _error(code, operation, **context):
        return errors.DbError(errors.ErrorStage.STORAGE, code, '事务操作失败',
                              context={'operation': operation, **context})

    @property
    def state(self):
        return self.guard.state

    def _no_scans(self, operation):
        count = getattr(self.storage, 'active_scan_count', None)
        if type(count) is not int or count < 0:
            raise self._error(errors.INVALID_ARGUMENT, operation, field='active_scan_count')
        if count:
            raise self._error(errors.ACTIVE_SCAN, operation, active_scan_count=count)

    def _close_resources(self):
        self.storage.close_scans()
        self.storage.abort_resources()
        self.fm.close()
        self.guard.close()

    def _fatal(self, original):
        cleanup = []
        if self.state not in (S.FAILED, S.CLOSED):
            self.guard.fail(operation='transaction_failure')
        for action in (self.invalidate_prepared, self.storage.close_scans,
                       self.storage.abort_resources, self.fm.close):
            try:
                action()
            except BaseException as exc:
                cleanup.append({'operation': getattr(action, '__name__', 'cleanup'), 'cause': str(exc)})
        if not cleanup:
            self.guard.close()
        if cleanup:
            if isinstance(original, errors.DbError):
                original._update_context(cleanup_errors=[*original.context.get('cleanup_errors', ()), *cleanup])
            else:
                original.add_note(str(cleanup))

    def begin_statement(self) -> UUID:
        self.guard.require(S.IDLE, operation='begin_statement')
        self._no_scans('begin_statement')
        try:
            if any(pool.has_dirty_pages for pool in self.fm._buffer_pools):
                raise self._error(errors.INVALID_TRANSACTION_STATE, 'begin_statement', reason='dirty cache before begin')
            self.guard.transition_to(S.PREPARING)
            self._sequence += 1
            txn = uuid4()
            self._info = journal.prepare(self.fm, txn, self._sequence)
            self.guard.transition_to(S.ACTIVE)
            return txn
        except BaseException as exc:
            self._fatal(exc)
            raise

    def commit(self) -> CommitOutcome:
        self.guard.require(S.ACTIVE, operation='commit')
        if self._info is None:
            raise self._error(errors.INVALID_TRANSACTION_STATE, 'commit', reason='missing prepared journal')
        try:
            self.storage.close_scans()
            self._no_scans('commit')
            self.guard.transition_to(S.COMMITTING)
            self.pool.flush_all()
            report = journal.mark_committed(self.fm, self._info.transaction_uuid)
        except BaseException as exc:
            if isinstance(exc, Exception) and self.state is S.COMMITTING:
                unknown = self._error(errors.COMMIT_OUTCOME_UNKNOWN, 'commit', cause=str(exc))
                self._fatal(unknown)
                raise unknown from exc
            self._fatal(exc)
            raise
        # 从这里开始已经持久提交：后续清理失败只能返回成功附警告。
        try:
            self.invalidate_prepared()
            path, _ = journal._paths(self.lock)
            path.unlink()
            self._info = None
            self.guard.transition_to(S.IDLE)
            return CommitOutcome(True, report.snapshot.transaction_uuid)
        except Exception as exc:
            self._fatal(exc)
            return CommitOutcome(True, report.snapshot.transaction_uuid, str(exc))

    def rollback(self) -> None:
        self.guard.require(S.ACTIVE, operation='rollback')
        if self._info is None:
            raise self._error(errors.INVALID_TRANSACTION_STATE, 'rollback', reason='missing prepared journal')
        try:
            self.storage.close_scans()
            self._no_scans('rollback')
            self.guard.transition_to(S.ROLLING_BACK)
            report = journal.inspect_journal(self.lock)
            if report is None or report.committed or report.snapshot != self._info:
                raise self._error(errors.RECOVERY_FAILED, 'rollback', reason='journal identity or state mismatch')
            path, _ = journal._paths(self.lock)
            with path.open('rb') as stream:
                stream.seek(journal.HEADER_SIZE)
                self.fm.restore_snapshot(stream, self._info.original_length,
                                         expected_sha256=self._info.payload_sha256)
            self.catalog.reload_from_storage()
            self.indexes.reload()
            self.invalidate_prepared()
            path.unlink()
            self._info = None
            self.guard.transition_to(S.IDLE)
        except BaseException as exc:
            self._fatal(exc)
            raise

    def close(self) -> None:
        if self.state is S.CLOSED:
            return
        self._no_scans('close_transaction')
        if self.state is S.ACTIVE:
            self.rollback()
        self.guard.require(S.IDLE, S.FAILED, operation='close_transaction')
        try:
            self._close_resources()
        except BaseException as exc:
            self._fatal(exc)
            raise
