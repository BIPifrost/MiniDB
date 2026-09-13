"""基于数据页的表记录存储实现。

StorageEngine 只组合三个已经约定好的部件：RowCodec 负责 Row 与 bytes，
DataPage 负责一张页内的记录，BufferPool 负责完整页面的缓存和持久化。
本模块不解析 SQL、不查询 CatalogManager，也不实现缓存替换算法。

测试可显式注入内存 BufferPool；正式入口接入真实 BufferPool 时不需要
修改这里的页链、扫描、删除和回收算法。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from minidb.core import errors
from minidb.core.disk_types import (
    CATALOG_ROOT_PAGE_ID,
    FORMAT_VERSION,
    INVALID_PAGE_ID,
    MAX_PAGE_ID,
    PAGE_SIZE,
)
from minidb.core.records import Row, RowId, RowScan, StoredRow
from minidb.core.schema import TableDef
from minidb.storage.data_page import MAX_RECORD_SIZE, DataPage, SlotState


class _FileManagerLike(Protocol):
    """StorageEngine 真正使用的 FileManager 最小形状。"""

    @property
    def is_new(self) -> bool: ...

    def sync(self) -> None: ...

    def close(self) -> None: ...


class _BufferPoolLike(Protocol):
    """只描述调用边界，不复制或替代队友维护的公开 BufferPool 接口。"""

    @property
    def file_manager(self) -> _FileManagerLike: ...

    def new_page(self) -> int: ...

    def get_page(self, page_id: int) -> bytes: ...

    def write_page(self, page_id: int, data: bytes) -> None: ...

    def free_page(self, page_id: int) -> None: ...

    def flush_all(self) -> None: ...


class _RowCodecLike(Protocol):
    def encoded_size(self, row: Row, schema) -> int: ...

    def encode(self, row: Row, schema) -> bytes: ...

    def decode(self, data: bytes, schema) -> Row: ...


class _PageRowScan:
    """逐页、逐槽读取的可关闭扫描器。

    扫描不一次性复制整张表。正常读完、显式 close 或迭代异常都会通知
    StorageEngine 注销活动扫描，防止后续写操作被永久阻塞。
    """

    def __init__(
        self,
        engine: "StorageEngine",
        table: TableDef,
        root_page: DataPage,
    ) -> None:
        self._engine = engine
        self._table = table
        # scan_rows 已经读取并校验过根页。直接接管这个快照，避免真正开始
        # 迭代时再次请求同一页，造成 BufferPool 命中统计和日志多记一次。
        self._next_page_id = root_page.header.next_page_id
        self._page: DataPage | None = root_page
        self._slot_id = 0
        self._visited: set[int] = {table.ref.root_page_id}
        self._closed = False

    def __iter__(self) -> "_PageRowScan":
        return self

    def __next__(self) -> StoredRow:
        if self._closed:
            raise StopIteration
        try:
            while True:
                if self._page is None:
                    if self._next_page_id == INVALID_PAGE_ID:
                        self.close()
                        raise StopIteration
                    page_id = self._next_page_id
                    self._page = self._engine._read_table_page(
                        self._table, page_id, self._visited
                    )
                    self._visited.add(page_id)
                    self._next_page_id = self._page.header.next_page_id
                    self._slot_id = 0

                while self._slot_id < self._page.header.slot_count:
                    slot_id = self._slot_id
                    self._slot_id += 1
                    slot = self._page.slots[slot_id]
                    record = self._page.record(slot_id)
                    if record is None:
                        continue
                    row = self._engine._codec.decode(record, self._table.schema)
                    return StoredRow(
                        RowId(self._page.page_id, slot_id, slot.generation), row
                    )

                # 当前页已经读完。清掉页面对象后，下一轮才加载后继页，
                # 因而不会把多张页面副本长期留在扫描器里。
                self._page = None
        except StopIteration:
            raise
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._page = None
        self._engine._scan_closed(self)


class StorageEngine:
    """表记录存储的正式实现和唯一公开接口。"""

    def __init__(
        self,
        buffer_pool: _BufferPoolLike,
        row_codec: _RowCodecLike,
        file_manager: _FileManagerLike,
    ) -> None:
        """装配同一会话的缓存、编码器和文件管理器。

        构造阶段只验证依赖形状和对象身份，不读取或修改任何页面。
        """
        _require_methods(
            buffer_pool,
            "buffer_pool",
            ("new_page", "get_page", "write_page", "free_page", "flush_all"),
        )
        _require_methods(
            row_codec,
            "row_codec",
            ("encoded_size", "encode", "decode"),
        )
        _require_methods(file_manager, "file_manager", ("sync", "close"))
        if getattr(buffer_pool, "file_manager", None) is not file_manager:
            _error(
                errors.INVALID_ARGUMENT,
                "BufferPool 与 StorageEngine 必须使用同一个 FileManager",
                "StorageEngine.__init__",
                field="file_manager",
                expected="buffer_pool.file_manager 与传入对象身份相同",
                actual="different object",
            )

        self._buffer = buffer_pool
        self._codec = row_codec
        self._file_manager = file_manager
        self._active_scans: set[_PageRowScan] = set()
        self._closed = False
        self._failed = False

    @property
    def active_scan_count(self) -> int:
        """返回当前尚未关闭的扫描数，供生命周期检查和诊断使用。"""
        return len(self._active_scans)

    @property
    def is_closed(self) -> bool:
        return self._closed

    def create_heap(self, table_id: int) -> int:
        """分配并初始化用户表根页，返回稳定的根页号。"""
        self._require_mutable("create_heap")
        _validate_table_id(table_id, allow_catalog=False, operation="create_heap")
        page_id = self._buffer.new_page()
        _validate_allocated_page_id(page_id, "create_heap")
        # new_page 只承诺全零和已分配；合法的数据页头由本层写入。
        page = DataPage.empty(table_id, page_id=page_id)
        self._buffer.write_page(page_id, page.to_bytes())
        return page_id

    def initialize_reserved_heap(self, table: TableDef) -> None:
        """把全新文件中预留的 page 1 初始化为系统目录根页。"""
        self._require_mutable("initialize_reserved_heap")
        formal = _validate_table(table, "initialize_reserved_heap")
        if (
            formal.ref.table_id != 0
            or formal.ref.root_page_id != CATALOG_ROOT_PAGE_ID
        ):
            _error(
                errors.INVALID_ARGUMENT,
                "系统目录必须固定使用 table 0 和 page 1",
                "initialize_reserved_heap",
                table_id=formal.ref.table_id,
                root_page_id=formal.ref.root_page_id,
            )
        if getattr(self._file_manager, "is_new", None) is not True:
            _error(
                errors.INVALID_ARGUMENT,
                "只有全新数据库文件可以初始化系统目录根页",
                "initialize_reserved_heap",
                field="file_manager.is_new",
                expected=True,
                actual=repr(getattr(self._file_manager, "is_new", None)),
            )

        reserved = self._buffer.get_page(CATALOG_ROOT_PAGE_ID)
        if type(reserved) is not bytes or reserved != bytes(PAGE_SIZE):
            _error(
                errors.PAGE_CORRUPTED,
                "系统目录预留页不是完整全零页",
                "initialize_reserved_heap",
                page_id=CATALOG_ROOT_PAGE_ID,
                field="reserved_page",
                expected="4096 zero bytes",
                actual=(
                    type(reserved).__name__
                    if type(reserved) is not bytes
                    else f"{len(reserved)} bytes with nonzero data"
                ),
            )
        page = DataPage.empty(0, page_id=CATALOG_ROOT_PAGE_ID)
        self._buffer.write_page(CATALOG_ROOT_PAGE_ID, page.to_bytes())

    def validate_table_root(self, table: TableDef) -> None:
        """读取真实根页，确认格式和 table_id 与 TableDef 一致。"""
        self._require_open("validate_table_root")
        formal = _validate_table(table, "validate_table_root")
        self._read_table_page(formal, formal.ref.root_page_id)

    def insert_row(self, table: TableDef, row: Row) -> RowId:
        """编码 Row，并按页链顺序首次适配插入。"""
        self._require_mutable("insert_row")
        formal = _validate_table(table, "insert_row")

        # 所有可预先发现的行错误都必须发生在任何页面修改之前。
        encoded_size = self._codec.encoded_size(row, formal.schema)
        encoded = self._codec.encode(row, formal.schema)
        if (
            type(encoded_size) is not int
            or type(encoded) is not bytes
            or encoded_size != len(encoded)
        ):
            _error(
                errors.ROW_ENCODING_ERROR,
                "RowCodec 的长度结果与实际编码不一致",
                "insert_row",
                encoded_size=repr(encoded_size),
                actual_size=(len(encoded) if type(encoded) is bytes else -1),
            )
        if not 1 <= encoded_size <= MAX_RECORD_SIZE:
            _error(
                errors.ROW_TOO_LARGE,
                "编码后的记录无法放入单个数据页",
                "insert_row",
                encoded_size=encoded_size,
                max_size=MAX_RECORD_SIZE,
            )

        tail_id = formal.ref.root_page_id
        for page_id, page in self._walk_pages(formal):
            tail_id = page_id
            row_slot = page.insert(encoded)
            if row_slot is not None:
                self._buffer.write_page(page_id, page.to_bytes())
                return RowId(page_id, row_slot.slot_id, row_slot.generation)

        # 所有旧页都放不下时才扩页。先写好新页，再把旧尾页连向它；
        # 这样不会让表链暂时指向一张全零、尚无合法页头的页面。
        new_page_id = self._buffer.new_page()
        _validate_allocated_page_id(new_page_id, "insert_row")
        new_page = DataPage.empty(formal.ref.table_id, page_id=new_page_id)
        row_slot = new_page.insert(encoded)
        if row_slot is None:  # 前面的 MAX_RECORD_SIZE 校验保证理论上不会发生。
            _error(
                errors.ROW_ENCODING_ERROR,
                "通过大小校验的记录仍无法写入空页",
                "insert_row",
                encoded_size=encoded_size,
            )
        self._buffer.write_page(new_page_id, new_page.to_bytes())

        # 重新获取尾页的最新副本，避免用遍历时保留的旧副本覆盖其他字段。
        tail = self._read_table_page(formal, tail_id)
        if tail.header.next_page_id != INVALID_PAGE_ID:
            _error(
                errors.PAGE_CORRUPTED,
                "扩页前发现尾页已经存在后继",
                "insert_row",
                page_id=tail_id,
                next_page_id=tail.header.next_page_id,
            )
        tail.set_next_page_id(new_page_id)
        self._buffer.write_page(tail_id, tail.to_bytes())
        return RowId(new_page_id, row_slot.slot_id, row_slot.generation)

    def scan_rows(self, table: TableDef) -> RowScan:
        """创建逐页流式扫描；调用方必须读完或显式 close。"""
        self._require_open("scan_rows")
        formal = _validate_table(table, "scan_rows")
        # 在登记扫描前先验证根页，避免一个根本无法启动的扫描占用名额。
        root_page = self._read_table_page(formal, formal.ref.root_page_id)
        scan = _PageRowScan(self, formal, root_page)
        self._active_scans.add(scan)
        return scan

    def fetch_row(self, table: TableDef, row_id: RowId) -> StoredRow:
        """按完整 RowId 读取一行，并拒绝过期或不属于目标表的位置。

        这是 UPDATE、索引回表和 RowMovement 校验共用的定点读取入口。它不
        注册活动扫描，也不缓存可变页面副本；generation 校验始终由 DataPage
        执行，因而删除槽被复用后旧 RowId 不会命中新记录。
        """
        self._require_open("fetch_row")
        formal = _validate_table(table, "fetch_row")
        if not isinstance(row_id, RowId):
            _error(
                errors.INVALID_ARGUMENT,
                "row_id 必须是 RowId",
                "fetch_row",
                field="row_id",
                expected="RowId",
                actual=type(row_id).__name__,
            )

        for page_id, page in self._walk_pages(formal):
            if page_id != row_id.page_id:
                continue
            record = page.record(row_id.slot_id, row_id.generation)
            if record is None:
                _error(
                    errors.STALE_ROW,
                    "RowId 指向的槽已删除或不再有效",
                    "fetch_row",
                    page_id=row_id.page_id,
                    slot_id=row_id.slot_id,
                    generation=row_id.generation,
                )
            return StoredRow(row_id, self._codec.decode(record, formal.schema))

        _error(
            errors.INVALID_ARGUMENT,
            "RowId 指向的页不属于目标表",
            "fetch_row",
            table_id=formal.ref.table_id,
            page_id=row_id.page_id,
            slot_id=row_id.slot_id,
        )

    def delete_row(self, table: TableDef, row_id: RowId) -> bool:
        """确认 RowId 位于目标表页链后，将有效槽标记为删除。"""
        self._require_mutable("delete_row")
        formal = _validate_table(table, "delete_row")
        if not isinstance(row_id, RowId):
            _error(
                errors.INVALID_ARGUMENT,
                "row_id 必须是 RowId",
                "delete_row",
                field="row_id",
                expected="RowId",
                actual=type(row_id).__name__,
            )

        for page_id, page in self._walk_pages(formal):
            if page_id != row_id.page_id:
                continue
            changed = page.delete(row_id.slot_id, row_id.generation)
            if changed:
                self._buffer.write_page(page_id, page.to_bytes())
            return changed
        _error(
            errors.INVALID_ARGUMENT,
            "RowId 指向的页不属于目标表",
            "delete_row",
            table_id=formal.ref.table_id,
            page_id=row_id.page_id,
            slot_id=row_id.slot_id,
        )

    def reclaim_empty_pages(self, table: TableDef) -> int:
        """摘除并释放空的非根页；根页只清空记录区域。"""
        self._require_mutable("reclaim_empty_pages")
        formal = _validate_table(table, "reclaim_empty_pages")
        root_id = formal.ref.root_page_id
        root = self._read_table_page(formal, root_id)

        if root.header.live_count == 0 and root.header.slot_count:
            root.reset_records()
            self._buffer.write_page(root_id, root.to_bytes())

        released = 0
        visited = {root_id}
        previous_id = root_id
        current_id = root.header.next_page_id
        while current_id != INVALID_PAGE_ID:
            current = self._read_table_page(formal, current_id, visited)
            visited.add(current_id)
            successor = current.header.next_page_id
            if current.header.live_count > 0:
                previous_id = current_id
                current_id = successor
                continue

            # 必须先让表链绕过当前页，再释放物理页。连续空页时，
            # previous_id 可能保持不变，因此每次都重新读取最新前驱页。
            previous = self._read_table_page(formal, previous_id)
            if previous.header.next_page_id != current_id:
                _error(
                    errors.PAGE_CORRUPTED,
                    "回收前发现页链前驱关系不一致",
                    "reclaim_empty_pages",
                    page_id=previous_id,
                    expected=current_id,
                    actual=previous.header.next_page_id,
                )
            previous.set_next_page_id(successor)
            self._buffer.write_page(previous_id, previous.to_bytes())
            self._buffer.free_page(current_id)
            released += 1
            current_id = successor
        return released

    def sync(self) -> None:
        """先写出所有脏页，再要求文件层完成持久化同步。"""
        self._require_open("sync")
        self._buffer.flush_all()
        self._file_manager.sync()

    def close(self) -> None:
        """正常关闭：拒绝活动扫描，成功同步后再关闭文件。"""
        if self._closed:
            return
        self._require_mutable("close")
        self.sync()
        self._file_manager.close()
        self._closed = True

    def abort(self) -> None:
        """失败清理：关闭扫描和文件，但绝不刷新脏页。"""
        if self._closed:
            return
        # 先标记 failed，确保清理期间任何新的业务调用都会被拒绝。
        self._failed = True
        for scan in tuple(self._active_scans):
            scan.close()
        self._file_manager.close()
        self._closed = True

    def _walk_pages(self, table: TableDef) -> Iterator[tuple[int, DataPage]]:
        """从根页遍历整条链，同时检测循环和跨表页面。"""
        current_id = table.ref.root_page_id
        visited: set[int] = set()
        while current_id != INVALID_PAGE_ID:
            page = self._read_table_page(table, current_id, visited)
            visited.add(current_id)
            yield current_id, page
            current_id = page.header.next_page_id

    def _read_table_page(
        self,
        table: TableDef,
        page_id: int,
        visited: set[int] | None = None,
    ) -> DataPage:
        """统一读取并校验一张表页，给错误补充物理 page_id。"""
        if visited is not None and page_id in visited:
            _error(
                errors.PAGE_CORRUPTED,
                "表数据页链存在循环",
                "scan_table_pages",
                table_id=table.ref.table_id,
                page_id=page_id,
            )
        data = self._buffer.get_page(page_id)
        page = DataPage(
            data,
            page_id=page_id,
            expected_table_id=table.ref.table_id,
        )
        if page.header.version != FORMAT_VERSION:
            _error(
                errors.PAGE_CORRUPTED,
                "数据页版本与数据库文件格式不一致",
                "read_table_page",
                page_id=page_id,
                expected_version=FORMAT_VERSION,
                actual_version=page.header.version,
            )
        return page

    def _scan_closed(self, scan: _PageRowScan) -> None:
        self._active_scans.discard(scan)

    def _require_open(self, operation: str) -> None:
        if self._closed or self._failed:
            _error(
                errors.CLOSED,
                "StorageEngine 已关闭或处于失败状态",
                operation,
                resource="StorageEngine",
                state="closed" if self._closed else "failed",
            )

    def _require_mutable(self, operation: str) -> None:
        self._require_open(operation)
        if self._active_scans:
            _error(
                errors.ACTIVE_SCAN,
                "存在活动扫描时不能修改或正常关闭存储",
                operation,
                active_scan_count=len(self._active_scans),
            )


def _require_methods(value: object, field: str, methods: tuple[str, ...]) -> None:
    for method in methods:
        if not callable(getattr(value, method, None)):
            _error(
                errors.INVALID_ARGUMENT,
                "StorageEngine 依赖缺少必要方法",
                "StorageEngine.__init__",
                field=field,
                expected=method,
                actual=type(value).__name__,
            )


def _validate_table(table: object, operation: str) -> TableDef:
    if not isinstance(table, TableDef):
        _error(
            errors.INVALID_ARGUMENT,
            "table 必须是正式 TableDef",
            operation,
            field="table",
            expected="TableDef",
            actual=type(table).__name__,
        )
    return table


def _validate_table_id(
    table_id: object, *, allow_catalog: bool, operation: str
) -> None:
    minimum = 0 if allow_catalog else 1
    if type(table_id) is not int or not minimum <= table_id <= MAX_PAGE_ID:
        _error(
            errors.INVALID_ARGUMENT,
            "table_id 超出支持范围",
            operation,
            field="table_id",
            expected=f"{minimum}..{MAX_PAGE_ID}",
            actual=repr(table_id),
        )


def _validate_allocated_page_id(page_id: object, operation: str) -> None:
    """复核 BufferPool.new_page 的返回值，防止覆盖文件头或目录根页。"""
    if type(page_id) is not int or not 2 <= page_id <= MAX_PAGE_ID:
        _error(
            errors.PAGE_ID_INVALID,
            "BufferPool 返回了非法的用户数据页号",
            operation,
            page_id=repr(page_id),
            expected=f"2..{MAX_PAGE_ID}",
        )


def _error(code: str, message: str, operation: str, **context: object) -> None:
    """唯一错误出口；存储层上下文只保存 JSON 可表示值。"""
    clean = {key: value for key, value in context.items() if value is not None}
    clean["operation"] = operation
    raise errors.DbError(errors.ErrorStage.STORAGE, code, message, None, clean)


__all__ = ["StorageEngine"]
