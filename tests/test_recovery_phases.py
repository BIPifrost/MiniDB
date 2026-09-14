"""R04/R08：真实进程终止，每个策略每个故障点重复三次。"""
import hashlib
import json
import os
import platform
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import UUID

from minidb.storage import page_v2, snapshot_journal as journal
from minidb.storage.data_page import DataPage

ROOT = Path(__file__).resolve().parents[1]
DB = UUID('00112233-4455-4677-8899-aabbccddeeff')
ORIGINAL = (page_v2.encode_file_header(page_v2.FileHeaderV2(DB, 20, 19))
            + DataPage.empty(0, page_id=1, version=2).to_bytes()
            + DataPage.empty(0xFFFFFFFE, page_id=2, version=2).to_bytes()
            + b'A'*(16*4096)
            + page_v2.encode_free_page(next_page_id=20))


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RecoveryPhaseTests(unittest.TestCase):
    def command(self, path, phase, policy):
        return [sys.executable, '-B', '-m', 'tests.fakes.recovery_phases', str(path), phase, policy]

    def terminate_at_phase(self, path, case, policy, expected):
        process = subprocess.Popen(self.command(path, case, policy), cwd=ROOT,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8')
        try:
            messages = queue.Queue()
            reader = threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True)
            reader.start()
            try:
                line = messages.get(timeout=15)
            except queue.Empty:
                self.fail(f'{case}没有在15秒内通知阶段')
            self.assertTrue(line, f'{case}进程提前退出')
            event = json.loads(line)
            self.assertEqual(event['phase'], expected)
            self.assertFalse(event['committed'])
            self.assertEqual(event['tail_length'], 0)
            process.kill()
            process.wait(timeout=15)
            self.assertNotEqual(process.returncode, 0)
            event['exit_code'] = process.returncode
            reader.join(timeout=1)
            return event
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def recover(self, path, policy):
        result = subprocess.run(self.command(path, 'recover', policy), cwd=ROOT,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        report['exit_code'] = result.returncode
        return report

    def run_case(self, case):
        evidence = []
        for policy in ('lru', 'fifo'):
            for repeat in range(1, 4):
                with self.subTest(policy=policy, repeat=repeat), tempfile.TemporaryDirectory() as temp:
                    path = Path(temp)/'phase.db'
                    path.write_bytes(ORIGINAL)
                    writer = self.terminate_at_phase(path, 'R04', policy, 'MAIN_SYNCED_BEFORE_COMMIT_TAIL')
                    changed = path.read_bytes()
                    self.assertEqual(writer['sha256'], digest(changed))
                    self.assertEqual(len(changed), 21*4096)
                    self.assertEqual(changed[3*4096:5*4096], b'B'*(2*4096))
                    formal = Path(str(path)+'.mdb2-journal')
                    log_bytes = formal.read_bytes()
                    with formal.open('rb') as stream:
                        inspection = journal.inspect_stream(stream)
                    self.assertFalse(inspection.committed)
                    self.assertEqual(inspection.snapshot.payload_sha256, hashlib.sha256(ORIGINAL).digest())
                    partial = None
                    if case == 'R08':
                        partial = self.terminate_at_phase(path, 'R08', policy, 'RESTORE_PARTIALLY_WRITTEN')
                        mixed = path.read_bytes()
                        n = 3*4096+17
                        self.assertEqual(partial['written_bytes'], n)
                        self.assertEqual(mixed, ORIGINAL[:n]+changed[n:])
                        self.assertNotEqual(mixed, ORIGINAL)
                        self.assertNotEqual(mixed, changed)
                        self.assertEqual(partial['sha256'], digest(mixed))
                        self.assertEqual(formal.read_bytes(), log_bytes)
                    restored = self.recover(path, policy)
                    self.assertEqual(restored['action'], 'ROLLED_BACK')
                    self.assertEqual(restored['restored_bytes'], len(ORIGINAL))
                    self.assertEqual(restored['sha256'], digest(ORIGINAL))
                    self.assertEqual(restored['length'], len(ORIGINAL))
                    self.assertEqual(restored['free_pages'], [19])
                    self.assertEqual(restored['next_page_id'], 20)
                    self.assertEqual(path.read_bytes(), ORIGINAL)
                    self.assertFalse(formal.exists())
                    self.assertFalse(Path(str(formal)+'.tmp').exists())
                    again = self.recover(path, policy)
                    self.assertEqual(again['action'], 'NONE')
                    self.assertEqual(again['sha256'], digest(ORIGINAL))
                    evidence.append({'case_id': case, 'seed': 'fixed-bytes', 'policy': policy,
                                     'capacity': 1, 'repeat': repeat, 'original_sha256': digest(ORIGINAL),
                                     'original_length': len(ORIGINAL), 'journal_sha256': digest(log_bytes),
                                     'writer': writer, 'partial_restore': partial, 'recovered': restored,
                                     'second_reopen': again, 'journal_after': 'absent',
                                     'logical_rows': '不适用：物理镜像测试', 'passed': True})
        self.assertEqual(len(evidence), 6)
        output = os.environ.get('MINIDB_RECOVERY_EVIDENCE_DIR')
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            payload = {'platform': platform.platform(), 'python': sys.version,
                       'source_revision': os.environ.get('MINIDB_TEST_REVISION', 'unspecified'),
                       'cases': evidence}
            (directory/(case+'.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_r04_main_synced_without_commit_tail_restores_old_image(self):
        self.run_case('R04')

    def test_r08_killed_partial_restore_can_restart(self):
        self.run_case('R08')
