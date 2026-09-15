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
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import (
    CATALOG_ROOT_PAGE_ID,
    FORMAT_VERSION,
    INVALID_PAGE_ID,
    MAX_PAGE_ID,
    PAGE_SIZE,
    PageSnapshot,
    V2_FORMAT_VERSION,
    V2_INDEX_CATALOG_ROOT_PAGE_ID,
    V2_MAX_PAGE_COUNT,
)
from minidb.core.records import (
    Row,
    RowId,
    RowMovement,
    RowScan,
    StoredRow,
    UpdateBatch,
    ValidatedWriteToken,
)
from minidb.core.schema import SYSTEM_INDEXES_ID, TableDef
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.catalog.catalog_manager import CatalogServices
from minidb.storage.data_page import MAX_RECORD_SIZE, DataPage


MAX_BATCH_ROWS = 10_000
MAX_BATCH_BYTES = 16 * 1024 * 1024


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

    def get_snapshot(self, page_id: int) -> PageSnapshot: ...

    def write_if_current(self, snapshot: PageSnapshot, data: bytes) -> None: ...

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
        guard: TransactionGuard | None = None,
    ) -> None:
        """装配同一会话的缓存、编码器和文件管理器。

        构造阶段只验证依赖形状和对象身份，不读取或修改任何页面。
        """
        _require_methods(
            buffer_pool,
            "buffer_pool",
            ("new_page", "get_page", "write_page", "get_snapshot", "write_if_current",
             "free_page", "flush_all"),
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
        if guard is not None and not isinstance(guard, TransactionGuard):
            _error(
                errors.INVALID_ARGUMENT,
                "guard 必须是正式 TransactionGuard",
                "StorageEngine.__init__",
                field="guard",
                expected="TransactionGuard or None",
                actual=type(guard).__name__,
            )
        if guard is not None and getattr(file_manager, "_guard", None) is not guard:
            _error(
                errors.INVALID_ARGUMENT,
                "v2 StorageEngine 与 FileManager 必须共享同一个 TransactionGuard",
                "StorageEngine.__init__",
                field="guard",
                expected="file_manager._guard 与传入对象身份相同",
                actual="different object",
            )

        self._buffer = buffer_pool
        self._codec = row_codec
        self._file_manager = file_manager
        self._guard = guard
        self._data_page_version = V2_FORMAT_VERSION if guard is not None else FORMAT_VERSION
        self._first_allocatable_page_id = (
            V2_INDEX_CATALOG_ROOT_PAGE_ID + 1 if guard is not None else 2
        )
        self._catalog_services: CatalogServices | None = None
        self._write_session_id: UUID | None = None
        self._catalog_generation = None
        self._token_is_authorized = None
        self._active_scans: set[_PageRowScan] = set()
        # IndexManager registers its closeable cursors here so the transaction
        # coordinator sees one combined active-scan count before begin/commit.
        self._external_scans: set[object] = set()
        self._closed = False
        self._failed = False

    @property
    def active_scan_count(self) -> int:
        """返回当前尚未关闭的扫描数，供生命周期检查和诊断使用。"""
        return len(self._active_scans) + len(self._external_scans)

    def register_external_scan(self, scan: object) -> None:
        """Register a Session-owned index cursor in the shared scan barrier."""
        self._require_open("register_external_scan")
        close = getattr(scan, "close", None)
        if not callable(close) or scan in self._external_scans:
            _error(
                errors.INVALID_ARGUMENT,
                "外部扫描必须可关闭且不能重复登记",
                "register_external_scan",
                resource=type(scan).__name__,
            )
        self._external_scans.add(scan)

    def unregister_external_scan(self, scan: object) -> None:
        """Remove an index cursor; repeated cursor close remains harmless."""
        self._external_scans.discard(scan)

    @property
    def buffer_pool(self) -> _BufferPoolLike:
        """参与事务装配的 BufferPool；必须与构造时对象身份相同。"""
        return self._buffer

    @property
    def file_manager(self) -> _FileManagerLike:
        """参与事务装配的 FileManager；不通过此属性执行旁路写入。"""
        return self._file_manager

    @property
    def guard(self) -> TransactionGuard | None:
        """v2 会话共享的状态守卫；v1 兼容实例返回 None。"""
        return self._guard

    @property
    def catalog_services(self) -> CatalogServices:
        """返回由 Session 完整绑定的目录依赖，未绑定时明确拒绝。"""
        if self._catalog_services is None:
            raise NotImplementedError(
                "StorageEngine.catalog_services 尚未由 Session 绑定"
            )
        return self._catalog_services

    def bind_catalog_services(
        self,
        *,
        write_catalog_rows,
        validate_index_root,
    ) -> None:
        """一次性装配 Catalog 回调，不复制目录或索引判断。

        write_catalog_rows 由 Session 适配受控目录 token；validate_index_root
        由正式 IndexManager 提供。StorageEngine 只固定共享 guard、codec 和
        文件格式身份，避免 CatalogServices 指向另一套会话对象。
        """
        self._require_open("bind_catalog_services")
        if self._guard is None:
            _error(
                errors.INVALID_ARGUMENT,
                "只有 v2 StorageEngine 可以绑定 CatalogServices",
                "bind_catalog_services",
                field="guard",
                expected="TransactionGuard",
                actual=None,
            )
        if self._catalog_services is not None:
            _error(
                errors.INVALID_ARGUMENT,
                "CatalogServices 已经绑定，不能替换",
                "bind_catalog_services",
                field="catalog_services",
                expected="unbound",
                actual="bound",
            )
        for name, callback in (
            ("write_catalog_rows", write_catalog_rows),
            ("validate_index_root", validate_index_root),
        ):
            if not callable(callback):
                _error(
                    errors.INVALID_ARGUMENT,
                    "CatalogServices 回调必须可调用",
                    "bind_catalog_services",
                    field=name,
                    expected="callable",
                    actual=type(callback).__name__,
                )
        self._catalog_services = CatalogServices(
            self._guard,
            self._codec,
            lambda: V2_FORMAT_VERSION,
            write_catalog_rows,
            validate_index_root,
        )

    def bind_write_authorizer(
        self,
        *,
        session_id: UUID,
        catalog_generation,
        token_is_authorized,
    ) -> None:
        """绑定 Session 持有的 token 登记表视图，且只允许绑定一次。

        StorageEngine 不消费 token，也不维护 PreparedWrite 状态。Session 在
        apply 期间只让当前已消费的 token 通过 ``token_is_authorized``，结束
        apply 后撤销；这里复核对象身份结果和 token 的两个公开绑定字段。
        """
        self._require_open("bind_write_authorizer")
        if self._guard is None:
            _error(
                errors.INVALID_ARGUMENT,
                "只有 v2 StorageEngine 可以绑定写授权器",
                "bind_write_authorizer",
                field="guard",
            )
        if self._write_session_id is not None:
            _error(
                errors.INVALID_ARGUMENT,
                "写授权器已经绑定，不能替换",
                "bind_write_authorizer",
                field="write_authorizer",
                expected="unbound",
                actual="bound",
            )
        if not isinstance(session_id, UUID):
            _error(
                errors.INVALID_ARGUMENT,
                "session_id 必须是 UUID",
                "bind_write_authorizer",
                field="session_id",
                actual=type(session_id).__name__,
            )
        for name, callback in (
            ("catalog_generation", catalog_generation),
            ("token_is_authorized", token_is_authorized),
        ):
            if not callable(callback):
                _error(
                    errors.INVALID_ARGUMENT,
                    "写授权器回调必须可调用",
                    "bind_write_authorizer",
                    field=name,
                    expected="callable",
                    actual=type(callback).__name__,
                )
        generation = catalog_generation()
        if type(generation) is not int or generation < 0:
            _error(
                errors.INVALID_ARGUMENT,
                "Catalog generation 必须是非负整数",
                "bind_write_authorizer",
                field="catalog_generation",
                actual=repr(generation),
            )
        self._write_session_id = session_id
        self._catalog_generation = catalog_generation
        self._token_is_authorized = token_is_authorized

    @property
    def is_closed(self) -> bool:
        return self._closed

    def create_heap(self, table_id: int) -> int:
        """分配并初始化用户表根页，返回稳定的根页号。"""
        self._require_mutable("create_heap", TransactionState.ACTIVE)
        _validate_table_id(table_id, allow_catalog=False, operation="create_heap")
        page_id = self._buffer.new_page()
        _validate_allocated_page_id(
            page_id, "create_heap", minimum=self._first_allocatable_page_id
        )
        # new_page 只承诺全零和已分配；合法的数据页头由本层写入。
        page = DataPage.empty(
            table_id, page_id=page_id, version=self._data_page_version
        )
        self._buffer.write_page(page_id, page.to_bytes())
        return page_id

    def initialize_reserved_heap(self, table: TableDef) -> None:
        """在 BOOTSTRAP 下初始化固定的系统目录根页。"""
        allowed = TransactionState.BOOTSTRAP if self._guard is not None else None
        self._require_mutable("initialize_reserved_heap", allowed)
        formal = _validate_table(table, "initialize_reserved_heap")
        expected_roots = {0: CATALOG_ROOT_PAGE_ID}
        if self._guard is not None:
            expected_roots[SYSTEM_INDEXES_ID] = V2_INDEX_CATALOG_ROOT_PAGE_ID
        if expected_roots.get(formal.ref.table_id) != formal.ref.root_page_id:
            _error(
                errors.INVALID_ARGUMENT,
                "系统目录表号和固定根页不匹配",
                "initialize_reserved_heap",
                table_id=formal.ref.table_id,
                root_page_id=formal.ref.root_page_id,
                expected=expected_roots,
            )
        if self._guard is None and getattr(self._file_manager, "is_new", None) is not True:
            _error(
                errors.INVALID_ARGUMENT,
                "只有全新数据库文件可以初始化系统目录根页",
                "initialize_reserved_heap",
                field="file_manager.is_new",
                expected=True,
                actual=repr(getattr(self._file_manager, "is_new", None)),
            )

        root_page_id = formal.ref.root_page_id
        reserved_snapshot = self._buffer.get_snapshot(root_page_id)
        reserved = reserved_snapshot.data
        if type(reserved) is not bytes or reserved != bytes(PAGE_SIZE):
            _error(
                errors.PAGE_CORRUPTED,
                "系统目录预留页不是完整全零页",
                "initialize_reserved_heap",
                page_id=root_page_id,
                field="reserved_page",
                expected="4096 zero bytes",
                actual=(
                    type(reserved).__name__
                    if type(reserved) is not bytes
                    else f"{len(reserved)} bytes with nonzero data"
                ),
            )
        page = DataPage.empty(
            formal.ref.table_id,
            page_id=root_page_id,
            version=self._data_page_version,
        )
        self._buffer.write_if_current(reserved_snapshot, page.to_bytes())

    def validate_table_root(self, table: TableDef) -> None:
        """读取真实根页，确认格式和 table_id 与 TableDef 一致。"""
        self._require_open("validate_table_root")
        formal = _validate_table(table, "validate_table_root")
        self._read_table_page(formal, formal.ref.root_page_id)

    def insert_row(
        self,
        table: TableDef,
        row: Row,
        token: ValidatedWriteToken | None = None,
    ) -> RowId | RowMovement:
        """插入一行；v2 必须携带已由 Session 授权的 token。

        三参数 v1 实例保留 RowId 返回值。注入 TransactionGuard 的 v2 实例
        拒绝无 token 调用，并返回 ``RowMovement(None, new)``。
        """
        self._require_mutable("insert_row", TransactionState.ACTIVE)
        self._require_write_token(token, "insert_row")
        formal = _validate_table(table, "insert_row")

        # 所有可预先发现的行错误都必须发生在任何页面修改之前。
        encoded = self._encode_candidate(formal, row, "insert_row")

        tail_id = formal.ref.root_page_id
        for snapshot, page in self._walk_pages(formal):
            page_id = snapshot.page_id
            tail_id = page_id
            row_slot = page.insert(encoded)
            if row_slot is not None:
                self._buffer.write_if_current(snapshot, page.to_bytes())
                row_id = RowId(page_id, row_slot.slot_id, row_slot.generation)
                return self._insert_result(row_id, row, token)

        # 所有旧页都放不下时才扩页。先写好新页，再把旧尾页连向它；
        # 这样不会让表链暂时指向一张全零、尚无合法页头的页面。
        self._preflight_page_capacity(1, "insert_row")
        new_page_id = self._buffer.new_page()
        _validate_allocated_page_id(
            new_page_id, "insert_row", minimum=self._first_allocatable_page_id
        )
        new_page = DataPage.empty(
            formal.ref.table_id,
            page_id=new_page_id,
            version=self._data_page_version,
        )
        row_slot = new_page.insert(encoded)
        if row_slot is None:  # 前面的 MAX_RECORD_SIZE 校验保证理论上不会发生。
            _error(
                errors.ROW_ENCODING_ERROR,
                "通过大小校验的记录仍无法写入空页",
                "insert_row",
                encoded_size=len(encoded),
            )
        self._buffer.write_page(new_page_id, new_page.to_bytes())

        # 重新获取尾页的最新副本，避免用遍历时保留的旧副本覆盖其他字段。
        tail_snapshot, tail = self._read_table_snapshot(formal, tail_id)
        if tail.header.next_page_id != INVALID_PAGE_ID:
            _error(
                errors.PAGE_CORRUPTED,
                "扩页前发现尾页已经存在后继",
                "insert_row",
                page_id=tail_id,
                next_page_id=tail.header.next_page_id,
            )
        tail.set_next_page_id(new_page_id)
        self._buffer.write_if_current(tail_snapshot, tail.to_bytes())
        row_id = RowId(new_page_id, row_slot.slot_id, row_slot.generation)
        return self._insert_result(row_id, row, token)

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

        for snapshot, page in self._walk_pages(formal):
            page_id = snapshot.page_id
            if page_id != row_id.page_id:
                continue
            try:
                record = page.record(row_id.slot_id, row_id.generation)
            except errors.DbError as error:
                # 回收逻辑会重置空根页的槽目录。此时旧 RowId 对调用者仍是
                # “已过期的行”，不能泄漏成较低层的 SLOT_ID_INVALID。
                if error.code != errors.SLOT_ID_INVALID:
                    raise
                record = None
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

    def encoded_size(self, table: TableDef, row: Row) -> int:
        """Return the RowCodec size used by prepare-time write accounting.

        This is deliberately a read-only StorageEngine boundary: Executor and
        ConstraintValidator may ask for the size, but neither may reach into
        the private codec or duplicate its format rules.
        """
        self._require_open("encoded_size")
        formal = _validate_table(table, "encoded_size")
        size = self._codec.encoded_size(row, formal.schema)
        if type(size) is not int or size < 0:
            _error(
                errors.ROW_ENCODING_ERROR,
                "RowCodec 返回了无效的编码长度",
                "encoded_size",
                encoded_size=repr(size),
            )
        return size

    def delete_row(self, table: TableDef, row_id: RowId) -> bool:
        """确认 RowId 位于目标表页链后，将有效槽标记为删除。"""
        self._require_mutable("delete_row", TransactionState.ACTIVE)
        if self._guard is not None:
            _error(
                errors.TRANSACTION_REQUIRED,
                "v2 删除必须使用带 token 和 expected_old 的 delete_rows",
                "delete_row",
            )
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

        for snapshot, page in self._walk_pages(formal):
            page_id = snapshot.page_id
            if page_id != row_id.page_id:
                continue
            changed = page.delete(row_id.slot_id, row_id.generation)
            if changed:
                self._buffer.write_if_current(snapshot, page.to_bytes())
            return changed
        _error(
            errors.INVALID_ARGUMENT,
            "RowId 指向的页不属于目标表",
            "delete_row",
            table_id=formal.ref.table_id,
            page_id=row_id.page_id,
            slot_id=row_id.slot_id,
        )

    def update_rows(
        self,
        table: TableDef,
        batch: UpdateBatch,
        token: ValidatedWriteToken,
    ) -> tuple[RowMovement, ...]:
        """整批预检后更新，页内容纳不下时迁移到其他页或新页。"""
        self._require_mutable("update_rows", TransactionState.ACTIVE)
        self._require_write_token(token, "update_rows")
        formal = _validate_table(table, "update_rows")
        if not isinstance(batch, UpdateBatch):
            _error(
                errors.INVALID_ARGUMENT,
                "batch 必须是 UpdateBatch",
                "update_rows",
                field="batch",
                expected="UpdateBatch",
                actual=type(batch).__name__,
            )
        if not batch.items:
            return ()
        self._check_batch_count(len(batch.items), "update_rows")

        encoded_new: list[bytes] = []
        total_bytes = 0
        for item in batch.items:
            old_bytes = self._encode_candidate(
                formal, item.expected_old, "update_rows.expected_old"
            )
            new_bytes = self._encode_candidate(
                formal, item.new_row, "update_rows.new_row"
            )
            total_bytes += len(old_bytes) + len(new_bytes)
            self._check_batch_bytes(total_bytes, "update_rows")
            encoded_new.append(new_bytes)

        pages = self._snapshot_table_pages(formal)
        current_rows: list[StoredRow] = []
        for item in batch.items:
            current_rows.append(
                self._validate_expected_row(
                    formal, pages, item.row_id, item.expected_old, "update_rows"
                )
            )

        destinations: list[tuple[str, int, int, int] | None] = [None] * len(batch.items)
        migrations: list[int] = []
        changed_existing: set[int] = set()
        destination_existing: set[int] = set()

        for index, (item, encoded) in enumerate(zip(batch.items, encoded_new)):
            page = pages[item.row_id.page_id][1]
            if page.replace_record(item.row_id.slot_id, item.row_id.generation, encoded):
                destinations[index] = (
                    "existing",
                    item.row_id.page_id,
                    item.row_id.slot_id,
                    item.row_id.generation,
                )
                changed_existing.add(item.row_id.page_id)
            else:
                migrations.append(index)

        planned_pages: list[DataPage] = []
        for index in migrations:
            encoded = encoded_new[index]
            destination = None
            for page_id, (_, candidate) in pages.items():
                row_slot = candidate.insert(encoded)
                if row_slot is not None:
                    destination = (
                        "existing", page_id, row_slot.slot_id, row_slot.generation
                    )
                    changed_existing.add(page_id)
                    destination_existing.add(page_id)
                    break
            if destination is None:
                for page_index, candidate in enumerate(planned_pages):
                    row_slot = candidate.insert(encoded)
                    if row_slot is not None:
                        destination = (
                            "new", page_index, row_slot.slot_id, row_slot.generation
                        )
                        break
            if destination is None:
                candidate = DataPage.empty(
                    formal.ref.table_id, version=self._data_page_version
                )
                row_slot = candidate.insert(encoded)
                if row_slot is None:
                    raise AssertionError("validated row did not fit an empty page")
                planned_pages.append(candidate)
                destination = (
                    "new", len(planned_pages) - 1, row_slot.slot_id, row_slot.generation
                )
            destinations[index] = destination

        # 所有新记录已经在内存副本中安置后，才删除迁移源槽。
        for index in migrations:
            item = batch.items[index]
            source = pages[item.row_id.page_id][1]
            source.delete(item.row_id.slot_id, item.row_id.generation)
            changed_existing.add(item.row_id.page_id)

        self._preflight_page_capacity(len(planned_pages), "update_rows")
        new_page_ids = [self._buffer.new_page() for _ in planned_pages]
        for page_id in new_page_ids:
            _validate_allocated_page_id(
                page_id, "update_rows", minimum=self._first_allocatable_page_id
            )

        if new_page_ids:
            tail_id = next(reversed(pages))
            tail = pages[tail_id][1]
            tail.set_next_page_id(new_page_ids[0])
            changed_existing.add(tail_id)
            for index, (page_id, candidate) in enumerate(
                zip(new_page_ids, planned_pages)
            ):
                actual = DataPage(
                    candidate.to_bytes(),
                    page_id=page_id,
                    expected_table_id=formal.ref.table_id,
                )
                successor = (
                    new_page_ids[index + 1]
                    if index + 1 < len(new_page_ids)
                    else INVALID_PAGE_ID
                )
                actual.set_next_page_id(successor)
                self._buffer.write_page(page_id, actual.to_bytes())

        # 已链接页中的迁移目标优先写入；同页既是目标又是源时一次提交最终副本。
        ordered_existing = [
            *[page_id for page_id in pages if page_id in destination_existing],
            *[
                page_id
                for page_id in pages
                if page_id in changed_existing and page_id not in destination_existing
            ],
        ]
        for page_id in ordered_existing:
            snapshot, page = pages[page_id]
            self._buffer.write_if_current(snapshot, page.to_bytes())

        movements: list[RowMovement] = []
        for index, (item, current) in enumerate(zip(batch.items, current_rows)):
            destination = destinations[index]
            if destination is None:
                raise AssertionError("update destination was not planned")
            kind, page_or_index, slot_id, generation = destination
            page_id = (
                page_or_index if kind == "existing" else new_page_ids[page_or_index]
            )
            movements.append(
                RowMovement(
                    current,
                    StoredRow(RowId(page_id, slot_id, generation), item.new_row),
                )
            )
        self.reclaim_empty_pages(formal)
        return tuple(movements)

    def delete_rows(
        self,
        table: TableDef,
        expected: tuple[StoredRow, ...],
        token: ValidatedWriteToken,
    ) -> tuple[RowMovement, ...]:
        """核对整批 generation 和 expected_old 后再删除并回收空页。"""
        self._require_mutable("delete_rows", TransactionState.ACTIVE)
        self._require_write_token(token, "delete_rows")
        formal = _validate_table(table, "delete_rows")
        if type(expected) is not tuple or any(
            not isinstance(item, StoredRow) for item in expected
        ):
            _error(
                errors.INVALID_ARGUMENT,
                "expected 必须是 tuple[StoredRow, ...]",
                "delete_rows",
                field="expected",
                expected="tuple[StoredRow, ...]",
                actual=type(expected).__name__,
            )
        if not expected:
            return ()
        self._check_batch_count(len(expected), "delete_rows")
        row_ids = tuple(item.row_id for item in expected)
        if len(set(row_ids)) != len(row_ids):
            _error(
                errors.INVALID_ARGUMENT,
                "DELETE 批次不能包含重复 RowId",
                "delete_rows",
                field="expected",
            )

        total_bytes = 0
        for item in expected:
            total_bytes += len(
                self._encode_candidate(formal, item.values, "delete_rows.expected_old")
            )
            self._check_batch_bytes(total_bytes, "delete_rows")

        pages = self._snapshot_table_pages(formal)
        current_rows = tuple(
            self._validate_expected_row(
                formal, pages, item.row_id, item.values, "delete_rows"
            )
            for item in expected
        )
        changed: set[int] = set()
        for item in expected:
            page = pages[item.row_id.page_id][1]
            page.delete(item.row_id.slot_id, item.row_id.generation)
            changed.add(item.row_id.page_id)
        for page_id in pages:
            if page_id in changed:
                snapshot, page = pages[page_id]
                self._buffer.write_if_current(snapshot, page.to_bytes())
        self.reclaim_empty_pages(formal)
        return tuple(RowMovement(item, None) for item in current_rows)

    def reclaim_empty_pages(self, table: TableDef) -> int:
        """摘除并释放空的非根页；根页只清空记录区域。"""
        self._require_mutable("reclaim_empty_pages", TransactionState.ACTIVE)
        formal = _validate_table(table, "reclaim_empty_pages")
        root_id = formal.ref.root_page_id
        root_snapshot, root = self._read_table_snapshot(formal, root_id)

        if root.header.live_count == 0 and root.header.slot_count:
            root.reset_records()
            self._buffer.write_if_current(root_snapshot, root.to_bytes())

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
            previous_snapshot, previous = self._read_table_snapshot(formal, previous_id)
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
            self._buffer.write_if_current(previous_snapshot, previous.to_bytes())
            self._buffer.free_page(current_id)
            released += 1
            current_id = successor
        return released

    def sync(self) -> None:
        """先写出所有脏页，再要求文件层完成持久化同步。"""
        self._require_open("sync")
        if self._guard is not None:
            _error(
                errors.INVALID_TRANSACTION_STATE,
                "v2 写入只能由 TransactionManager 提交",
                "sync",
                actual_state=self._guard.state.value,
            )
        self._buffer.flush_all()
        self._file_manager.sync()

    def flush_for_commit(self) -> None:
        """供事务协调器在 COMMITTING 状态持久化已有脏页。"""
        self._require_open("flush_for_commit")
        if self._guard is None:
            return self.sync()
        self._guard.require(TransactionState.COMMITTING, operation="flush_for_commit")
        self._buffer.flush_all()
        self._file_manager.sync()

    def close(self) -> None:
        """正常关闭：拒绝活动扫描，成功同步后再关闭文件。"""
        if self._closed:
            return
        self._require_open("close")
        self._require_no_scans("close")
        if self._guard is not None:
            _error(
                errors.INVALID_TRANSACTION_STATE,
                "v2 StorageEngine 必须由 TransactionManager 关闭",
                "close",
                actual_state=self._guard.state.value,
            )
        self.sync()
        self._file_manager.close()
        self._closed = True

    def abort(self) -> None:
        """失败清理：关闭扫描和文件，但绝不刷新脏页。"""
        if self._closed:
            return
        # 先标记 failed，确保清理期间任何新的业务调用都会被拒绝。
        self.abort_resources()
        self._file_manager.close()
        self._closed = True

    def close_scans(self) -> None:
        """关闭全部扫描；TransactionManager 可安全重复调用。"""
        for scan in tuple(self._active_scans):
            scan.close()
        for scan in tuple(self._external_scans):
            scan.close()

    def abort_resources(self) -> None:
        """停止 StorageEngine 并关闭扫描，但不刷新或关闭 FileManager。"""
        self._failed = True
        self.close_scans()

    def _walk_pages(self, table: TableDef) -> Iterator[tuple[PageSnapshot, DataPage]]:
        """从根页遍历整条链，同时检测循环和跨表页面。"""
        current_id = table.ref.root_page_id
        visited: set[int] = set()
        while current_id != INVALID_PAGE_ID:
            snapshot, page = self._read_table_snapshot(table, current_id, visited)
            visited.add(current_id)
            yield snapshot, page
            current_id = page.header.next_page_id

    def _snapshot_table_pages(
        self, table: TableDef
    ) -> dict[int, tuple[PageSnapshot, DataPage]]:
        """一次遍历保留每页版本和独立可变副本，供整批预检使用。"""
        return {
            snapshot.page_id: (snapshot, page)
            for snapshot, page in self._walk_pages(table)
        }

    def _validate_expected_row(
        self,
        table: TableDef,
        pages: dict[int, tuple[PageSnapshot, DataPage]],
        row_id: RowId,
        expected: Row,
        operation: str,
    ) -> StoredRow:
        entry = pages.get(row_id.page_id)
        if entry is None:
            _error(
                errors.INVALID_ARGUMENT,
                "RowId 指向的页不属于目标表",
                operation,
                table_id=table.ref.table_id,
                page_id=row_id.page_id,
                slot_id=row_id.slot_id,
            )
        record = entry[1].record(row_id.slot_id, row_id.generation)
        if record is None:
            _error(
                errors.STALE_ROW,
                "RowId 指向的记录已经失效",
                operation,
                page_id=row_id.page_id,
                slot_id=row_id.slot_id,
                generation=row_id.generation,
            )
        current = self._codec.decode(record, table.schema)
        if current != expected:
            _error(
                errors.STALE_ROW,
                "当前记录与 expected_old 不一致",
                operation,
                page_id=row_id.page_id,
                slot_id=row_id.slot_id,
                generation=row_id.generation,
                expected=repr(expected),
                actual=repr(current),
            )
        return StoredRow(row_id, current)

    def _encode_candidate(self, table: TableDef, row: Row, operation: str) -> bytes:
        encoded_size = self._codec.encoded_size(row, table.schema)
        encoded = self._codec.encode(row, table.schema)
        if (
            type(encoded_size) is not int
            or type(encoded) is not bytes
            or encoded_size != len(encoded)
        ):
            _error(
                errors.ROW_ENCODING_ERROR,
                "RowCodec 的长度结果与实际编码不一致",
                operation,
                encoded_size=repr(encoded_size),
                actual_size=(len(encoded) if type(encoded) is bytes else -1),
            )
        if not 1 <= encoded_size <= MAX_RECORD_SIZE:
            _error(
                errors.ROW_TOO_LARGE,
                "编码后的记录无法放入单个数据页",
                operation,
                encoded_size=encoded_size,
                max_size=MAX_RECORD_SIZE,
            )
        return encoded

    def _require_write_token(
        self, token: ValidatedWriteToken | None, operation: str
    ) -> None:
        if self._guard is None:
            if token is not None:
                _error(
                    errors.INVALID_ARGUMENT,
                    "v1 兼容实例不接受 v2 写入 token",
                    operation,
                    field="token",
                )
            return
        if not isinstance(token, ValidatedWriteToken):
            _error(
                errors.TRANSACTION_REQUIRED,
                "v2 写入必须携带 ValidatedWriteToken",
                operation,
                field="token",
                expected="ValidatedWriteToken",
                actual=type(token).__name__,
            )
        if (
            self._write_session_id is None
            or self._catalog_generation is None
            or self._token_is_authorized is None
        ):
            _error(
                errors.TRANSACTION_REQUIRED,
                "Session 尚未绑定 v2 写授权器",
                operation,
                field="write_authorizer",
            )
        generation = self._catalog_generation()
        if type(generation) is not int or generation < 0:
            _error(
                errors.INVALID_ARGUMENT,
                "当前 Catalog generation 不合法",
                operation,
                field="catalog_generation",
                actual=repr(generation),
            )
        if (
            token.session_id != self._write_session_id
            or token.catalog_generation != generation
            or self._token_is_authorized(token) is not True
        ):
            _error(
                errors.INVALID_ARGUMENT,
                "ValidatedWriteToken 不属于当前 apply 或已经失效",
                operation,
                field="token",
                expected_session_id=str(self._write_session_id),
                actual_session_id=str(token.session_id),
                expected_catalog_generation=generation,
                actual_catalog_generation=token.catalog_generation,
            )

    def _insert_result(
        self, row_id: RowId, row: Row, token: ValidatedWriteToken | None
    ) -> RowId | RowMovement:
        if self._guard is None:
            return row_id
        if token is None:
            raise AssertionError("v2 insert token was not checked")
        return RowMovement(None, StoredRow(row_id, row))

    def _check_batch_count(self, count: int, operation: str) -> None:
        if count > MAX_BATCH_ROWS:
            _execution_error(
                errors.RESOURCE_LIMIT,
                "批量写入目标超过 10000 行",
                operation,
                limit=MAX_BATCH_ROWS,
                actual=count,
            )

    def _check_batch_bytes(self, size: int, operation: str) -> None:
        if size > MAX_BATCH_BYTES:
            _execution_error(
                errors.RESOURCE_LIMIT,
                "批量写入候选编码超过 16 MiB",
                operation,
                limit=MAX_BATCH_BYTES,
                actual=size,
            )

    def _preflight_page_capacity(self, count: int, operation: str) -> None:
        """在首次分配前检查 v2 空闲页和文件增长余量。"""
        if count == 0 or self._guard is None:
            return
        header = getattr(self._file_manager, "_header", None)
        free_pages = getattr(self._file_manager, "_free_pages", None)
        next_page_id = getattr(header, "next_page_id", None)
        if type(next_page_id) is not int or not isinstance(free_pages, set):
            _error(
                errors.INVALID_ARGUMENT,
                "v2 FileManager 未提供页容量快照",
                operation,
                field="file_manager.capacity",
            )
        available = len(free_pages) + max(0, V2_MAX_PAGE_COUNT - next_page_id)
        if count > available:
            _execution_error(
                errors.RESOURCE_LIMIT,
                "数据库剩余页容量不足",
                operation,
                requested_pages=count,
                available_pages=available,
            )

    def _read_table_page(
        self,
        table: TableDef,
        page_id: int,
        visited: set[int] | None = None,
    ) -> DataPage:
        """只读调用不保留快照；计数仍为一次页读取。"""
        return self._read_table_snapshot(table, page_id, visited)[1]

    def _read_table_snapshot(
        self, table: TableDef, page_id: int, visited: set[int] | None = None,
    ) -> tuple[PageSnapshot, DataPage]:
        """在同一次读取中取得数据和版本，不能写前才补取版本。"""
        if visited is not None and page_id in visited:
            _error(
                errors.PAGE_CORRUPTED,
                "表数据页链存在循环",
                "scan_table_pages",
                table_id=table.ref.table_id,
                page_id=page_id,
            )
        snapshot = self._buffer.get_snapshot(page_id)
        page = DataPage(
            snapshot.data,
            page_id=page_id,
            expected_table_id=table.ref.table_id,
        )
        if page.header.version != self._data_page_version:
            _error(
                errors.PAGE_CORRUPTED,
                "数据页版本与数据库文件格式不一致",
                "read_table_page",
                page_id=page_id,
                expected_version=self._data_page_version,
                actual_version=page.header.version,
            )
        return snapshot, page

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

    def _require_no_scans(self, operation: str) -> None:
        if self.active_scan_count:
            _error(
                errors.ACTIVE_SCAN,
                "存在活动扫描时不能修改或正常关闭存储",
                operation,
                active_scan_count=self.active_scan_count,
            )

    def _require_mutable(
        self,
        operation: str,
        allowed_state: TransactionState | None = TransactionState.ACTIVE,
    ) -> None:
        self._require_open(operation)
        self._require_no_scans(operation)
        if self._guard is not None:
            if allowed_state is None:
                _error(
                    errors.INVALID_TRANSACTION_STATE,
                    "v2 写入没有声明允许的事务状态",
                    operation,
                    actual_state=self._guard.state.value,
                )
            self._guard.require(allowed_state, operation=operation)


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


def _validate_allocated_page_id(
    page_id: object, operation: str, *, minimum: int = 2
) -> None:
    """复核 BufferPool.new_page 的返回值，防止覆盖文件头或目录根页。"""
    if type(page_id) is not int or not minimum <= page_id <= MAX_PAGE_ID:
        _error(
            errors.PAGE_ID_INVALID,
            "BufferPool 返回了非法的用户数据页号",
            operation,
            page_id=repr(page_id),
            expected=f"{minimum}..{MAX_PAGE_ID}",
        )


def _error(code: str, message: str, operation: str, **context: object) -> None:
    """唯一错误出口；存储层上下文只保存 JSON 可表示值。"""
    clean = {key: value for key, value in context.items() if value is not None}
    clean["operation"] = operation
    raise errors.DbError(errors.ErrorStage.STORAGE, code, message, None, clean)


def _execution_error(
    code: str, message: str, operation: str, **context: object
) -> None:
    """资源上限属于 EXECUTION 阶段，不能通过存储错误出口伪造阶段。"""
    clean = {key: value for key, value in context.items() if value is not None}
    clean["operation"] = operation
    raise errors.DbError(errors.ErrorStage.EXECUTION, code, message, None, clean)


__all__ = ["StorageEngine"]
