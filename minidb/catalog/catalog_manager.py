"""v2目录的加载、预检和事务内发布；不实现页、事务、令牌消费或索引算法。

目录依赖的装配接口：
    StorageEngine.catalog_services: CatalogServices
由Session装配下列服务，其中 write_catalog_rows(table, rows) -> None 是拟定的
目录写入适配接口：必须使用Session签发的目录写令牌并调用正式StorageEngine，
不得省略token、开启第二个事务或在适配层复制约束规则。
format_version() -> int 必须返回实际已验证文件版本；不是让调用方随意填常量。
validate_index_root(index, table) -> None 由IndexManager校验锚点及页归属。
StorageEngine已提供catalog_services及bind_catalog_services；正式Session仍需绑定
目录写入和索引根校验回调。未装配时明确停止，不尝试写v1文件。
"""
from dataclasses import dataclass
from collections.abc import Callable
from minidb.catalog.catalog import Catalog, SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE
from minidb.catalog.catalog_rows import catalog_from_rows, table_to_catalog_rows, index_to_catalog_row
from minidb.core._v2_contract import fail, require_method
from minidb.core.errors import DbError
from minidb.core.schema import MAX_USER_TABLE_ID, TableDef, IndexDef
from minidb.core.transaction import TransactionGuard, TransactionState


@dataclass(frozen=True, slots=True)
class CatalogServices:
    """仅描述目录所依赖的服务；实现和装配归相应模块负责人。"""
    guard: TransactionGuard
    codec: object
    format_version: Callable
    write_catalog_rows: Callable
    validate_index_root: Callable


class CatalogManager:
    def __init__(self, storage, catalog: Catalog):
        self._storage = storage
        self._services = _services(storage)
        if not isinstance(catalog, Catalog):
            fail("INVALID_ARGUMENT", "catalog必须是Catalog")
        self._catalog = catalog
        self._generation = 0
        self._reset_next_ids()

    @classmethod
    def bootstrap_or_load(cls, storage, is_new: bool):
        if type(is_new) is not bool:
            fail("INVALID_ARGUMENT", "is_new必须为bool")
        manager = cls(storage, Catalog())
        services = manager._services
        if is_new:
            services.guard.require(TransactionState.BOOTSTRAP, operation="catalog.bootstrap")
            # 首次初始化前已经验收外部接口；空目录没有要编码的行。
            init = require_method(storage, "initialize_reserved_heap",
                                  "initialize_reserved_heap(table: TableDef) -> None")
            for table in (SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE):
                init(table)
                storage.validate_table_root(table)
        else:
            services.guard.require(TransactionState.READ_ONLY_STARTUP, TransactionState.RECOVERY,
                                   TransactionState.IDLE, operation="catalog.load")
            manager._catalog = manager._read_snapshot()
            manager._reset_next_ids()
        return manager

    @property
    def generation(self) -> int:
        return self._generation

    def find_table(self, name: str) -> TableDef | None:
        return self._catalog.find_table(name)

    def list_tables(self) -> list[TableDef]:
        return self._catalog.list_tables()

    def find_index(self, name: str) -> IndexDef | None:
        return self._catalog.find_index(name)

    def indexes_for_table(self, table_id: int) -> tuple[IndexDef, ...]:
        return self._catalog.indexes_for_table(table_id)

    def validate_integrity(self):
        """Session提交前调用；不负责提交事务。"""
        self._catalog.validate_integrity()

    def _reset_next_ids(self):
        self._next_table_id = max((t.ref.table_id for t in self._catalog.tables), default=0) + 1
        self._next_index_id = max((i.index_id for i in self._catalog.indexes), default=0) + 1

    def _reserve(self, attribute, kind):
        self._active("catalog.reserve_" + kind + "_id")
        value = getattr(self, attribute)
        if value > MAX_USER_TABLE_ID:
            fail("ID_EXHAUSTED", "编号已耗尽", stage="STORAGE", id_kind=kind, limit=MAX_USER_TABLE_ID)
        setattr(self, attribute, value + 1)
        return value

    def reserve_table_id(self) -> int:
        return self._reserve("_next_table_id", "table")

    def reserve_index_id(self) -> int:
        """索引申请号与页分配分离；prepare阶段不得调用。"""
        return self._reserve("_next_index_id", "index")

    def _active(self, operation):
        self._services.guard.require(TransactionState.ACTIVE, operation=operation)

    def persist_and_register(self, table: TableDef) -> None:
        self._active("catalog.persist_table")
        if not isinstance(table, TableDef):
            fail("INVALID_ARGUMENT", "需要TableDef")
        if self.find_table(table.ref.name) is not None:
            fail("TABLE_EXISTS", "表名已经存在", table_name=table.ref.name)
        candidate = Catalog(self._catalog.tables + (table,), self._catalog.indexes)
        rows = table_to_catalog_rows(table)
        self.preflight_rows(SYSTEM_CATALOG_TABLE, rows)
        self._storage.validate_table_root(table)
        self._services.write_catalog_rows(SYSTEM_CATALOG_TABLE, rows)
        # 写失败不会发布候选；磁盘回滚必须由Session/TransactionManager完成。
        self._catalog = candidate
        self._next_table_id = max(self._next_table_id, table.ref.table_id + 1)

    def persist_and_register_index(self, index: IndexDef) -> None:
        self._active("catalog.persist_index")
        if not isinstance(index, IndexDef):
            fail("INVALID_ARGUMENT", "需要IndexDef")
        if self.find_index(index.name) is not None:
            fail("INDEX_EXISTS", "索引名已经存在", index_name=index.name)
        candidate = Catalog(self._catalog.tables, self._catalog.indexes + (index,))
        rows = (index_to_catalog_row(index),)
        self.preflight_rows(SYSTEM_INDEXES_TABLE, rows)
        table = next(t for t in candidate.tables if t.ref.table_id == index.table_id)
        self._services.validate_index_root(index, table)
        self._services.write_catalog_rows(SYSTEM_INDEXES_TABLE, rows)
        self._catalog = candidate
        self._next_index_id = max(self._next_index_id, index.index_id + 1)

    def preflight_rows(self, table, rows):
        """prepare阶段可调用：全部记录编码检查后才允许进入写阶段。"""
        for row in rows:
            size = self._services.codec.encoded_size(row, table.schema)
            if type(size) is not int or size < 0:
                fail("INVALID_ARGUMENT", "RowCodec.encoded_size必须返回非负整数")
            if size > 4056:
                fail("ROW_TOO_LARGE", "目录行不能放入空数据页", stage="STORAGE",
                     encoded_size=size, max_size=4056)

    def reload_from_storage(self) -> None:
        self._services.guard.require(TransactionState.IDLE, TransactionState.RECOVERY,
                                     TransactionState.ROLLING_BACK, operation="catalog.reload")
        candidate = self._read_snapshot()
        # 所有表、列、索引和根页校验成功才一次替换，失败不改变generation。
        self._catalog = candidate
        self._reset_next_ids()
        self._generation += 1

    def _read_snapshot(self):
        for table in (SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE):
            self._storage.validate_table_root(table)
        # 边读边校验，坏记录先于close错误被发现；只记当前行的位置。
        positions = {}
        readers = (_read_rows(self._storage, SYSTEM_CATALOG_TABLE, 128 * 64, positions),
                   _read_rows(self._storage, SYSTEM_INDEXES_TABLE, 16381, positions))
        error = None
        try:
            candidate = catalog_from_rows(*readers)
        except BaseException as caught:
            error = caught
            if isinstance(error, DbError) and "row_index" in error.context:
                row_id = positions.get(error.context.get("table_name"))
                if row_id is not None:
                    error._update_context(row_id={"page_id": row_id.page_id,
                                                  "slot_id": row_id.slot_id,
                                                  "generation": row_id.generation})
            raise
        finally:
            # 纯目录转换不拥有输入流；管理器必须关闭两个自己创建的读取器。
            cleanup_error = None
            for reader in readers:
                try:
                    reader.close()
                except Exception as cleanup:
                    if error is not None:
                        _add_cleanup(error, cleanup)
                    elif cleanup_error is not None:
                        _add_cleanup(cleanup_error, cleanup)
                    else:
                        cleanup_error = cleanup
            if cleanup_error is not None:
                raise cleanup_error
        for table in candidate.tables:
            self._storage.validate_table_root(table)
        by_id = {table.ref.table_id: table for table in candidate.tables}
        for index in candidate.indexes:
            self._services.validate_index_root(index, by_id[index.table_id])
        return candidate


def _services(storage):
    services = getattr(storage, "catalog_services", None)
    if not isinstance(services, CatalogServices):
        raise NotImplementedError("StorageEngine.catalog_services: CatalogServices 尚未提供；v1文件不能写入v2目录")
    if not isinstance(services.guard, TransactionGuard):
        fail("INVALID_ARGUMENT", "目录必须共享正式TransactionGuard")
    for name in ("format_version", "write_catalog_rows", "validate_index_root"):
        require_method(services, name, name + "(...)")
    require_method(services.codec, "encoded_size", "encoded_size(row, schema) -> int")
    for name in ("validate_table_root", "scan_rows"):
        require_method(storage, name, name + "(table: TableDef)")
    version = services.format_version()
    if type(version) is not int or version != 2:
        fail("FORMAT_VERSION_UNSUPPORTED", "目录只支持v2文件；旧库须离线迁移", stage="STORAGE",
             actual=repr(version), expected=2)
    return services


def _read_rows(storage, table, limit, positions):
    scan = storage.scan_rows(table)
    error = None
    try:
        for number, record in enumerate(scan):
            if number >= limit:
                fail("CATALOG_CORRUPTED", "目录记录超过文件/表资源上限", stage="STORAGE",
                     table_name=table.ref.name, limit=limit)
            positions[table.ref.name] = getattr(record, "row_id", None)
            yield record.values
    except GeneratorExit:
        # 管理器因目录内容错误主动关闭时，将close异常交回它附加到首错。
        raise
    except BaseException as caught:
        error = caught
        if isinstance(error, DbError) and "table_name" not in error.context:
            error._update_context(table_name=table.ref.name)
        raise
    finally:
        try:
            scan.close()
        except Exception as cleanup:
            if error is None:
                raise
            _add_cleanup(error, cleanup)


def _add_cleanup(error, cleanup):
    """保留首错身份，DbError附加可序列化详情，其余异常用note记录。"""
    if isinstance(error, DbError):
        detail = ({"stage": cleanup.stage.name, "code": cleanup.code,
                   "message": cleanup.message, "context": dict(cleanup.context)}
                  if isinstance(cleanup, DbError) else
                  {"type": type(cleanup).__name__, "message": str(cleanup)})
        error._update_context(cleanup_errors=[*error.context.get("cleanup_errors", ()), detail])
    else:
        error.add_note(f"目录扫描close失败：{cleanup}")
