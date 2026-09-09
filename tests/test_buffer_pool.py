"""真实页缓存契约测试，数据库文件均位于临时目录。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb.core import errors
from minidb.core.disk_types import BufferStats, PAGE_SIZE
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_manager import FileManager


class BufferPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'buffer.db'
        self.fm = FileManager.open(str(self.path)); self.addCleanup(self.fm.close)
        self.pages = [self.fm.allocate_page() for _ in range(4)]
        for i, page in enumerate(self.pages):
            self.fm.write_page(page, bytes([i + 1]) * PAGE_SIZE)
        self.fm.sync()

    def assert_code(self, code, fn, *args):
        with self.assertRaises(errors.DbError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_f02_real_cache_lru_and_fifo(self):
        a, b, c, _ = self.pages
        for policy, victim in (('lru', b), ('fifo', a)):
            pool = BufferPool(self.fm, 2, policy)
            with self.assertLogs('minidb.storage.buffer_pool', level='DEBUG') as captured:
                for page in (a, b, a, c):
                    pool.get_page(page)
            self.assertEqual(pool.stats(), BufferStats(4, 1, 3, 1, 0))
            events = [json.loads(record.getMessage()) for record in captured.records]
            self.assertEqual([e['event_type'] for e in events],
                             ['MISS', 'LOAD', 'MISS', 'LOAD', 'HIT', 'MISS', 'EVICT', 'LOAD'])
            self.assertEqual(next(e['page_id'] for e in events if e['event_type']=='EVICT'), victim)
            self.assertEqual(pool.stats().hit_rate, .25)
            for event in events:
                self.assertEqual(set(event), {'kind','event_type','page_id','dirty_before','policy','reason'})
                self.assertEqual(event['kind'], 'STORAGE_EVENT')
                self.assertEqual(event['policy'], policy)

    def test_f03_dirty_eviction_writeback_precedes_removal(self):
        a, b, *_ = self.pages
        pool = BufferPool(self.fm, 1)
        pool.write_page(a, b'X'*PAGE_SIZE)
        with self.assertLogs('minidb.storage.buffer_pool', level='DEBUG') as captured:
            pool.get_page(b)
        events = [r.storage_event for r in captured.records]
        self.assertEqual([e['event_type'] for e in events], ['MISS','WRITEBACK','EVICT','LOAD'])
        self.assertTrue(events[1]['dirty_before'])
        self.assertFalse(events[2]['dirty_before'])
        self.assertEqual(self.fm.read_page(a), b'X'*PAGE_SIZE)
        self.assertEqual(pool.stats(), BufferStats(1, 0, 1, 1, 1))
        self.assertEqual(pool.get_page(a), b'X'*PAGE_SIZE)

    def test_copy_read_submit_and_no_read_for_uncached_write(self):
        a = self.pages[0]; pool = BufferPool(self.fm, 1)
        original = pool.get_page(a)
        changed = bytearray(original); changed[0] = 99
        self.assertEqual(pool.get_page(a), original)
        pool.write_page(a, bytes(changed))
        self.assertIs(type(pool.get_page(a)), bytes)
        self.assertEqual(original[0], 1)
        self.assertEqual(pool.get_page(a)[0], 99)
        fresh = BufferPool(self.fm, 1)
        with patch.object(self.fm, 'read_page', side_effect=AssertionError('must not read')):
            fresh.write_page(self.pages[1], b'Z'*PAGE_SIZE)
        self.assertEqual(fresh.stats(), BufferStats())

    def test_new_page_zero_dirty_no_get_counters(self):
        pool = BufferPool(self.fm, 1)
        page = pool.new_page()
        self.assertEqual(pool.stats(), BufferStats())
        self.assertTrue(pool._frames[page].dirty)
        self.assertEqual(pool.get_page(page), bytes(PAGE_SIZE))
        pool.flush_all()
        self.assertEqual(pool.stats(), BufferStats(1, 1, 0, 0, 1))
        self.assertEqual(self.fm.read_page(page), bytes(PAGE_SIZE))

    def test_free_discards_dirty_cache_and_reuse_does_not_restore_old_data(self):
        a = self.pages[0]; pool = BufferPool(self.fm, 2)
        pool.write_page(a, b'Z'*PAGE_SIZE)
        with self.assertLogs('minidb.storage.buffer_pool', level='DEBUG') as captured:
            pool.free_page(a)
        self.assertEqual([r.storage_event['event_type'] for r in captured.records], ['FREE'])
        self.assertTrue(captured.records[0].storage_event['dirty_before'])
        self.assertEqual(pool.stats(), BufferStats())
        self.assert_code(errors.PAGE_NOT_ALLOCATED, pool.get_page, a)
        self.assert_code(errors.PAGE_ALREADY_FREE, pool.free_page, a)
        self.assertEqual(pool.new_page(), a)
        self.assertEqual(pool.get_page(a), bytes(PAGE_SIZE))
        pool.flush_all(); self.fm.sync()
        self.assertEqual(self.fm.read_page(a), bytes(PAGE_SIZE))

    def test_invalid_calls_do_not_mutate_file_cache_or_counters(self):
        a = self.pages[0]; pool = BufferPool(self.fm, 1); pool.write_page(a, b'Q'*PAGE_SIZE)
        cases = [(pool.get_page, (0,), errors.RESERVED_PAGE),
                 (pool.free_page, (1,), errors.RESERVED_PAGE),
                 (pool.get_page, (True,), errors.PAGE_ID_INVALID),
                 (pool.get_page, (999,), errors.PAGE_NOT_ALLOCATED),
                 (pool.flush_page, (999,), errors.PAGE_NOT_ALLOCATED),
                 (pool.write_page, (self.pages[1], bytes(4095)), errors.INVALID_ARGUMENT),
                 (pool.write_page, (a, bytearray(4096)), errors.INVALID_ARGUMENT)]
        before = (self.path.read_bytes(), pool.stats(), pool._replacement.snapshot(), pool._frames[a].data)
        for fn, args, code in cases:
            with self.subTest(fn=fn.__name__, args=args[:1]):
                self.assert_code(code, fn, *args)
                self.assertEqual((self.path.read_bytes(), pool.stats(), pool._replacement.snapshot(), pool._frames[a].data), before)

    def test_flush_sorted_idempotent_no_order_change_and_no_fsync(self):
        a, b, c, _ = self.pages; pool = BufferPool(self.fm, 3)
        for page in (c, a, b): pool.write_page(page, b'Q'*PAGE_SIZE)
        order = pool._replacement.snapshot()
        with patch.object(self.fm, 'write_page', wraps=self.fm.write_page) as write, \
             patch.object(self.fm, 'sync', side_effect=AssertionError('pool must not sync')):
            pool.flush_all(); pool.flush_all(); pool.flush_page(a)
            self.assertEqual([call.args[0] for call in write.call_args_list], [a,b,c])
        self.assertEqual(pool._replacement.snapshot(), order)
        self.assertEqual(pool.stats(), BufferStats(writebacks=3))
        pool.flush_page(self.pages[3])  # valid but uncached, no write
        self.assertEqual(pool.stats().writebacks, 3)

    def test_read_failure_preserves_cached_page_and_counts_miss(self):
        a, b, *_ = self.pages; pool = BufferPool(self.fm, 1); pool.get_page(a)
        error = self.fm._error(errors.IO_READ_FAILED, 'read_page', cause='injected')
        with patch.object(self.fm, 'read_page', side_effect=error):
            self.assert_code(errors.IO_READ_FAILED, pool.get_page, b)
        self.assertEqual(pool._replacement.snapshot(), (a,))
        self.assertEqual(pool.stats(), BufferStats(2,0,2))
        with patch.object(self.fm, 'read_page') as read:
            self.assertIs(self.assert_code(errors.IO_READ_FAILED, pool.get_page, b), error)
            read.assert_not_called()

    def test_writeback_failure_keeps_dirty_victim(self):
        a,b,*_ = self.pages; pool=BufferPool(self.fm,1); pool.write_page(a,b'X'*PAGE_SIZE)
        failure=self.fm._error(errors.IO_WRITE_FAILED,'write_page',cause='injected')
        with patch.object(self.fm,'write_page',side_effect=failure), \
             self.assertLogs('minidb.storage.buffer_pool', level='DEBUG') as captured:
            self.assert_code(errors.IO_WRITE_FAILED,pool.get_page,b)
        self.assertEqual([r.storage_event['event_type'] for r in captured.records], ['MISS'])
        self.assertTrue(pool._frames[a].dirty)
        self.assertEqual(pool._replacement.snapshot(),(a,))
        self.assertEqual(pool.stats(),BufferStats(1,0,1))
        with patch.object(self.fm,'write_page') as write:
            self.assert_code(errors.IO_WRITE_FAILED,pool.flush_all)
            write.assert_not_called()

    def test_flush_failure_stops_after_successful_prefix(self):
        a,b,c,_=self.pages; pool=BufferPool(self.fm,3)
        for page in (c,b,a): pool.write_page(page,b'X'*PAGE_SIZE)
        original=self.fm.write_page
        def write(page,data):
            if page==b: raise self.fm._error(errors.IO_WRITE_FAILED,'write_page',cause='injected')
            original(page,data)
        with patch.object(self.fm,'write_page',side_effect=write) as spy:
            self.assert_code(errors.IO_WRITE_FAILED,pool.flush_all)
            self.assertEqual([call.args[0] for call in spy.call_args_list],[a,b])
        self.assertEqual(pool.stats().writebacks,1)
        self.assertFalse(pool._frames[a].dirty)
        self.assertTrue(pool._frames[b].dirty)
        self.assertTrue(pool._frames[c].dirty)

    def test_release_failure_emits_no_success_and_cannot_continue(self):
        a=self.pages[0];pool=BufferPool(self.fm,1);pool.write_page(a,b'X'*PAGE_SIZE)
        failure=self.fm._error(errors.IO_WRITE_FAILED,'release_page',cause='injected')
        with patch.object(self.fm,'release_page',side_effect=failure), \
             patch('minidb.storage.buffer_pool._LOG.debug') as log:
            self.assert_code(errors.IO_WRITE_FAILED,pool.free_page,a)
            log.assert_not_called()
        self.assertNotIn(a,pool._frames)
        self.assertEqual(pool.stats(),BufferStats())
        self.assert_code(errors.IO_WRITE_FAILED,pool.new_page)

    def test_constructor_and_identity(self):
        for capacity in (0,-1,True,1.0):
            self.assert_code(errors.INVALID_ARGUMENT,BufferPool,self.fm,capacity)
        for policy in ('LRU','random',None):
            self.assert_code(errors.INVALID_ARGUMENT,BufferPool,self.fm,1,policy)
        self.assert_code(errors.INVALID_ARGUMENT,BufferPool,None)
        pool=BufferPool(self.fm)
        self.assertIs(pool.file_manager,self.fm)
        with self.assertRaises(AttributeError): pool.file_manager=None

    def test_closed_file_rejected_even_on_hit_or_empty_flush(self):
        pool=BufferPool(self.fm);pool.get_page(1);self.fm.close()
        self.assert_code(errors.CLOSED,pool.get_page,1)
        self.assert_code(errors.CLOSED,pool.flush_all)
        self.assertEqual(pool.stats(),BufferStats(1,0,1))

    def test_write_hit_updates_lru_but_not_fifo(self):
        a,b,c,_=self.pages
        for policy,victim in (('lru',b),('fifo',a)):
            pool=BufferPool(self.fm,2,policy);pool.get_page(a);pool.get_page(b)
            pool.write_page(a,b'X'*PAGE_SIZE);pool.get_page(c)
            self.assertNotIn(victim,pool._frames)

    def test_capacity_one_multi_page_flush_and_reopen(self):
        for policy in ('lru','fifo'):
            pool=BufferPool(self.fm,1,policy)
            expected={}
            for i,page in enumerate(self.pages):
                expected[page]=bytes([20+i])*PAGE_SIZE
                pool.write_page(page,expected[page])
            for page in self.pages: self.assertEqual(pool.get_page(page),expected[page])
            pool.flush_all();self.fm.sync();self.fm.close()
            self.fm=FileManager.open(str(self.path));self.addCleanup(self.fm.close)
            for page,data in expected.items(): self.assertEqual(self.fm.read_page(page),data)


if __name__=='__main__': unittest.main()
