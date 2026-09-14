"""真实文件/缓存/存储/索引/事务联调；固定目录和token登记为测试替身。"""
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from minidb.core import errors
from minidb.core.schema import ColumnDef, TypeSpec, DataType, Schema, TableDef, TableRef, IndexDef, IndexOrigin
from minidb.core.records import _issue_validated_write_token, RowUpdate, UpdateBatch
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage.file_manager import FileManager
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from minidb.storage.index_manager import IndexManager
from minidb.storage.transaction import TransactionManager
from tests.fakes.index_transaction_catalog import FixedIndexCatalog


class IndexTransactionTests(unittest.TestCase):
    def connect(self, path, policy, capacity, definitions=None):
        guard = TransactionGuard(S.ACTIVE if definitions is None else S.IDLE)
        lock = DatabaseLock.acquire(str(path))
        fm = FileManager.open_locked(str(path), lock, guard=guard)
        self.addCleanup(fm.close)
        pool = BufferPool(fm, capacity=capacity, policy=policy)
        storage = StorageEngine(pool, RowCodec(), fm, guard)
        if definitions is None:
            table = TableDef(TableRef(1, 'items', storage.create_heap(1)), Schema((
                ColumnDef('id', TypeSpec(DataType.INT)),
                ColumnDef('name', TypeSpec(DataType.VARCHAR, length=1024)),
            )))
            anchor = pool.new_page()
            index = IndexDef(1, 'name_idx', 1, 1, anchor, False, IndexOrigin.USER)
        else:
            table, index = definitions
        catalog = FixedIndexCatalog(storage, table, index)
        indexes = IndexManager(pool, storage, catalog, guard)
        tokens = []
        session_id = uuid4()
        storage.bind_write_authorizer(session_id=session_id, catalog_generation=lambda: catalog.generation,
                                     token_is_authorized=lambda t: any(t is other for other in tokens))
        def token():
            result = _issue_validated_write_token(session_id, catalog.generation, uuid4())
            tokens.append(result)
            return result
        if definitions is None:
            indexes.create(index, ())
            pool.flush_all()
            fm.sync()
            guard.transition_to(S.COMMITTING)
            guard.transition_to(S.IDLE)
        manager = TransactionManager(fm, pool, storage, catalog, indexes, lock, guard,
                                     invalidate_prepared=tokens.clear)
        return fm, pool, storage, catalog, indexes, manager, token

    def rows(self, storage, table):
        scan = storage.scan_rows(table)
        try:
            return list(scan)
        finally:
            scan.close()

    def check(self, storage, catalog, indexes, expected):
        rows = self.rows(storage, catalog.table)
        self.assertEqual(sorted(row.values for row in rows), sorted(expected))
        for row in rows:
            cursor = indexes.probe(catalog.index, row.values[1])
            try:
                self.assertIn(row.row_id, list(cursor))
            finally:
                cursor.close()
        indexes.check_indexes(catalog.table)
        self.assertEqual(storage.active_scan_count, 0)
        self.assertEqual(indexes.active_cursor_count, 0)

    def run_scenario(self, mode):
        for policy in ('lru', 'fifo'):
            for capacity in (1, 2):
                with self.subTest(policy=policy, capacity=capacity), tempfile.TemporaryDirectory() as temp:
                    path = Path(temp)/'index.db'
                    FileManager.create_v2(str(path))
                    fm, pool, storage, catalog, indexes, manager, token = self.connect(path, policy, capacity)
                    table, index = catalog.table, catalog.index
                    manager.begin_statement()
                    expected = [(n, f'{n:04d}'+'x'*180) for n in range(40)]
                    for values in expected:
                        movement = storage.insert_row(table, values, token())
                        indexes.apply_movements((index,), (movement,))
                    self.assertTrue(manager.commit().committed)
                    self.check(storage, catalog, indexes, expected)
                    self.assertGreater(indexes.validate(index).page_count, 2)
                    # 主文件持久字节基线，不经第二文件句柄绕过Windows排他锁。
                    fm._handle.seek(0)
                    original = fm._handle.read()
                    stale = pool.get_snapshot(index.root_page_id)
                    if mode == 'cursor':
                        cursor = indexes.probe(index, expected[0][1])
                        with self.assertRaises(errors.DbError) as caught:
                            manager.begin_statement()
                        self.assertEqual(caught.exception.code, errors.ACTIVE_SCAN)
                        self.assertFalse(Path(str(path)+'.mdb2-journal').exists())
                        cursor.close()
                        manager.begin_statement()
                        manager.rollback()
                    else:
                        existing = self.rows(storage, table)
                        target = min(existing, key=lambda row: row.values[0])
                        manager.begin_statement()
                        changed = (target.values[0], 'z'*500)
                        movements = storage.update_rows(table, UpdateBatch((RowUpdate(target.row_id, target.values, changed),)), token())
                        indexes.apply_movements((index,), movements)
                        if mode == 'commit':
                            victim = max(existing, key=lambda row: row.values[0])
                            removed = storage.delete_rows(table, (victim,), token())
                            indexes.apply_movements((index,), removed)
                            self.assertTrue(manager.commit().committed)
                            expected = [changed if row[0] == target.values[0] else row for row in expected if row[0] != victim.values[0]]
                        else:
                            # 已完成一次索引修改，再让下一次索引写失败；真实事务回滚全部修改。
                            extra = storage.insert_row(table, (99, 'y'*300), token())
                            def fail_write(*args, **kwargs):
                                raise OSError('injected index write failure')
                            with patch.object(indexes, '_write_page', side_effect=fail_write):
                                with self.assertRaisesRegex(OSError, 'index write failure'):
                                    indexes.apply_movements((index,), (extra,))
                            pool.flush_all()
                            manager.rollback()
                    self.check(storage, catalog, indexes, expected)
                    if mode != 'commit':
                        fm._handle.seek(0)
                        self.assertEqual(fm._handle.read(), original)
                        with self.assertRaises(errors.DbError) as caught:
                            pool.write_if_current(stale, stale.data)
                        self.assertEqual(caught.exception.code, errors.STALE_PAGE)
                    self.assertFalse(Path(str(path)+'.mdb2-journal').exists())
                    manager.close()
                    reopened = self.connect(path, policy, capacity, (table, index))
                    try:
                        self.check(reopened[2], reopened[3], reopened[4], expected)
                    finally:
                        reopened[5].close()

    def test_commit_update_delete_and_reopen(self):
        self.run_scenario('commit')

    def test_index_write_failure_rolls_back_table_and_index(self):
        self.run_scenario('rollback')

    def test_active_index_cursor_blocks_begin_then_close_allows_it(self):
        self.run_scenario('cursor')
