"""张振：通过存储接口管理系统目录，连接只读 Catalog 与目录持久化。

外部接口已经由计划 7.3、7.4、15.2 节确定，这里只调用，不实现：
周升荣的 StorageEngine：
    initialize_reserved_heap(table: TableDef) -> None
    validate_table_root(table: TableDef) -> None
    scan_rows(table: TableDef) -> RowScan
    insert_row(table: TableDef, row: Row) -> RowId
其中 RowScan 可迭代 StoredRow，提供 close() -> None；StoredRow 有 values、row_id。
赵凯航的 RowCodec()：encoded_size(row: Row, schema: Schema) -> int。
公共 DbError(stage, code, message, span, context) 已复用队友写好的实现。

会话由 CLI/Session 装配。sync/close/abort 仍由 Session 调度；本模块不会
自行创建文件、实现页读写或用 JSON 文件替代系统目录。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, NoReturn

from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, Catalog
from minidb.catalog.catalog_rows import catalog_from_rows, table_to_catalog_rows
from minidb.core.disk_types import PAGE_SIZE
from minidb.core.schema import TableDef
from minidb.storage.data_page import DATA_PAGE_HEADER_SIZE, RECORD_SLOT_SIZE

if TYPE_CHECKING:
    from minidb.core.records import Row
    from minidb.storage.storage_engine import StorageEngine


class CatalogManager:
    """维护当前目录快照和下一表号；通过 bootstrap_or_load 完成启动。"""

    def __init__(self, storage: StorageEngine, catalog: Catalog) -> None:
        """保存已加载目录，并从现有最大表号之后继续编号。"""
        # storage 是同一会话传入的实例，不能在这里再创建第二套存储。
        self._storage = storage
        self._catalog = catalog
        self._next_table_id = max((table.ref.table_id for table in catalog.tables), default=0) + 1

    @classmethod
    def bootstrap_or_load(cls, storage: StorageEngine, is_new: bool) -> CatalogManager:
        """新文件初始化目录根页；已有文件完整读取并校验后才返回管理器。"""
        if type(is_new) is not bool:
            _error("INVALID_ARGUMENT", "is_new 必须为 bool", "bootstrap_or_load",
                   field="is_new", expected="bool", actual=repr(is_new))
        # 检查目录实际需要的存储方法；具体存储对象由会话传入。
        for method in ("initialize_reserved_heap", "validate_table_root", "scan_rows", "insert_row"):
            if not callable(getattr(storage, method, None)):
                _error("INVALID_ARGUMENT", "存储对象缺少目录所需接口", "bootstrap_or_load",
                       field="storage", expected=method, actual=type(storage).__name__)

        with _catalog_context("bootstrap_or_load"):
            if is_new:
                # page 1 是否真是全零预留页、文件是否为新文件，由此正式接口复核。
                # 不能单凭调用者传来的 True 无条件清空页面。
                storage.initialize_reserved_heap(SYSTEM_CATALOG_TABLE)
                storage.validate_table_root(SYSTEM_CATALOG_TABLE)
                catalog = Catalog()
            else:
                storage.validate_table_root(SYSTEM_CATALOG_TABLE)
                catalog = _load_catalog(storage)
                for table in catalog.tables:
                    with _catalog_context("validate_table_root", table_id=table.ref.table_id,
                                          table_name=table.ref.name):
                        storage.validate_table_root(table)
        return cls(storage, catalog)

    def find_table(self, name: str) -> TableDef | None:
        """实现 CatalogRead：大小写不敏感地查询用户表，没有则返回 None。"""
        return self._catalog.find_table(name)

    def list_tables(self) -> list[TableDef]:
        """返回按表号排序的新列表，调用者不能通过它改写当前目录。"""
        return self._catalog.list_tables()

    def reserve_table_id(self) -> int:
        """领取一次递增表号；失败建表可以留下空号，不把旧表号重新发出去。"""
        if self._next_table_id > 0xFFFFFFFE:
            _error("ID_EXHAUSTED", "用户表号已经用尽", "reserve_table_id",
                   id_kind="table", limit=0xFFFFFFFE)
        table_id = self._next_table_id
        self._next_table_id += 1
        return table_id

    def persist_and_register(self, table: TableDef) -> None:
        """校验 → 写每一列的目录记录 → 发布新快照。同步成功由 Session 确认。"""
        operation = "persist_and_register"
        if not isinstance(table, TableDef) or table.ref.table_id == 0:
            _error("INVALID_ARGUMENT", "只能注册完整的用户表定义", operation,
                   field="table", expected="用户 TableDef", actual=repr(table))
        if self.find_table(table.ref.name) is not None:
            _error("TABLE_EXISTS", "表名已经登记", operation, table_name=table.ref.name)
        for existing in self._catalog.tables:
            for field in ("table_id", "root_page_id"):
                if getattr(existing.ref, field) == getattr(table.ref, field):
                    _error("INVALID_ARGUMENT", "表号或根页号已经登记", operation,
                           field=field, expected="未登记的编号", actual=getattr(table.ref, field))

        with _catalog_context(operation, table_id=table.ref.table_id, table_name=table.ref.name):
            rows = table_to_catalog_rows(table)
            self._storage.validate_table_root(table)
            _preflight_rows(rows)
            # 先构造候选快照，尽早发现逻辑问题。此变量尚未成为公开目录。
            candidate = Catalog(self._catalog.tables + (table,))
            for row in rows:
                self._storage.insert_row(SYSTEM_CATALOG_TABLE, row)
            # 如果第 N 行写失败，控制流不会走到这里，旧内存目录仍保留。
            # 已经写出的磁盘内容不保证回滚，异常交给 Session 终止会话。
            self._catalog = candidate
            self._next_table_id = max(self._next_table_id, table.ref.table_id + 1)


def _preflight_rows(rows: tuple[Row, ...]) -> None:
    """所有目录行先检查可编码性和大小，避免写到一半才发现普通参数问题。"""
    from minidb.storage.row_codec import RowCodec

    codec = RowCodec()
    # 共用已有页格式常量：4096 字节页 - 32 字节页头 - 8 字节记录槽。
    max_size = PAGE_SIZE - DATA_PAGE_HEADER_SIZE - RECORD_SLOT_SIZE
    for row in rows:
        size = codec.encoded_size(row, SYSTEM_CATALOG_TABLE.schema)
        if size > max_size:
            _error("ROW_TOO_LARGE", "目录记录无法放入一张空数据页", "persist_and_register",
                   encoded_size=size, max_size=max_size)


def _load_catalog(storage: StorageEngine) -> Catalog:
    """从 StoredRow.values 恢复目录，并在所有路径上关闭扫描资源。"""
    scan = storage.scan_rows(SYSTEM_CATALOG_TABLE)
    primary_error = None
    try:
        # catalog_from_rows 已实现七字段检查、分组、缺列检查和列序恢复。
        return catalog_from_rows(record.values for record in scan)
    except BaseException as error:
        # 保留第一次失败；包括用户中断时也必须释放活动扫描。
        primary_error = error
        raise
    finally:
        try:
            scan.close()
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            _record_cleanup_error(primary_error, cleanup_error)


@contextmanager
def _catalog_context(operation: str, **details):
    """为底层 DbError 补目录上下文，保留原 code、stage 和异常对象。"""
    try:
        yield
    except Exception as error:
        # 原样传播底层错误，只为有上下文字典的异常补充目录信息。
        context = getattr(error, "context", None)
        # 目录上下文默认使用系统目录名，但不能覆盖调用方传入的用户表名。
        # 否则用户表根页校验失败时，错误会被误报为 _sys_catalog 损坏，
        # 导致上层无法准确定位实际出错的表。
        updates = {"operation": operation, **details}
        updates.setdefault("table_name", SYSTEM_CATALOG_TABLE.ref.name)
        if hasattr(error, "_update_context"):
            missing = {key: value for key, value in updates.items() if key not in context}
            error._update_context(**missing)
        elif isinstance(context, dict):
            for key, value in updates.items():
                context.setdefault(key, value)
        raise


def _record_cleanup_error(primary: BaseException, cleanup: Exception) -> None:
    """读取失败和 close 同时失败时，保留主错误并附上清理失败信息。"""
    context = getattr(primary, "context", None)
    # 正式接口的 close 错误应为无 SQL 位置的 DbError，可写入规划要求的数组。
    if hasattr(primary, "_update_context") and all(hasattr(cleanup, key) for key in ("stage", "code", "message", "context")):
        primary._update_context(cleanup_errors=[*context.get("cleanup_errors", ()), {
            "stage": cleanup.stage.name,
            "code": cleanup.code,
            "message": cleanup.message,
            "context": cleanup.context,
        }])
    elif isinstance(context, dict) and all(hasattr(cleanup, key) for key in ("stage", "code", "message", "context")):
        context.setdefault("cleanup_errors", []).append({
            "stage": cleanup.stage.name,
            "code": cleanup.code,
            "message": cleanup.message,
            "context": cleanup.context,
        })
    else:
        # 未预期的编程异常仍交给会话处理；Python 3.11 的 note 保存第二个原因。
        primary.add_note(f"关闭目录扫描时又发生 {type(cleanup).__name__}: {cleanup}")


def _error(code: str, message: str, operation: str, **context) -> NoReturn:
    """目录持久化的错误属于 STORAGE；错误类和代码取自公共模块。"""
    from minidb.core import errors

    context["operation"] = operation
    raise errors.DbError(errors.ErrorStage.STORAGE, getattr(errors, code), message, None, context)
