"""R07/R13/R14连续故障链：真实文件和恢复，目录行及参与方为测试替身。

fsync注入验证错误协议，不模拟掉电；R14使用真实目录校验但非持久目录行。
"""
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from minidb.catalog.catalog import Catalog
from minidb.catalog.catalog_manager import CatalogManager
from minidb.core import errors
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage import page_v2, snapshot_journal as journal
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.file_manager import FileManager
from minidb.storage.transaction import TransactionManager
from tests.fakes.transaction_participants import StorageParticipant, CatalogParticipant, IndexParticipant
from tests.fakes.v2_catalog_contracts import CatalogStorage

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = (page_v2.encode_file_header(page_v2.FileHeaderV2(
    UUID('00112233-4455-4677-8899-aabbccddeeff'), 4)) + bytes(8192) + b'A'*4096)
NEW = ORIGINAL[:3*4096] + b'B'*4096


def digest(data):
    return hashlib.sha256(data).hexdigest()


class TransactionFailureChainTests(unittest.TestCase):
    def connect(self, path, policy, capacity, real_catalog=False):
        lock = DatabaseLock.acquire(str(path))
        guard = TransactionGuard(S.IDLE)
        fm = FileManager.open_locked(str(path), lock, guard=guard)
        pool = BufferPool(fm, capacity=capacity, policy=policy)
        events = []
        storage = StorageParticipant(fm, pool, guard, events)
        rows = None
        if real_catalog:
            rows = CatalogStorage(state=S.IDLE)
            storage.catalog_services = replace(rows.catalog_services, guard=guard)
            storage.validate_table_root = rows.validate_table_root
            storage.scan_rows = rows.scan_rows
            catalog = CatalogManager(storage, Catalog())
        else:
            catalog = CatalogParticipant(storage, events)
        indexes = IndexParticipant(pool, storage, catalog, guard, events)
        manager = TransactionManager(fm, pool, storage, catalog, indexes, lock, guard,
                                     invalidate_prepared=lambda: events.append('invalidate_prepared'))
        return manager, rows, events

    def reopen(self, path, policy, expected, action):
        result = subprocess.run([sys.executable, '-B', '-m', 'tests.fakes.recovery_phases',
                                 str(path), 'recover', policy], cwd=ROOT,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['action'], action)
        self.assertEqual(report['restored_bytes'], len(ORIGINAL) if action == 'ROLLED_BACK' else 0)
        self.assertEqual(report['sha256'], digest(expected))
        self.assertEqual(report['length'], len(expected))
        self.assertEqual(report['free_pages'], [])
        self.assertEqual(report['next_page_id'], 4)
        self.assertEqual(path.read_bytes(), expected)
        self.assertFalse(Path(str(path)+'.mdb2-journal').exists())
        self.assertFalse(Path(str(path)+'.mdb2-journal.tmp').exists())
        return {**report, 'exit_code': result.returncode}

    def assert_blocked(self, manager):
        for operation in (manager.begin_statement, manager.commit, manager.rollback):
            with self.assertRaises(errors.DbError) as caught:
                operation()
            self.assertEqual(caught.exception.code, errors.INVALID_TRANSACTION_STATE)

    def run_chain(self, case):
        evidence = []
        for policy in ('lru', 'fifo'):
            for capacity in (1, 2):
                for repeat in range(1, 4):
                    with self.subTest(case=case, policy=policy, capacity=capacity, repeat=repeat), tempfile.TemporaryDirectory() as temp:
                        path = Path(temp)/'failure.db'
                        path.write_bytes(ORIGINAL)
                        manager, rows, events = self.connect(path, policy, capacity, case == 'R14')
                        try:
                            txn = manager.begin_statement()
                            formal = Path(str(path)+'.mdb2-journal')
                            initial_log = formal.read_bytes()
                            manager.pool.write_page(3, b'B'*4096)
                            details = {}
                            if case == 'R07':
                                real_unlink = Path.unlink
                                def fail_formal(target, *args, **kwargs):
                                    if target == formal:
                                        raise OSError('R07 journal cleanup failed')
                                    return real_unlink(target, *args, **kwargs)
                                with patch.object(Path, 'unlink', autospec=True, side_effect=fail_formal), patch.object(manager.fm, 'restore_snapshot', wraps=manager.fm.restore_snapshot) as restore:
                                    outcome = manager.commit()
                                self.assertTrue(outcome.committed)
                                self.assertEqual(outcome.transaction_uuid, txn)
                                self.assertIn('R07', outcome.cleanup_warning)
                                restore.assert_not_called()
                                self.assertEqual(manager.state, S.CLOSED)
                                expected, action = NEW, 'COMMITTED_CLEANED'
                                details['cleanup_warning'] = outcome.cleanup_warning
                            elif case == 'R13':
                                real_sync = journal._sync
                                fsync_calls = []
                                def fail_tail_sync(stream):
                                    # 注入范围只包含提交尾同步，主库sync已经真实完成。
                                    self.assertEqual(stream.tell(), 128+len(ORIGINAL)+64)
                                    def fail_fsync(fd):
                                        fsync_calls.append(fd)
                                        raise OSError('R13 commit tail fsync failed')
                                    with patch.object(journal.os, 'fsync', side_effect=fail_fsync):
                                        real_sync(stream)
                                with patch.object(journal, '_sync', side_effect=fail_tail_sync) as sync, patch.object(manager.fm, 'restore_snapshot', wraps=manager.fm.restore_snapshot) as restore:
                                    with self.assertRaises(errors.DbError) as caught:
                                        manager.commit()
                                self.assertEqual(caught.exception.code, errors.COMMIT_OUTCOME_UNKNOWN)
                                self.assertIn('R13', str(caught.exception.__cause__))
                                self.assertEqual(sync.call_count, 1)
                                self.assertEqual(len(fsync_calls), 1)
                                restore.assert_not_called()
                                self.assertEqual(manager.state, S.CLOSED)
                                # 完整尾仍可读取。本例重开保留新镜像，不推广为掉电结果。
                                expected, action = NEW, 'COMMITTED_CLEANED'
                                details.update(error=caught.exception.code, fsync_calls=1)
                            else:
                                # 先令真实文件发生变化，再验证恢复成功、目录重载失败。
                                manager.pool.flush_all()
                                manager.fm.sync()
                                from tests.fakes.file_bytes import read_file_bytes
                                self.assertEqual(read_file_bytes(manager.fm), NEW)
                                rows.rows[0] = [(1,)]  # 真正的目录转换器拒绝损坏行。
                                first_errors = []
                                real_reload = manager.catalog.reload_from_storage
                                def capture_reload():
                                    try:
                                        real_reload()
                                    except errors.DbError as error:
                                        first_errors.append(error)
                                        raise
                                with patch.object(manager.catalog, 'reload_from_storage', side_effect=capture_reload), patch.object(manager, 'invalidate_prepared', side_effect=RuntimeError('R14 token cleanup failed')) as invalidate, patch.object(manager.storage, 'abort_resources', side_effect=RuntimeError('R14 resource cleanup failed')) as abort, patch.object(manager.fm, 'close', wraps=manager.fm.close) as close:
                                    with self.assertRaises(errors.DbError) as caught:
                                        manager.rollback()
                                self.assertEqual(len(first_errors), 1)
                                self.assertIs(caught.exception, first_errors[0])
                                self.assertEqual(caught.exception.code, errors.CATALOG_CORRUPTED)
                                cleanup = caught.exception.context['cleanup_errors']
                                self.assertEqual(len(cleanup), 2)
                                self.assertIn('token cleanup', cleanup[0]['cause'])
                                self.assertIn('resource cleanup', cleanup[1]['cause'])
                                invalidate.assert_called_once()
                                abort.assert_called_once()
                                close.assert_called_once()
                                self.assertTrue(all(scan.closed for scan in rows.scans))
                                self.assertEqual(manager.catalog.generation, 0)
                                self.assertNotIn('index_reload', events)
                                # 清理不全保留FAILED；文件已关闭，业务必须被阻止。
                                self.assertEqual(manager.state, S.FAILED)
                                self.assertEqual(formal.read_bytes(), initial_log)
                                expected, action = ORIGINAL, 'ROLLED_BACK'
                                details.update(error=caught.exception.code, cleanup_errors=cleanup)
                            self.assertTrue(manager.lock.handle.closed)
                            self.assertTrue(manager.lock.path_handle.closed)
                            self.assert_blocked(manager)
                            self.assertEqual(path.read_bytes(), expected)
                            before_reopen = formal.read_bytes()
                            with formal.open('rb') as stream:
                                inspection = journal.inspect_stream(stream)
                            self.assertEqual(inspection.committed, case != 'R14')
                            self.assertEqual(inspection.snapshot.transaction_uuid, txn)
                            report = self.reopen(path, policy, expected, action)
                            again = self.reopen(path, policy, expected, 'NONE')
                            manager.close()
                            self.assertEqual(manager.state, S.CLOSED)
                            evidence.append({'case_id': case, 'policy': policy, 'capacity': capacity,
                                             'repeat': repeat, 'seed': 'fixed-bytes',
                                             'old_sha256': digest(ORIGINAL), 'new_sha256': digest(NEW),
                                             'journal_sha256': digest(before_reopen), 'details': details,
                                             'recovered': report, 'second_reopen': again, 'passed': True})
                        finally:
                            manager.fm.close()
        self.assertEqual(len(evidence), 12)
        output = os.environ.get('MINIDB_RECOVERY_EVIDENCE_DIR')
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            (directory/(case+'_chain.json')).write_text(json.dumps({
                'platform': platform.platform(), 'python': sys.version,
                'source_revision': os.environ.get('MINIDB_TEST_REVISION', 'unspecified'),
                'boundary': '故障注入与新进程物理恢复；非掉电、持久目录或SQL验收',
                'cases': evidence}, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_r07_committed_cleanup_failure_then_reopen(self):
        self.run_chain('R07')

    def test_r13_tail_fsync_failure_is_unknown_without_retry(self):
        self.run_chain('R13')

    def test_r14_real_catalog_error_preserves_first_error_and_cleanup_details(self):
        self.run_chain('R14')
