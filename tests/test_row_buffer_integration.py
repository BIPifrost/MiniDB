"""正式 RowCodec/BufferPool/FileManager 联调；页装配仅为测试夹具。

本文件不实现 StorageEngine，也不代表 DataPage 增删算法已经验收。
"""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, INVALID_PAGE_ID
from minidb.core.schema import ColumnDef, DataType, Schema
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_manager import FileManager
from minidb.storage.row_codec import RowCodec


SCHEMA = Schema((ColumnDef('id', DataType.INT), ColumnDef('name', DataType.VARCHAR)))
HEADER = struct.Struct('<4sHHIIHHHHII')
SLOT = struct.Struct('<HHB3x')


def fixture_page(row, schema=SCHEMA, next_page=INVALID_PAGE_ID):
    """按工作计划装配单条记录的标准页，不作为生产数据页实现。"""
    encoded = RowCodec().encode(row, schema)
    page = bytearray(PAGE_SIZE)
    HEADER.pack_into(page, 0, b'MDPG', 1, 1, 1, next_page,
                     1, 32 + len(encoded), 4088, 0, 1, 0)
    page[32:32 + len(encoded)] = encoded
    SLOT.pack_into(page, 4088, 32, len(encoded), 1)
    return bytes(page)


def fixture_row(page, schema=SCHEMA):
    offset, size, state = SLOT.unpack_from(page, 4088)
    if state != 1:
        raise AssertionError('测试夹具预期为活动记录')
    return RowCodec().decode(page[offset:offset + size], schema)


class RowBufferIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'integration.db'
        self.fm = FileManager.open(str(self.path))
        self.addCleanup(self.fm.close)

    def test_golden_page_survives_dirty_eviction(self):
        expected_header = bytes.fromhex('4d4450470100010001000000ffffffff01002f00f80f00000100000000000000')
        page = fixture_page((1, '中'))
        self.assertEqual(page[:32], expected_header)
        self.assertEqual(page[32:47].hex(), '010000000000000003000000e4b8ad')
        self.assertEqual(page[47:4088], bytes(4041))
        self.assertEqual(page[4088:].hex(), '20000f0001000000')
        self.assertEqual(hashlib.sha256(page).hexdigest(),
                         'd21d4e75e485ebde191e0d0d3409390daf70f5ebae38a7d6977d0ac13c051950')
        pool = BufferPool(self.fm, 1)
        root = pool.new_page()
        pool.write_page(root, page)
        pool.new_page()
        self.assertEqual(pool.get_page(root), page)
        self.assertEqual(fixture_row(pool.get_page(root)), (1, '中'))

    def test_maximum_row_fits_exactly(self):
        schema = Schema((ColumnDef('text', DataType.VARCHAR),))
        row = ('a' * 4052,)
        page = fixture_page(row, schema)
        self.assertEqual(HEADER.unpack_from(page)[6:8], (4088, 4088))
        pool = BufferPool(self.fm, 1)
        page_id = pool.new_page()
        pool.write_page(page_id, page)
        pool.flush_all()
        self.assertEqual(fixture_row(self.fm.read_page(page_id), schema), row)

    def test_oversized_row_rejected_before_fixture_allocation(self):
        # 上层必须先编码再分配；这里只验证推荐的调用顺序，非 StorageEngine。
        pool = BufferPool(self.fm, 1)
        schema = Schema((ColumnDef('text', DataType.VARCHAR),))
        before = self.path.read_bytes()
        with patch.object(pool, 'new_page', wraps=pool.new_page) as allocate:
            with patch.object(pool, 'write_page', wraps=pool.write_page) as write:
                with self.assertRaises(errors.DbError) as caught:
                    page = fixture_page(('a' * 4053,), schema)
                    pool.write_page(pool.new_page(), page)
                self.assertEqual(caught.exception.code, errors.ROW_TOO_LARGE)
                allocate.assert_not_called()
                write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_capacity_one_fresh_copy_preserves_record_and_link(self):
        pool = BufferPool(self.fm, 1)
        root = pool.new_page()
        pool.write_page(root, fixture_page((1, 'old')))
        stale = pool.get_page(root)
        tail = pool.new_page()
        pool.write_page(tail, fixture_page((2, 'tail')))
        pool.write_page(root, fixture_page((1, 'updated')))
        pool.get_page(tail)  # 使根页淘汰；修改指针前必须重新获取副本。
        fresh = bytearray(pool.get_page(root))
        struct.pack_into('<I', fresh, 12, tail)
        pool.write_page(root, bytes(fresh))
        pool.flush_all()
        self.assertEqual(fixture_row(stale), (1, 'old'))
        self.assertEqual(fixture_row(self.fm.read_page(root)), (1, 'updated'))
        self.assertEqual(struct.unpack_from('<I', self.fm.read_page(root), 12)[0], tail)

    def test_unlink_free_and_reuse_does_not_restore_old_bytes(self):
        pool = BufferPool(self.fm, 1)
        root, tail = pool.new_page(), pool.new_page()
        pool.write_page(root, fixture_page((1, 'root'), next_page=tail))
        pool.write_page(tail, fixture_page((2, 'deleted')))
        pool.write_page(root, fixture_page((1, 'root')))
        pool.free_page(tail)
        self.assertEqual(pool.new_page(), tail)
        self.assertEqual(pool.get_page(tail), bytes(PAGE_SIZE))
        pool.write_page(tail, fixture_page((3, 'reused')))
        pool.flush_all()
        self.fm.sync()
        self.fm.close()
        reopened = FileManager.open(str(self.path))
        self.addCleanup(reopened.close)
        self.assertEqual(fixture_row(reopened.read_page(root)), (1, 'root'))
        self.assertEqual(struct.unpack_from('<I', reopened.read_page(root), 12)[0], INVALID_PAGE_ID)
        self.assertEqual(fixture_row(reopened.read_page(tail)), (3, 'reused'))

    def test_independent_process_write_then_read_page_chain(self):
        self.fm.close()
        # 两次启动独立解释器，读取端不共享缓存、句柄或 Python 对象。
        common = '''
import json, struct, sys
from test_row_buffer_integration import fixture_page, fixture_row
from minidb.storage.file_manager import FileManager
from minidb.storage.buffer_pool import BufferPool
from minidb.core.disk_types import INVALID_PAGE_ID
fm = FileManager.open(sys.argv[1])
pool = BufferPool(fm, 1, sys.argv[2])
'''
        writer = common + '''
rows = [(1, '中'), (-9223372036854775808, ''), (9223372036854775807, 'a\\n\\x00b'), (1, '中')]
pages = [pool.new_page() for _ in rows]
for i, row in enumerate(rows):
    pool.write_page(pages[i], fixture_page(row, next_page=pages[i+1] if i+1<len(pages) else INVALID_PAGE_ID))
pool.flush_all()
fm.sync()
fm.close()
print(json.dumps({'root': pages[0], 'rows': rows}))
'''
        reader = common + '''
page_id = int(sys.argv[3])
rows, seen = [], set()
while page_id != INVALID_PAGE_ID:
    assert page_id not in seen
    seen.add(page_id)
    page = pool.get_page(page_id)
    rows.append(fixture_row(page))
    page_id = struct.unpack_from('<I', page, 12)[0]
fm.close()
print(json.dumps({'rows': rows, 'misses': pool.stats().misses}))
'''
        repo = Path(__file__).resolve().parents[1]
        bootstrap = 'import sys; sys.path.insert(0, "tests")\n'
        for policy in ('lru', 'fifo'):
            with self.subTest(policy=policy):
                path = Path(self.temp.name) / (policy + '.db')
                def run(code, *extra):
                    result = subprocess.run([sys.executable, '-B', '-c', bootstrap + code,
                                             str(path), policy, *extra], cwd=repo,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)
                written = run(writer)
                loaded = run(reader, str(written['root']))
                self.assertEqual(loaded['rows'], written['rows'])
                self.assertEqual(loaded['misses'], 4)


if __name__ == '__main__':
    unittest.main()
