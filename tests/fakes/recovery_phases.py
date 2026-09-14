"""R04/R08子进程故障钩子，仅用于测试，不改变生产恢复逻辑。"""
import hashlib
import json
import os
import sys
from uuid import UUID

from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.file_manager import FileManager
from minidb.storage.buffer_pool import BufferPool
from minidb.storage import recovery, file_snapshot, snapshot_journal as journal


def image(handle):
    position = handle.tell()
    handle.seek(0)
    data = handle.read()
    handle.seek(position)
    return {'sha256': hashlib.sha256(data).hexdigest(), 'length': len(data)}


def pause(phase, lock, **extra):
    info = journal.inspect_journal(lock)
    print(json.dumps({'phase': phase, **image(lock.handle),
                      'committed': info.committed, 'tail_length': info.tail_length,
                      **extra}), flush=True)
    # 父进程必须收到精确phase后kill，不依赖sleep，也不由子进程自行退出。
    sys.stdin.readline()
    raise AssertionError('父进程必须终止故障进程，不能放行')


def main():
    path, phase, policy = sys.argv[1:]
    lock = DatabaseLock.acquire(path)
    if phase == 'R04':
        guard = TransactionGuard(S.PREPARING)
        fm = FileManager.open_locked(path, lock, guard=guard)
        pool = BufferPool(fm, capacity=1, policy=policy)
        txn = UUID('10213243-5465-4787-98a9-bacbdcedfe0f')
        journal.prepare(fm, txn)
        guard.transition_to(S.ACTIVE)
        for page in (3, 4):
            snapshot = pool.get_snapshot(page)
            pool.write_if_current(snapshot, b'B'*4096)
        pool.new_page()  # 复用空闲页。
        pool.new_page()  # 扩展文件，改变文件头和长度。
        pool.free_page(18)  # 改变空闲链。
        guard.transition_to(S.COMMITTING)
        pool.flush_all()
        real_sync = fm.sync
        def after_sync():
            real_sync()
            pause('MAIN_SYNCED_BEFORE_COMMIT_TAIL', lock)
        fm.sync = after_sync
        journal.mark_committed(fm, txn)
        raise AssertionError('未停在指定提交阶段')
    if phase == 'R08':
        real_write = file_snapshot.write_all
        def partial_write(destination, data):
            if destination is lock.handle:
                prefix = data[:3*4096+17]
                real_write(destination, prefix)
                destination.flush()
                os.fsync(destination.fileno())
                pause('RESTORE_PARTIALLY_WRITTEN', lock, written_bytes=len(prefix))
            return real_write(destination, data)
        file_snapshot.write_all = partial_write
        recovery.RecoveryManager.inspect_and_recover(path, lock)
        raise AssertionError('未停在指定恢复阶段')
    if phase == 'recover':
        report = recovery.RecoveryManager.inspect_and_recover(path, lock)
        result = {'action': report.action, 'restored_bytes': report.restored_bytes,
                  **image(lock.handle)}
        fm = FileManager.open_locked(path, lock)
        result['free_pages'] = sorted(fm._free_pages)
        result['next_page_id'] = fm._header.next_page_id
        fm.close()
        print(json.dumps(result), flush=True)
        return
    raise AssertionError(phase)


if __name__ == '__main__':
    main()
