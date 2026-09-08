"""Contract tests for shared storage definitions (no database I/O)."""

from dataclasses import FrozenInstanceError, asdict
import json
import struct
import unittest

from minidb.core.disk_types import (
    BufferStats, CATALOG_ROOT_PAGE_ID, FIRST_ALLOCATABLE_PAGE_ID,
    FORMAT_VERSION, HEADER_PAGE_ID, INITIAL_NEXT_PAGE_ID, INVALID_PAGE_ID,
    MAX_NEXT_PAGE_ID, MAX_PAGE_ID, MIN_PAGE_ID, PAGE_SIZE, PageId,
)


class DiskTypesTests(unittest.TestCase):
    def test_format_and_reserved_pages(self):
        self.assertEqual(PAGE_SIZE, 4096)
        self.assertEqual(FORMAT_VERSION, 1)
        self.assertEqual((HEADER_PAGE_ID, CATALOG_ROOT_PAGE_ID), (0, 1))
        self.assertEqual(FIRST_ALLOCATABLE_PAGE_ID, 2)
        self.assertEqual(INITIAL_NEXT_PAGE_ID * PAGE_SIZE, 8192)
        self.assertIs(PageId, int)

    def test_unsigned_page_boundaries(self):
        self.assertEqual(MIN_PAGE_ID, 0)
        self.assertEqual(MAX_PAGE_ID, 4294967294)
        self.assertEqual(INVALID_PAGE_ID, 4294967295)
        self.assertEqual(MAX_NEXT_PAGE_ID, INVALID_PAGE_ID)
        self.assertEqual(MAX_PAGE_ID + 1, MAX_NEXT_PAGE_ID)
        self.assertEqual(struct.pack('<I', INVALID_PAGE_ID), b'\xff' * 4)

    def test_zero_snapshot(self):
        self.assertEqual(asdict(BufferStats()), {
            'requests': 0, 'hits': 0, 'misses': 0, 'evictions': 0,
            'writebacks': 0, 'hit_rate': 0.0,
        })
        self.assertIs(type(BufferStats().hit_rate), float)

    def test_f02_snapshot_and_json(self):
        stats = BufferStats(requests=4, hits=1, misses=3, evictions=1)
        self.assertEqual(stats.hit_rate, 0.25)
        self.assertEqual(json.loads(json.dumps(asdict(stats))), {
            'requests': 4, 'hits': 1, 'misses': 3, 'evictions': 1,
            'writebacks': 0, 'hit_rate': 0.25,
        })

    def test_immutable_and_detached_serialization(self):
        stats = BufferStats(requests=1, hits=1)
        for name in asdict(stats):
            with self.subTest(field=name), self.assertRaises(FrozenInstanceError):
                setattr(stats, name, 99)
        copy = asdict(stats)
        copy['hits'] = 0
        self.assertEqual(stats.hits, 1)
        self.assertEqual(stats.hit_rate, 1.0)
        with self.assertRaises(TypeError):
            BufferStats(hit_rate=0.5)

    def test_invalid_counter_types_and_negatives(self):
        for name in ('requests', 'hits', 'misses', 'evictions', 'writebacks'):
            for value in (True, False, 1.0, '1', None):
                with self.subTest(field=name, value=value), self.assertRaises(TypeError):
                    BufferStats(**{name: value})
            with self.subTest(field=name), self.assertRaises(ValueError):
                BufferStats(**{name: -1})

    def test_inconsistent_request_totals(self):
        for values in ({'requests': 1}, {'hits': 1},
                       {'requests': 2, 'hits': 2, 'misses': 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BufferStats(**values)

    def test_writes_do_not_require_get_requests(self):
        stats = BufferStats(evictions=3, writebacks=5)
        self.assertEqual(stats.requests, 0)
        self.assertEqual(stats.hit_rate, 0.0)
        self.assertEqual(stats.writebacks, 5)
        self.assertEqual(BufferStats(requests=3, misses=3).hit_rate, 0.0)




class FilePageLayoutTests(unittest.TestCase):
    """Golden bytes are transcribed from the spec, not from the encoder."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        fixture = Path(__file__).parent / 'fixtures' / 'file_pages_v1.json'
        cls.fixture = json.loads(fixture.read_text(encoding='utf-8'))

    def golden_page(self, name):
        item = self.fixture[name]
        return bytes.fromhex(item['prefix_hex']) + bytes(item['zero_padding_bytes'])

    def test_file_header_exact_golden_bytes(self):
        from minidb.storage.page import initial_file_header_page
        actual = initial_file_header_page()
        self.assertIs(type(actual), bytes)
        self.assertEqual(len(actual), 4096)
        self.assertEqual(actual, self.golden_page('initial_file_header'))

    def test_file_header_field_offsets(self):
        from minidb.storage import page
        actual = page.initial_file_header_page()
        self.assertEqual(actual[0:8], b'MINIDB01')
        self.assertEqual(page.FILE_MAGIC_OFFSET, 0)
        fields = (
            (page.FILE_VERSION_OFFSET, 8, 1),
            (page.FILE_PAGE_SIZE_OFFSET, 12, 4096),
            (page.FILE_CATALOG_ROOT_OFFSET, 16, 1),
            (page.FILE_NEXT_PAGE_ID_OFFSET, 20, 2),
            (page.FILE_FREE_HEAD_OFFSET, 24, 4294967295),
        )
        for offset, expected_offset, expected_value in fields:
            with self.subTest(offset=offset):
                self.assertEqual(offset, expected_offset)
                self.assertEqual(int.from_bytes(actual[offset:offset + 4], 'little'), expected_value)
        self.assertEqual(page.FILE_HEADER_STRUCT.size, 28)
        self.assertEqual(page.FILE_RESERVED_OFFSET, 28)
        self.assertEqual(page.FILE_RESERVED_SIZE, 4068)
        self.assertEqual(actual[28:], bytes(4068))

    def test_terminal_free_page_exact_golden_bytes(self):
        from minidb.storage import page
        actual = page.terminal_free_page()
        self.assertIs(type(actual), bytes)
        self.assertEqual(len(actual), 4096)
        self.assertEqual(actual, self.golden_page('terminal_free_page'))
        self.assertEqual(page.FREE_NEXT_PAGE_ID_OFFSET, 0)
        self.assertEqual(page.FREE_NEXT_STRUCT.size, 4)
        self.assertEqual(page.FREE_RESERVED_OFFSET, 4)
        self.assertEqual(page.FREE_RESERVED_SIZE, 4092)
        self.assertEqual(actual[:4], b'\xff' * 4)
        self.assertEqual(actual[4:], bytes(4092))

    def test_initial_file_fixture_contains_two_pages(self):
        from minidb.storage.page import initial_file_header_page
        reserved = self.golden_page('initial_catalog_reserved_page')
        self.assertEqual(reserved, bytes(4096))
        image = initial_file_header_page() + reserved
        self.assertEqual(len(image), self.fixture['initial_file_size'])
        self.assertEqual(len(image), 8192)
        self.assertNotEqual(reserved[:4], b'MDPG')

    def test_mutating_local_copy_does_not_change_initial_bytes(self):
        from minidb.storage.page import initial_file_header_page, terminal_free_page
        for factory in (initial_file_header_page, terminal_free_page):
            with self.subTest(factory=factory.__name__):
                original = factory()
                local = bytearray(original)
                local[0] ^= 255
                self.assertNotEqual(bytes(local), original)
                self.assertEqual(factory(), original)




class ReplacementPolicyTests(unittest.TestCase):
    def policy(self, name='lru'):
        from minidb.storage.replacement import ReplacementPolicy
        return ReplacementPolicy(name)

    def test_empty_policy(self):
        for name in ('lru', 'fifo'):
            p = self.policy(name)
            self.assertIsNone(p.victim())
            self.assertEqual(p.snapshot(), ())
            self.assertEqual(len(p), 0)

    def test_f02_order_with_two_resident_pages(self):
        for name, expected in (('lru', 3), ('fifo', 2)):
            with self.subTest(policy=name):
                p = self.policy(name)
                p.record_access(2)  # A admitted
                p.record_access(3)  # B admitted
                p.record_access(2)  # A hit
                self.assertEqual(p.victim(), expected)
                self.assertTrue(p.remove(expected))  # successful eviction
                p.record_access(4)  # C admitted
                self.assertEqual(len(p), 2)
                self.assertEqual(p.snapshot(), ((2, 4) if name == 'lru' else (3, 4)))

    def test_write_access_refreshes_lru_not_fifo(self):
        for name, order in (('lru', (3, 2)), ('fifo', (2, 3))):
            p = self.policy(name)
            p.record_access(2)
            p.record_access(3)
            p.record_access(2)  # also used after a successful write_page
            self.assertEqual(p.snapshot(), order)

    def test_peek_does_not_lose_victim_on_uncommitted_eviction(self):
        p = self.policy()
        p.record_access(2)
        p.record_access(3)
        before = p.snapshot()
        for _ in range(3):
            self.assertEqual(p.victim(), 2)
        # A caller whose writeback fails never calls remove.
        self.assertEqual(p.snapshot(), before)

    def test_free_and_reuse_has_new_admission_order(self):
        for name in ('lru', 'fifo'):
            p = self.policy(name)
            p.record_access(2)
            p.record_access(3)
            self.assertTrue(p.remove(2))
            self.assertFalse(p.remove(2))
            p.record_access(2)
            self.assertEqual(p.snapshot(), (3, 2))

    def test_snapshot_is_detached(self):
        p = self.policy()
        p.record_access(2)
        old = p.snapshot()
        p.record_access(3)
        self.assertEqual(old, (2,))
        self.assertEqual(p.snapshot(), (2, 3))
        self.assertEqual(p.victim(), 2)

    def test_invalid_helper_calls_do_not_change_order(self):
        p = self.policy()
        p.record_access(2)
        for method in (p.record_access, p.remove):
            for value, error in ((True, TypeError), (2.0, TypeError),
                                 ('2', TypeError), (None, TypeError),
                                 (0, ValueError), (-1, ValueError),
                                 (0xFFFFFFFF, ValueError)):
                with self.subTest(value=value), self.assertRaises(error):
                    method(value)
                self.assertEqual(p.snapshot(), (2,))

    def test_invalid_policy_and_readonly_name(self):
        for name in ('LRU', '', 'random', None, True):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.policy(name)
        p = self.policy()
        with self.assertRaises(AttributeError):
            p.policy = 'fifo'

    def test_catalog_page_and_maximum_real_page(self):
        p = self.policy()
        p.record_access(1)
        p.record_access(0xFFFFFFFE)
        self.assertEqual(p.snapshot(), (1, 0xFFFFFFFE))

    def test_order_against_independent_list_model(self):
        import random
        for name in ('lru', 'fifo'):
            p = self.policy(name)
            model = []
            rng = random.Random(20260908)
            for _ in range(300):
                page_id = rng.randint(1, 8)
                if rng.randrange(3) == 0:
                    existed = page_id in model
                    self.assertEqual(p.remove(page_id), existed)
                    if existed:
                        model.remove(page_id)
                else:
                    if page_id in model and name == 'lru':
                        model.remove(page_id)
                    if page_id not in model:
                        model.append(page_id)
                    p.record_access(page_id)
                self.assertEqual(p.snapshot(), tuple(model))
                self.assertEqual(p.victim(), model[0] if model else None)




class PageValidationTests(unittest.TestCase):
    def assert_error(self, code, callback, **kwargs):
        from minidb.storage._errors import DbError, ErrorStage
        with self.assertRaises(DbError) as caught:
            callback(**kwargs)
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertIs(error.stage, ErrorStage.STORAGE)
        self.assertIsNone(error.span)
        self.assertIn('operation', error.context)
        self.assertIn('expected', error.context)
        self.assertIn('actual', error.context)
        json.dumps(error.context, allow_nan=False)
        return error

    def test_temporary_error_contract(self):
        from minidb.storage._errors import DbError, ErrorStage, DB_FORMAT_MISMATCH
        context = {'page_id': 0, 'expected': [1]}
        error = DbError(ErrorStage.STORAGE, DB_FORMAT_MISMATCH, 'bad header', context=context)
        context['expected'].append(2)
        self.assertEqual(error.context['expected'], [1])
        self.assertIsInstance(error, Exception)
        self.assertIn('DB_FORMAT_MISMATCH', str(error))
        self.assertEqual(error.args, ('bad header',))
        self.assertEqual({s.value for s in ErrorStage},
                         {'LEXICAL', 'SYNTAX', 'SEMANTIC', 'PLAN', 'EXECUTION', 'STORAGE'})

    def test_header_roundtrip_and_initial_compatibility(self):
        from minidb.storage.page import FileHeader, encode_file_header, decode_file_header, initial_file_header_page
        self.assertEqual(encode_file_header(FileHeader()), initial_file_header_page())
        for header in (FileHeader(), FileHeader(5, 2), FileHeader(0xFFFFFFFF, 0xFFFFFFFE)):
            with self.subTest(header=header):
                encoded = encode_file_header(header)
                self.assertEqual(len(encoded), 4096)
                self.assertEqual(decode_file_header(encoded, file_size=header.next_page_id * 4096), header)

    def test_header_corrupt_fixed_fields(self):
        from minidb.storage.page import initial_file_header_page, decode_file_header
        for offset, field in ((0, 'magic'), (8, 'format_version'), (12, 'page_size'),
                              (16, 'catalog_root_page_id'), (28, 'reserved'), (4095, 'reserved')):
            data = bytearray(initial_file_header_page())
            data[offset] ^= 1
            with self.subTest(field=field):
                error = self.assert_error('DB_FORMAT_MISMATCH', decode_file_header,
                                          data=bytes(data), file_size=8192, path='temp.db')
                self.assertEqual(error.context['field'], field)
                self.assertEqual(error.context['path'], 'temp.db')
                self.assertEqual(error.context['page_id'], 0)

    def test_header_bad_boundary_and_free_head(self):
        from minidb.storage.page import initial_file_header_page, decode_file_header
        for offset, value in ((20, 0), (20, 1), (24, 0), (24, 1), (24, 2)):
            data = bytearray(initial_file_header_page())
            struct.pack_into('<I', data, offset, value)
            with self.subTest(offset=offset, value=value):
                self.assert_error('DB_FORMAT_MISMATCH', decode_file_header, data=bytes(data), file_size=8192)

    def test_header_truncated_and_extra_bytes(self):
        from minidb.storage.page import initial_file_header_page, decode_file_header
        data = initial_file_header_page()
        for size in (0, 27, 4095):
            self.assert_error('DB_FILE_TRUNCATED', decode_file_header, data=data[:size], file_size=8192)
        self.assert_error('DB_FORMAT_MISMATCH', decode_file_header, data=data+b'\0', file_size=8192)
        for file_size, code in ((0, 'DB_FILE_TRUNCATED'), (8191, 'DB_FILE_TRUNCATED'),
                                (8193, 'DB_FORMAT_MISMATCH'), (12288, 'DB_FORMAT_MISMATCH')):
            self.assert_error(code, decode_file_header, data=data, file_size=file_size)

    def test_header_invalid_api_arguments(self):
        from minidb.storage.page import FileHeader, encode_file_header, decode_file_header, initial_file_header_page
        self.assert_error('INVALID_ARGUMENT', encode_file_header, header={})
        for value in (True, 1, -1, 0x100000000, '2'):
            self.assert_error('INVALID_ARGUMENT', encode_file_header, header=FileHeader(value))
        for value, code in ((True, 'PAGE_ID_INVALID'), (-1, 'PAGE_ID_INVALID'),
                            (0, 'RESERVED_PAGE'), (1, 'RESERVED_PAGE'), (2, 'PAGE_NOT_ALLOCATED')):
            self.assert_error(code, encode_file_header, header=FileHeader(2, value))
        for data in (bytearray(4096), None, 'bad'):
            self.assert_error('INVALID_ARGUMENT', decode_file_header, data=data, file_size=8192)
        for value in (True, -1, 8192.0):
            self.assert_error('INVALID_ARGUMENT', decode_file_header, data=initial_file_header_page(), file_size=value)

    def test_free_page_roundtrip(self):
        from minidb.storage.page import encode_free_page, decode_free_page, terminal_free_page
        self.assertEqual(encode_free_page(next_page_id=3), terminal_free_page())
        for successor in (3, 0xFFFFFFFF):
            data = encode_free_page(successor, next_page_id=4)
            self.assertEqual(decode_free_page(data, page_id=2, next_page_id=4), successor)
        self.assertEqual(encode_free_page(3, next_page_id=4), b'\x03\0\0\0' + bytes(4092))

    def test_free_page_corrupted_links(self):
        from minidb.storage.page import decode_free_page
        for successor in (0, 1, 2, 4):
            data = struct.pack('<I', successor) + bytes(4092)
            self.assert_error('DB_FORMAT_MISMATCH', decode_free_page, data=data, page_id=2, next_page_id=4)

    def test_free_page_corrupted_size_padding_and_id(self):
        from minidb.storage.page import terminal_free_page, decode_free_page
        data = terminal_free_page()
        self.assert_error('DB_FILE_TRUNCATED', decode_free_page, data=data[:-1], page_id=2, next_page_id=3)
        bad = bytearray(data); bad[-1] = 1
        self.assert_error('DB_FORMAT_MISMATCH', decode_free_page, data=bytes(bad), page_id=2, next_page_id=3)
        for value, code in ((True, 'PAGE_ID_INVALID'), (0xFFFFFFFF, 'PAGE_ID_INVALID'),
                            (0, 'RESERVED_PAGE'), (1, 'RESERVED_PAGE'), (3, 'PAGE_NOT_ALLOCATED')):
            self.assert_error(code, decode_free_page, data=data, page_id=value, next_page_id=3)

    def test_free_page_encode_rejects_invalid_link(self):
        from minidb.storage.page import encode_free_page
        for value, code in ((False, 'PAGE_ID_INVALID'), (-1, 'PAGE_ID_INVALID'),
                            (0x100000000, 'PAGE_ID_INVALID'), (0, 'RESERVED_PAGE'),
                            (1, 'RESERVED_PAGE'), (3, 'PAGE_NOT_ALLOCATED')):
            self.assert_error(code, encode_free_page, next_free_page_id=value, next_page_id=3)




class FileManagerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'nested' / 'test.db'

    def open_db(self):
        from minidb.storage.file_manager import FileManager
        fm = FileManager.open(str(self.path))
        self.addCleanup(fm.close)
        return fm

    def assert_code(self, code, call, *args, **kwargs):
        from minidb.storage._errors import DbError, ErrorStage
        with self.assertRaises(DbError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertIs(caught.exception.stage, ErrorStage.STORAGE)
        json.dumps(caught.exception.context, allow_nan=False)
        return caught.exception

    def fixture(self, pages, free_head=0xFFFFFFFF):
        from minidb.storage.page import FileHeader, encode_file_header
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(encode_file_header(FileHeader(len(pages)+1, free_head)) + b''.join(pages))

    def test_create_write_sync_reopen(self):
        from minidb.storage.page import initial_file_header_page
        fm = self.open_db()
        self.assertTrue(fm.is_new)
        self.assertEqual(self.path.stat().st_size, 8192)
        self.assertEqual(fm.read_page(0), initial_file_header_page())
        self.assertEqual(fm.read_page(1), bytes(4096))
        data = bytes(range(256)) * 16
        fm.write_page(1, data)
        self.assertEqual(fm.read_page(1), data)
        fm.sync(); fm.close()
        again = self.open_db()
        self.assertFalse(again.is_new)
        self.assertEqual(again.read_page(1), data)

    def test_existing_bad_files_are_not_overwritten(self):
        from minidb.storage.file_manager import FileManager
        from minidb.storage.page import initial_file_header_page
        self.path.parent.mkdir(parents=True)
        for data, code in ((b'', 'DB_FILE_TRUNCATED'), (b'x'*20, 'DB_FILE_TRUNCATED'),
                           (b'x'*8192, 'DB_FORMAT_MISMATCH'),
                           (initial_file_header_page()+bytes(4095), 'DB_FILE_TRUNCATED'),
                           (initial_file_header_page()+bytes(4097), 'DB_FORMAT_MISMATCH')):
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                self.assert_code(code, FileManager.open, str(self.path))
                self.assertEqual(self.path.read_bytes(), data)

    def test_open_directory_and_invalid_path(self):
        from minidb.storage.file_manager import FileManager
        self.assert_code('IO_OPEN_FAILED', FileManager.open, self.temp.name)
        for value in ('', '\x00', None):
            self.assert_code('INVALID_ARGUMENT', FileManager.open, value)

    def test_read_write_invalid_arguments_leave_file_unchanged(self):
        fm = self.open_db()
        before = self.path.read_bytes()
        for value in (-1, True, 0xFFFFFFFF, '1'):
            self.assert_code('PAGE_ID_INVALID', fm.read_page, value)
        self.assert_code('PAGE_NOT_ALLOCATED', fm.read_page, 2)
        self.assert_code('RESERVED_PAGE', fm.write_page, 0, bytes(4096))
        for data in (bytes(4095), bytes(4097), bytearray(4096), None):
            self.assert_code('INVALID_ARGUMENT', fm.write_page, 1, data)
        self.assertEqual(self.path.read_bytes(), before)

    def test_validate_is_readonly_and_flags_are_strict(self):
        from unittest.mock import patch
        fm = self.open_db()
        with patch.object(fm, '_read_raw', side_effect=AssertionError('unexpected I/O')):
            fm.validate_page_id(1)
            fm.validate_page_id(0, allow_header=True)
            self.assert_code('RESERVED_PAGE', fm.validate_page_id, 0)
            self.assert_code('RESERVED_PAGE', fm.validate_page_id, 1, for_release=True)
            self.assert_code('INVALID_ARGUMENT', fm.validate_page_id, 1, allow_header=1)
            self.assert_code('INVALID_ARGUMENT', fm.validate_page_id, 1, allow_header=True, for_release=True)

    def test_rebuild_free_set_from_valid_chain(self):
        from minidb.storage.page import encode_free_page
        self.fixture([bytes(4096), encode_free_page(3, next_page_id=5),
                      encode_free_page(next_page_id=5), b'X'*4096], free_head=2)
        fm = self.open_db()
        self.assertEqual(fm.read_page(4), b'X'*4096)
        for page_id in (2, 3):
            self.assert_code('PAGE_NOT_ALLOCATED', fm.read_page, page_id)
            self.assert_code('PAGE_ALREADY_FREE', fm.validate_page_id, page_id, for_release=True)
            self.assert_code('PAGE_NOT_ALLOCATED', fm.write_page, page_id, bytes(4096))

    def test_reject_multi_page_free_cycle(self):
        from minidb.storage.page import encode_free_page
        from minidb.storage.file_manager import FileManager
        self.fixture([bytes(4096), encode_free_page(3, next_page_id=4),
                      encode_free_page(2, next_page_id=4)], free_head=2)
        before = self.path.read_bytes()
        self.assert_code('DB_FORMAT_MISMATCH', FileManager.open, str(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_closed_operations_and_repeated_close(self):
        fm = self.open_db(); fm.close(); fm.close()
        for call, args in ((fm.read_page, (1,)), (fm.write_page, (1, bytes(4096))),
                           (fm.sync, ()), (fm.validate_page_id, (1,))):
            self.assert_code('CLOSED', call, *args)

    def test_io_read_write_and_sync_failures(self):
        from unittest.mock import Mock, patch
        fm = self.open_db()
        for method, code, args in (('read', 'IO_READ_FAILED', (1,)),
                                    ('write', 'IO_WRITE_FAILED', (1, bytes(4096)))):
            fake = Mock()
            getattr(fake, method).side_effect = OSError('injected')
            with patch.object(fm, '_handle', fake):
                call = fm.read_page if method == 'read' else fm.write_page
                error = self.assert_code(code, call, *args)
                self.assertIsInstance(error.__cause__, OSError)
        with patch('minidb.storage.file_manager.os.fsync', side_effect=OSError('sync failure')):
            self.assert_code('IO_SYNC_FAILED', fm.sync)

    def test_short_read_and_short_write(self):
        from unittest.mock import Mock, patch
        fm = self.open_db()
        fake = Mock(); fake.read.return_value = bytes(4095)
        with patch.object(fm, '_handle', fake):
            self.assert_code('DB_FILE_TRUNCATED', fm.read_page, 1)
        fake = Mock(); fake.write.side_effect = [100, 0]
        with patch.object(fm, '_handle', fake):
            error = self.assert_code('IO_WRITE_FAILED', fm.write_page, 1, b'A'*4096)
            self.assertEqual(error.context['actual'], 100)
        fake = Mock(); fake.write.side_effect = [100, 3996]
        with patch.object(fm, '_handle', fake):
            fm.write_page(1, b'A'*4096)
            self.assertEqual(len(fake.write.call_args_list[1].args[0]), 3996)

    def test_close_failure_can_be_retried_without_sync(self):
        from unittest.mock import Mock, patch
        fm = self.open_db()
        fake = Mock(); fake.close.side_effect = OSError('close failure')
        with patch.object(fm, '_handle', fake):
            self.assert_code('IO_CLOSE_FAILED', fm.close)
            self.assertFalse(fm._closed)
            fake.flush.assert_not_called()
        fm.close()

    def test_open_failure_preserves_cleanup_error(self):
        from unittest.mock import Mock, patch
        from minidb.storage.file_manager import FileManager
        fake = Mock(); fake.read.return_value = b''
        fake.close.side_effect = OSError('close failure')
        with patch('builtins.open', return_value=fake):
            error = self.assert_code('DB_FILE_TRUNCATED', FileManager.open, str(self.path))
        self.assertEqual(error.context['cleanup_errors'][0]['code'], 'IO_CLOSE_FAILED')

    def test_read_after_external_truncation(self):
        fm = self.open_db()
        # Simulate a damaged file using a separate handle to our temporary file.
        with self.path.open('r+b') as stream:
            stream.truncate(4097)
        self.assert_code('DB_FILE_TRUNCATED', fm.read_page, 1)


if __name__ == '__main__':
    unittest.main()
