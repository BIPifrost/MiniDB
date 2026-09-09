import unittest

from tests.fakes.in_memory_buffer_pool import (
    PAGE_SIZE,
    InMemoryBufferPool,
    InMemoryFileManager,
)


def page_bytes(marker: int) -> bytes:
    return bytes([marker]) + bytes(PAGE_SIZE - 1)


class InMemoryBufferPoolTests(unittest.TestCase):
    def test_constructor_and_file_manager_identity(self):
        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager, capacity=2, policy="lru")

        self.assertIs(buffer.file_manager, file_manager)
        for invalid_capacity in (True, 0, -1, "2"):
            with self.assertRaises((TypeError, ValueError)):
                InMemoryBufferPool(file_manager, capacity=invalid_capacity)
        with self.assertRaises(ValueError):
            InMemoryBufferPool(file_manager, policy="LRU")

    def test_new_page_is_zeroed_cached_and_dirty(self):
        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager, capacity=1)

        page_id = buffer.new_page()

        self.assertEqual(buffer.get_page(page_id), bytes(PAGE_SIZE))
        self.assertEqual(buffer.cached_page_ids, (page_id,))
        self.assertEqual(buffer.stats().writebacks, 0)
        buffer.flush_page(page_id)
        self.assertEqual(buffer.stats().writebacks, 1)

    def test_get_returns_data_independent_from_caller_changes(self):
        file_manager = InMemoryFileManager()
        page_id = file_manager.allocate_page()
        file_manager.write_page(page_id, page_bytes(7))
        buffer = InMemoryBufferPool(file_manager)

        caller_copy = bytearray(buffer.get_page(page_id))
        caller_copy[0] = 99

        self.assertEqual(buffer.get_page(page_id)[0], 7)
        self.assertEqual(buffer.stats().requests, 2)
        self.assertEqual(buffer.stats().hits, 1)
        self.assertEqual(buffer.stats().misses, 1)

    def test_lru_and_fifo_choose_different_victims(self):
        def prepared(policy):
            file_manager = InMemoryFileManager()
            page_ids = [file_manager.allocate_page() for _ in range(3)]
            for marker, page_id in enumerate(page_ids, start=1):
                file_manager.write_page(page_id, page_bytes(marker))
            return InMemoryBufferPool(file_manager, capacity=2, policy=policy), page_ids

        lru, (a, b, c) = prepared("lru")
        lru.get_page(a)
        lru.get_page(b)
        lru.get_page(a)
        lru.get_page(c)
        self.assertEqual(lru.cached_page_ids, (a, c))

        fifo, (a, b, c) = prepared("fifo")
        fifo.get_page(a)
        fifo.get_page(b)
        fifo.get_page(a)
        fifo.get_page(c)
        self.assertEqual(fifo.cached_page_ids, (b, c))

        for buffer in (lru, fifo):
            stats = buffer.stats()
            self.assertEqual(
                (stats.requests, stats.hits, stats.misses, stats.evictions),
                (4, 1, 3, 1),
            )
            self.assertEqual(stats.hit_rate, 0.25)

    def test_dirty_eviction_writes_page_before_removal(self):
        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager, capacity=1)
        first = buffer.new_page()
        buffer.write_page(first, page_bytes(11))

        second = buffer.new_page()

        self.assertEqual(buffer.cached_page_ids, (second,))
        self.assertEqual(file_manager.read_page(first)[0], 11)
        self.assertEqual(buffer.stats().writebacks, 1)
        self.assertEqual(buffer.stats().evictions, 1)

    def test_invalid_write_does_not_evict_or_change_stats(self):
        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager, capacity=1)
        first = buffer.new_page()
        second = file_manager.allocate_page()
        before = buffer.stats()

        with self.assertRaises(ValueError):
            buffer.write_page(second, bytes(PAGE_SIZE - 1))

        self.assertEqual(buffer.cached_page_ids, (first,))
        self.assertEqual(buffer.stats(), before)

    def test_free_discards_dirty_data_and_reallocated_page_is_zeroed(self):
        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager)
        page_id = buffer.new_page()
        buffer.write_page(page_id, page_bytes(21))

        buffer.free_page(page_id)
        reused_page_id = buffer.new_page()

        self.assertEqual(reused_page_id, page_id)
        self.assertEqual(buffer.get_page(reused_page_id), bytes(PAGE_SIZE))
        self.assertEqual(buffer.stats().writebacks, 0)

    def test_reserved_pages_and_unallocated_pages_are_rejected(self):
        buffer = InMemoryBufferPool(InMemoryFileManager())

        with self.assertRaises(ValueError):
            buffer.get_page(0)
        with self.assertRaises(ValueError):
            buffer.free_page(1)
        with self.assertRaises(KeyError):
            buffer.get_page(99)

    def test_flush_all_uses_page_id_order_and_does_not_change_lru(self):
        file_manager = InMemoryFileManager()
        page_ids = [file_manager.allocate_page() for _ in range(3)]
        buffer = InMemoryBufferPool(file_manager, capacity=3, policy="lru")
        for page_id in reversed(page_ids):
            buffer.write_page(page_id, page_bytes(page_id))
        before_order = buffer.cached_page_ids
        file_manager.write_log.clear()

        buffer.flush_all()

        self.assertEqual(file_manager.write_log, sorted(page_ids))
        self.assertEqual(buffer.cached_page_ids, before_order)
        self.assertEqual(buffer.stats().writebacks, 3)

    def test_stats_snapshot_is_immutable(self):
        buffer = InMemoryBufferPool(InMemoryFileManager())
        stats = buffer.stats()

        self.assertEqual(stats.hit_rate, 0.0)
        with self.assertRaises(AttributeError):
            stats.hits = 1


if __name__ == "__main__":
    unittest.main()
