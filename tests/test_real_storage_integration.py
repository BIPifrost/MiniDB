"""真实目录、数据页、编码、缓存与文件的集成测试，不使用内存替身。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from minidb.catalog.catalog_manager import CatalogManager
from minidb.compiler.plan import CreateTablePlan
from minidb.core import errors
from minidb.core.disk_types import INVALID_PAGE_ID
from minidb.core.schema import ColumnDef, DataType, Schema
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.data_page import DataPage
from minidb.storage.file_manager import FileManager
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from tests.fixtures.contracts import STUDENT_SCHEMA, span


def open_session(path, policy='lru'):
    fm = FileManager.open(str(path))
    buffer = BufferPool(fm, capacity=1, policy=policy)
    storage = StorageEngine(buffer, RowCodec(), fm)
    try:
        catalog = CatalogManager.bootstrap_or_load(storage, fm.is_new)
    except BaseException:
        storage.abort()
        raise
    return fm, buffer, storage, catalog


def create_table(storage, catalog, schema=STUDENT_SCHEMA):
    location = span('CREATE TABLE student(id INT, name VARCHAR, age INT);')
    Executor().execute(CreateTablePlan('student', schema, location),
                       ExecutionContext(catalog, storage))
    return catalog.find_table('student')


class RealStorageIntegrationTests(unittest.TestCase):
    def test_consecutive_reclaim_reuse_and_root_reset_for_both_policies(self):
        for policy in ('lru', 'fifo'):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'real.db'
                fm, buffer, storage, catalog = open_session(path, policy)
                try:
                    table = create_table(storage, catalog)
                    rows = [(i, str(i) * 3000, 20) for i in range(4)]
                    ids = [storage.insert_row(table, row) for row in rows]
                    self.assertEqual(len({rid.page_id for rid in ids}), 4)
                    size = path.stat().st_size
                    for rid in ids[1:3]:
                        self.assertTrue(storage.delete_row(table, rid))
                    self.assertEqual(storage.reclaim_empty_pages(table), 2)
                    self.assertEqual([r.values for r in storage.scan_rows(table)], [rows[0], rows[3]])
                    replacement = (9, 'new' * 1000, 30)
                    reused = storage.insert_row(table, replacement)
                    self.assertEqual(reused.page_id, ids[2].page_id)
                    self.assertEqual(path.stat().st_size, size)
                    storage.close()
                    fm, buffer, storage, catalog = open_session(path, policy)
                    self.assertEqual(catalog.find_table('student'), table)
                    records = list(storage.scan_rows(table))
                    self.assertEqual([r.values for r in records], [rows[0], rows[3], replacement])
                    for record in records:
                        storage.delete_row(table, record.row_id)
                    self.assertEqual(storage.reclaim_empty_pages(table), 2)
                    root = DataPage(buffer.get_page(table.ref.root_page_id),
                                    page_id=table.ref.root_page_id, expected_table_id=table.ref.table_id)
                    self.assertEqual(root.header.slot_count, 0)
                    self.assertEqual(root.header.next_page_id, INVALID_PAGE_ID)
                    new_id = storage.insert_row(table, (10, 'root reused', 40))
                    self.assertEqual(new_id.page_id, table.ref.root_page_id)
                    storage.close()
                finally:
                    storage.abort()

    def test_real_insert_maximum_and_oversize_before_any_page_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'boundary.db'
            fm, buffer, storage, catalog = open_session(path)
            try:
                schema = Schema((ColumnDef('text', DataType.VARCHAR),))
                table = create_table(storage, catalog, schema)
                row = ('x' * 4052,)
                storage.insert_row(table, row)
                storage.sync()
                before, stats = path.read_bytes(), buffer.stats()
                with patch.object(buffer, 'new_page', wraps=buffer.new_page) as allocate, \
                     patch.object(buffer, 'write_page', wraps=buffer.write_page) as write:
                    with self.assertRaises(errors.DbError) as caught:
                        storage.insert_row(table, ('x' * 4053,))
                    self.assertEqual(caught.exception.code, errors.ROW_TOO_LARGE)
                    allocate.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(buffer.stats(), stats)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual([r.values for r in storage.scan_rows(table)], [row])
                storage.close()
            finally:
                storage.abort()

    def test_failed_real_writeback_then_abort_does_not_retry_or_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            fm, buffer, storage, catalog = open_session(Path(directory) / 'failure.db')
            try:
                table = create_table(storage, catalog)
                storage.sync()
                storage.insert_row(table, (1, 'pending', 20))
                scan = storage.scan_rows(table)
                failure = errors.DbError(errors.ErrorStage.STORAGE, errors.IO_WRITE_FAILED,
                                         'injected', context={'operation': 'write_page'})
                with patch.object(fm, 'write_page', side_effect=failure) as write, \
                     patch.object(fm, 'sync', wraps=fm.sync) as sync:
                    with self.assertRaises(errors.DbError) as caught:
                        storage.sync()
                    self.assertIs(caught.exception, failure)
                    storage.abort()
                    storage.abort()
                    self.assertEqual(write.call_count, 1)
                    sync.assert_not_called()
                self.assertTrue(storage.is_closed)
                self.assertEqual(storage.active_scan_count, 0)
                self.assertEqual(list(scan), [])
            finally:
                storage.abort()

    def test_new_process_restores_catalog_rows_and_free_list(self):
        common = '''
import json, sys
from test_real_storage_integration import open_session, create_table
fm, buffer, storage, catalog = open_session(sys.argv[1], sys.argv[2])
'''
        writer = common + '''
try:
    table = create_table(storage, catalog)
    rows = [(1, '中' * 900, 20), (2, 'B' * 3000, 21), (3, 'C' * 3000, 22)]
    ids = [storage.insert_row(table, row) for row in rows]
    storage.delete_row(table, ids[1])
    assert storage.reclaim_empty_pages(table) == 1
    storage.close()
    print(json.dumps({'root': table.ref.root_page_id, 'freed': ids[1].page_id, 'rows': [rows[0], rows[2]]}))
finally:
    storage.abort()
'''
        reader = common + '''
try:
    table = catalog.find_table('student')
    assert table is not None
    rows = [record.values for record in storage.scan_rows(table)]
    size = __import__('os').path.getsize(sys.argv[1])
    rid = storage.insert_row(table, (4, 'D' * 3000, 23))
    assert __import__('os').path.getsize(sys.argv[1]) == size
    storage.close()
    print(json.dumps({'root': table.ref.root_page_id, 'reused': rid.page_id, 'rows': rows}))
finally:
    storage.abort()
'''
        verifier = common + '''
try:
    table = catalog.find_table('student')
    print(json.dumps([record.values for record in storage.scan_rows(table)]))
    storage.close()
finally:
    storage.abort()
'''
        with tempfile.TemporaryDirectory() as directory:
            for policy in ('lru', 'fifo'):
                with self.subTest(policy=policy):
                    path = Path(directory) / (policy + '.db')
                    def run(code):
                        result = subprocess.run([sys.executable, '-B', '-c',
                                                 'import sys; sys.path.insert(0, "tests")\n' + code,
                                                 str(path), policy],
                                                cwd=Path(__file__).resolve().parents[1],
                                                capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        return json.loads(result.stdout)
                    first, second = run(writer), run(reader)
                    self.assertEqual(second['root'], first['root'])
                    self.assertEqual(second['rows'], first['rows'])
                    self.assertEqual(second['reused'], first['freed'])
                    self.assertEqual(run(verifier), first['rows'] + [[4, 'D' * 3000, 23]])


if __name__ == '__main__':
    unittest.main()
