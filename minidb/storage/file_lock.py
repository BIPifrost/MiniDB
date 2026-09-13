"""Windows 内核独占句柄；路径锁载体可残留，文件存在不代表持锁。

CreateFileW 的 share mode=0 拒绝再次读、写或删除打开。主库句柄直接
交给 FileManager 使用，内核按实际文件识别硬链接。跨平台实现后置。
"""
import os
from pathlib import Path
from typing import BinaryIO

from minidb.core import errors


def _open_exclusive(path: str) -> tuple[BinaryIO, bool]:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # OPEN_ALWAYS 不截断旧文件；GetLastError 区分已有文件与新建文件。
    handle = create(path, 0x80000000 | 0x40000000, 0, None, 4, 0x80, None)
    result = ctypes.get_last_error()
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(result)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    # open_osfhandle 接管原 HANDLE；后续只通过 fd/文件对象关闭。
    try:
        os.set_inheritable(fd, False)
        stream = os.fdopen(fd, 'r+b', buffering=0)
    except BaseException:
        os.close(fd)
        raise
    return stream, result != 183


class DatabaseLock:
    def __init__(self, path: str, path_handle: BinaryIO,
                 handle: BinaryIO, is_new: bool) -> None:
        self.path = path
        self.path_handle = path_handle
        self.handle = handle
        self.is_new = is_new
        stat = os.fstat(handle.fileno())
        self.lock_identity = f'{stat.st_dev}:{stat.st_ino}'

    @classmethod
    def acquire(cls, path: str) -> 'DatabaseLock':
        if type(path) is not str or not path or '\x00' in path:
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.INVALID_ARGUMENT,
                                 '数据库路径必须为非空字符串', context={'operation': 'lock'})
        if os.name != 'nt':
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.IO_OPEN_FAILED,
                                 '当前独占文件实现仅支持 Windows',
                                 context={'path': path, 'operation': 'lock', 'platform': os.name})
        actual = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        carrier = actual + '.mdb2-lock'
        path_handle = handle = None
        locking = carrier
        try:
            Path(actual).parent.mkdir(parents=True, exist_ok=True)
            path_handle, _ = _open_exclusive(carrier)
            locking = actual
            handle, is_new = _open_exclusive(actual)
            return cls(actual, path_handle, handle, is_new)
        except OSError as exc:
            code = errors.DATABASE_BUSY if getattr(exc, 'winerror', None) in (32, 33) else errors.IO_OPEN_FAILED
            original = errors.DbError(errors.ErrorStage.STORAGE, code,
                                      '数据库正在被占用' if code == errors.DATABASE_BUSY else '无法打开数据库独占句柄',
                                      context={'path': actual, 'operation': 'lock',
                                               'lock_identity': locking, 'cause': str(exc)})
            for stream in (handle, path_handle):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError as cleanup:
                        original._update_context(cleanup_errors=[*original.context.get('cleanup_errors', ()),
                            {'stage': 'STORAGE', 'code': errors.IO_CLOSE_FAILED, 'message': str(cleanup)}])
            raise original from exc
        except BaseException:
            for stream in (handle, path_handle):
                if stream is not None:
                    stream.close()
            raise

    def close(self) -> None:
        # 先关主库，再放开路径锁；主库关闭失败时保留路径锁以便重试。
        self.handle.close()
        self.path_handle.close()
