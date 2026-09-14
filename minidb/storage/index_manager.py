"""Persistent single-column B+ trees built on the shared v2 IndexPage format.

This module owns tree navigation, splitting, deletion, cursors and index/table
synchronization.  It deliberately does not define catalog metadata, constraint
rules, index-page bytes, transactions, or row encoding.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Protocol

from minidb.core import errors
from minidb.core.disk_types import INVALID_PAGE_ID, PAGE_SIZE, PageSnapshot
from minidb.core.records import RowId, RowMovement
from minidb.core.schema import IndexBounds, IndexDef, TableDef
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.storage.index_page import (
    HEADER,
    SLOT,
    IndexEntry,
    IndexKeyCodec,
    IndexPage,
    IndexPageType,
)


MAX_BUILD_ENTRIES = 100_000
MAX_BUILD_KEY_BYTES = 16 * 1024 * 1024


class _BufferLike(Protocol):
    def new_page(self) -> int: ...
    def get_snapshot(self, page_id: int) -> PageSnapshot: ...
    def write_if_current(self, snapshot: PageSnapshot, data: bytes) -> None: ...
    def free_page(self, page_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class IndexCheckReport:
    """Summary returned after a complete reachable-tree structure check."""

    page_count: int
    leaf_count: int
    entry_count: int
    height: int


class IndexCursor(Iterator[RowId]):
    """Closeable lazy leaf-chain cursor.

    The manager keeps writes disabled until this cursor is exhausted or closed.
    Each leaf is read as a PageSnapshot and decoded before any RowId is exposed.
    """

    def __init__(
        self,
        manager: "IndexManager",
        index: IndexDef,
        codec: IndexKeyCodec,
        first_leaf: int,
        bounds: IndexBounds,
    ) -> None:
        self._manager = manager
        self._index = index
        self._codec = codec
        self._next_page_id = first_leaf
        self._bounds = bounds
        self._entries: tuple[IndexEntry, ...] = ()
        self._position = 0
        self._visited: set[int] = set()
        self._previous_key: tuple | None = None
        self._current_value: object | None = None
        self._closed = False

    def __iter__(self) -> "IndexCursor":
        return self

    @property
    def current_value(self) -> object | None:
        """Value belonging to the most recently returned RowId."""
        return self._current_value

    def __next__(self) -> RowId:
        if self._closed:
            raise StopIteration
        try:
            while True:
                while self._position < len(self._entries):
                    entry = self._entries[self._position]
                    self._position += 1
                    key = _entry_key(self._codec, entry)
                    if self._previous_key is not None and key <= self._previous_key:
                        _fail(errors.INDEX_CORRUPTED, "叶链中的复合键没有严格递增")
                    self._previous_key = key
                    relation = _in_bounds(self._codec, entry.value, self._bounds)
                    if relation > 0:
                        self.close()
                        raise StopIteration
                    if relation == 0:
                        self._current_value = entry.value
                        return entry.row_id

                if self._next_page_id == INVALID_PAGE_ID:
                    self.close()
                    raise StopIteration
                page_id = self._next_page_id
                if page_id in self._visited:
                    _fail(errors.INDEX_CORRUPTED, "索引叶链存在环")
                self._visited.add(page_id)
                snapshot, page = self._manager._read_page(
                    self._index, self._codec, page_id
                )
                if page.page_type is not IndexPageType.LEAF:
                    _fail(errors.INDEX_CORRUPTED, "索引叶链指向了非叶页")
                # Keep the revision with the current decoded page. Mutations are
                # blocked while a cursor is active, so a changed revision is an
                # invariant failure instead of a result that may be skipped.
                self._manager._cursor_revisions[self] = snapshot.revision
                self._entries = page.entries
                self._position = 0
                self._next_page_id = page.right_sibling
        except StopIteration:
            raise
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._entries = ()
        self._manager._close_cursor(self)


class IndexManager:
    """Session-scoped owner of all persistent B+ tree operations."""

    def __init__(
        self,
        buffer_pool: _BufferLike,
        storage: object,
        catalog: object,
        guard: TransactionGuard,
    ) -> None:
        for name in ("new_page", "get_snapshot", "write_if_current", "free_page"):
            if not callable(getattr(buffer_pool, name, None)):
                raise TypeError(f"buffer_pool must provide {name}()")
        if not isinstance(guard, TransactionGuard):
            raise TypeError("guard must be a TransactionGuard")
        for dependency, name, methods in (
            (storage, "storage", (
                "register_external_scan", "unregister_external_scan",
                "scan_rows", "fetch_row",
            )),
            (catalog, "catalog", ("list_tables", "indexes_for_table")),
        ):
            for method in methods:
                if not callable(getattr(dependency, method, None)):
                    raise TypeError(f"{name} must provide {method}()")
        if (getattr(storage, "buffer_pool", None) is not buffer_pool
                or getattr(storage, "guard", None) is not guard
                or getattr(catalog, "_storage", None) is not storage):
            raise ValueError("IndexManager dependencies must belong to one Session")
        self._buffer = buffer_pool
        self._storage = storage
        self._catalog = catalog
        self._guard = guard
        self._active_cursors: set[IndexCursor] = set()
        self._cursor_revisions: dict[IndexCursor, int] = {}

    @property
    def active_cursor_count(self) -> int:
        return len(self._active_cursors)

    @property
    def buffer_pool(self) -> _BufferLike:
        return self._buffer

    @property
    def storage(self) -> object:
        return self._storage

    @property
    def catalog(self) -> object:
        return self._catalog

    @property
    def guard(self) -> TransactionGuard:
        return self._guard

    def reserve_anchor(self) -> int:
        """Reserve the stable page id stored in IndexDef before tree creation."""
        self._require_write("IndexManager.reserve_anchor")
        return self._buffer.new_page()

    def create(
        self,
        index: IndexDef,
        entries: Iterable[tuple[object, RowId]],
    ) -> None:
        """Initialize a reserved anchor and build an index from prepared entries."""
        table, codec = self._context(index)
        self._require_write("IndexManager.create")
        prepared = tuple(IndexEntry(value, row_id) for value, row_id in entries)
        if len(prepared) > MAX_BUILD_ENTRIES:
            _fail(errors.RESOURCE_LIMIT, "建索引候选超过 100000 项")
        ordered = sorted(prepared, key=lambda item: _entry_key(codec, item))
        if prepared != tuple(ordered) or len({_entry_key(codec, item) for item in prepared}) != len(prepared):
            _fail(errors.INVALID_ARGUMENT, "建索引候选必须按复合键严格递增且不重复")
        key_bytes = 0
        for item in prepared:
            _preflight_entry(codec, item)
            key_bytes += len(codec.encode(item.value)[1])
            if key_bytes > MAX_BUILD_KEY_BYTES:
                _fail(errors.RESOURCE_LIMIT, "建索引候选键负载超过 16 MiB")

        anchor_snapshot = self._buffer.get_snapshot(index.root_page_id)
        if any(anchor_snapshot.data):
            _fail(errors.INVALID_ARGUMENT, "索引锚页必须是本事务刚保留的空白页")
        leaf_id = self._buffer.new_page()
        leaf_snapshot = self._buffer.get_snapshot(leaf_id)
        leaf = IndexPage(IndexPageType.LEAF, index.index_id, index.root_page_id)
        anchor = IndexPage(
            IndexPageType.ANCHOR,
            index.index_id,
            INVALID_PAGE_ID,
            left_child=leaf_id,
        )
        self._write_page(leaf_snapshot, leaf, codec)
        self._write_page(anchor_snapshot, anchor, codec)
        tree = _BPlusTree(self, index, table, codec)
        for item in prepared:
            tree.insert(item)

    def search(self, index: IndexDef, bounds: IndexBounds) -> IndexCursor:
        """Open a lazy range cursor. Callers must close it when stopping early."""
        if not isinstance(bounds, IndexBounds):
            raise TypeError("bounds must be IndexBounds")
        table, codec = self._context(index)
        del table
        first_leaf = _BPlusTree(self, index, self._table(index), codec).find_leaf(
            _seek_key(codec, bounds)
        )
        cursor = IndexCursor(self, index, codec, first_leaf, bounds)
        self._active_cursors.add(cursor)
        try:
            self._storage.register_external_scan(cursor)
        except BaseException:
            self._active_cursors.discard(cursor)
            raise
        return cursor

    def probe(self, index: IndexDef, key: object) -> IndexCursor:
        """ConstraintLookup implementation for one exact normalized value."""
        if key is None:
            bounds = IndexBounds(False, None, False, False, None, False, True)
        else:
            bounds = IndexBounds(True, key, True, True, key, True, False)
        return self.search(index, bounds)

    def apply_movements(
        self,
        indexes: tuple[IndexDef, ...],
        movements: tuple[RowMovement, ...],
    ) -> None:
        """Synchronize table movements, adding replacements before removing old keys."""
        self._require_write("IndexManager.apply_movements")
        if type(indexes) is not tuple or any(not isinstance(i, IndexDef) for i in indexes):
            raise TypeError("indexes must be tuple[IndexDef, ...]")
        if type(movements) is not tuple or any(not isinstance(m, RowMovement) for m in movements):
            raise TypeError("movements must be tuple[RowMovement, ...]")
        if len({index.index_id for index in indexes}) != len(indexes):
            _fail(errors.INVALID_ARGUMENT, "affected indexes 不能重复")

        contexts = [(index, *_tree_context(self, index)) for index in indexes]
        additions: list[tuple[_BPlusTree, IndexEntry]] = []
        removals: list[tuple[_BPlusTree, IndexEntry]] = []
        for index, table, codec in contexts:
            tree = _BPlusTree(self, index, table, codec)
            for movement in movements:
                old = movement.old
                new = movement.new
                if old is not None:
                    removals.append((tree, IndexEntry(old.values[index.column_index], old.row_id)))
                if new is not None:
                    additions.append((tree, IndexEntry(new.values[index.column_index], new.row_id)))

        unchanged = {
            (tree.index.index_id, _entry_key(tree.codec, entry))
            for tree, entry in additions
        } & {
            (tree.index.index_id, _entry_key(tree.codec, entry))
            for tree, entry in removals
        }
        addition_keys = [
            (tree.index.index_id, _entry_key(tree.codec, entry))
            for tree, entry in additions
            if (tree.index.index_id, _entry_key(tree.codec, entry)) not in unchanged
        ]
        removal_keys = [
            (tree.index.index_id, _entry_key(tree.codec, entry))
            for tree, entry in removals
            if (tree.index.index_id, _entry_key(tree.codec, entry)) not in unchanged
        ]
        if len(set(addition_keys)) != len(addition_keys) or len(set(removal_keys)) != len(removal_keys):
            _fail(errors.INVALID_ARGUMENT, "同一批索引变更包含重复目标")
        # Validate every physical target before the first page mutation. The
        # surrounding file transaction still owns rollback for later I/O faults.
        for tree, entry in additions + removals:
            _preflight_entry(tree.codec, entry)
        for tree, entry in removals:
            identity = (tree.index.index_id, _entry_key(tree.codec, entry))
            if identity not in unchanged and not tree.contains(entry):
                _fail(errors.INDEX_CORRUPTED, "RowMovement 的旧索引项不存在")
        for tree, entry in additions:
            identity = (tree.index.index_id, _entry_key(tree.codec, entry))
            if identity not in unchanged and tree.contains(entry):
                _fail(errors.INDEX_CORRUPTED, "RowMovement 的新索引项已经存在")
        for tree, entry in additions:
            if (tree.index.index_id, _entry_key(tree.codec, entry)) not in unchanged:
                tree.insert(entry)
        for tree, entry in removals:
            if (tree.index.index_id, _entry_key(tree.codec, entry)) not in unchanged:
                tree.delete(entry)

    def validate(self, index: IndexDef) -> IndexCheckReport:
        table, codec = self._context(index)
        return _BPlusTree(self, index, table, codec).validate()

    def check_indexes(
        self, table: TableDef | None = None
    ) -> tuple[IndexCheckReport, ...]:
        """Compare every active table row with every selected index leaf entry."""
        if table is None:
            tables = tuple(self._catalog.list_tables())
        else:
            if not isinstance(table, TableDef) or all(
                candidate != table for candidate in self._catalog.list_tables()
            ):
                _fail(errors.INVALID_ARGUMENT, "check_indexes 的表不属于当前目录")
            tables = (table,)

        reports: list[IndexCheckReport] = []
        for current in tables:
            indexes = self._catalog.indexes_for_table(current.ref.table_id)
            if not indexes:
                continue
            rows = self._storage.scan_rows(current)
            expected = {index.index_id: set() for index in indexes}
            try:
                for stored in rows:
                    for index in indexes:
                        codec = IndexKeyCodec(
                            current.schema.columns[index.column_index].type_spec
                        )
                        entry = IndexEntry(
                            stored.values[index.column_index], stored.row_id
                        )
                        expected[index.index_id].add(_entry_key(codec, entry))
            finally:
                close = getattr(rows, "close", None)
                if callable(close):
                    close()

            for index in indexes:
                codec = IndexKeyCodec(
                    current.schema.columns[index.column_index].type_spec
                )
                tree = _BPlusTree(self, index, current, codec)
                report = tree.validate()
                actual = {
                    _entry_key(codec, entry) for entry in tree.leaf_entries()
                }
                if actual != expected[index.index_id]:
                    missing = len(expected[index.index_id] - actual)
                    extra = len(actual - expected[index.index_id])
                    _fail(
                        errors.INDEX_CORRUPTED,
                        f"索引与表记录不一致：缺少 {missing} 项，多出 {extra} 项",
                    )
                reports.append(report)
        return tuple(reports)

    def validate_index_root(self, index: IndexDef, table: TableDef) -> None:
        """CatalogServices callback used while a catalog snapshot is loading."""
        if not isinstance(table, TableDef) or table.ref.table_id != index.table_id:
            _fail(errors.CATALOG_CORRUPTED, "索引与目录表不匹配")
        codec = IndexKeyCodec(table.schema.columns[index.column_index].type_spec)
        _BPlusTree(self, index, table, codec).validate()

    def reload(self) -> None:
        """Discard cursor state and revalidate indexes after catalog rollback reload."""
        self.close_cursors()
        for table in self._catalog.list_tables():
            for index in self._catalog.indexes_for_table(table.ref.table_id):
                self.validate_index_root(index, table)

    def close_cursors(self) -> None:
        for cursor in tuple(self._active_cursors):
            cursor.close()

    def _table(self, index: IndexDef) -> TableDef:
        table = next(
            (candidate for candidate in self._catalog.list_tables()
             if candidate.ref.table_id == index.table_id),
            None,
        )
        if not isinstance(table, TableDef) or table.ref.table_id != index.table_id:
            _fail(errors.INVALID_ARGUMENT, "索引引用的表不属于当前目录")
        if index.column_index >= len(table.schema.columns):
            _fail(errors.CATALOG_CORRUPTED, "索引列序号越界")
        return table

    def _context(self, index: IndexDef) -> tuple[TableDef, IndexKeyCodec]:
        if not isinstance(index, IndexDef):
            raise TypeError("index must be an IndexDef")
        table = self._table(index)
        return table, IndexKeyCodec(table.schema.columns[index.column_index].type_spec)

    def _require_write(self, operation: str) -> None:
        self._guard.require(TransactionState.ACTIVE, operation=operation)
        if self._active_cursors:
            _fail(errors.ACTIVE_SCAN, "活动索引游标关闭前不能修改索引")

    def _read_page(
        self, index: IndexDef, codec: IndexKeyCodec, page_id: int
    ) -> tuple[PageSnapshot, IndexPage]:
        snapshot = self._buffer.get_snapshot(page_id)
        page = IndexPage.decode(
            snapshot.data,
            codec,
            page_id=page_id,
            expected_index_id=index.index_id,
        )
        return snapshot, page

    def _write_page(
        self, snapshot: PageSnapshot, page: IndexPage, codec: IndexKeyCodec
    ) -> None:
        data = page.encode(codec, page_id=snapshot.page_id)
        self._buffer.write_if_current(snapshot, data)

    def _close_cursor(self, cursor: IndexCursor) -> None:
        self._active_cursors.discard(cursor)
        self._cursor_revisions.pop(cursor, None)
        self._storage.unregister_external_scan(cursor)


class _BPlusTree:
    def __init__(self, manager: IndexManager, index: IndexDef, table: TableDef, codec: IndexKeyCodec) -> None:
        self.manager = manager
        self.index = index
        self.table = table
        self.codec = codec

    def _read(self, page_id: int) -> tuple[PageSnapshot, IndexPage]:
        return self.manager._read_page(self.index, self.codec, page_id)

    def _write(self, snapshot: PageSnapshot, page: IndexPage) -> None:
        self.manager._write_page(snapshot, page, self.codec)

    def _root(self) -> tuple[PageSnapshot, IndexPage, int]:
        anchor_snapshot, anchor = self._read(self.index.root_page_id)
        if anchor.page_type is not IndexPageType.ANCHOR:
            _fail(errors.INDEX_CORRUPTED, "目录根页不是索引锚页")
        return anchor_snapshot, anchor, anchor.left_child

    def find_leaf(self, seek: tuple | None) -> int:
        _, _, page_id = self._root()
        visited = {self.index.root_page_id}
        while True:
            if page_id in visited:
                _fail(errors.INDEX_CORRUPTED, "索引孩子指针存在环")
            visited.add(page_id)
            _, page = self._read(page_id)
            if page.page_type is IndexPageType.LEAF:
                return page_id
            if page.page_type is not IndexPageType.INTERNAL:
                _fail(errors.INDEX_CORRUPTED, "索引树包含非法页类型")
            children = _children(page)
            if seek is None:
                page_id = children[0]
                continue
            position = 0
            while position < len(page.entries) and _entry_key(self.codec, page.entries[position]) <= seek:
                position += 1
            page_id = children[position]

    def insert(self, entry: IndexEntry) -> None:
        self.manager._require_write("IndexManager.insert")
        _preflight_entry(self.codec, entry)
        leaf_id = self.find_leaf(_entry_key(self.codec, entry))
        snapshot, leaf = self._read(leaf_id)
        entries = list(leaf.entries)
        key = _entry_key(self.codec, entry)
        position = _lower_bound(self.codec, entries, key)
        if position < len(entries) and _entry_key(self.codec, entries[position]) == key:
            _fail(errors.INDEX_CORRUPTED, "尝试插入重复索引复合键")
        entries.insert(position, entry)
        candidate = replace(leaf, entries=tuple(entries))
        if _fits(candidate, self.codec, leaf_id):
            self._write(snapshot, candidate)
            if position == 0:
                self._propagate_min(leaf_id, entry)
            return
        self._split_leaf(snapshot, leaf, tuple(entries))

    def contains(self, entry: IndexEntry) -> bool:
        key = _entry_key(self.codec, entry)
        leaf_id = self.find_leaf(key)
        _, leaf = self._read(leaf_id)
        position = _lower_bound(self.codec, list(leaf.entries), key)
        return (
            position < len(leaf.entries)
            and _entry_key(self.codec, leaf.entries[position]) == key
        )

    def _split_leaf(self, snapshot: PageSnapshot, leaf: IndexPage, entries: tuple[IndexEntry, ...]) -> None:
        split = _leaf_split(self.codec, entries)
        right_id = self.manager._buffer.new_page()
        right_snapshot = self.manager._buffer.get_snapshot(right_id)
        left_page = replace(leaf, right_sibling=right_id, entries=entries[:split])
        right_page = IndexPage(
            IndexPageType.LEAF,
            self.index.index_id,
            leaf.parent_page_id,
            right_sibling=leaf.right_sibling,
            entries=entries[split:],
        )
        self._write(right_snapshot, right_page)
        self._write(snapshot, left_page)
        self._insert_parent(snapshot.page_id, right_id, right_page.entries[0])

    def _insert_parent(self, left_id: int, right_id: int, separator: IndexEntry) -> None:
        _, left = self._read(left_id)
        parent_id = left.parent_page_id
        if parent_id == self.index.root_page_id:
            self._new_root(left_id, right_id, separator)
            return
        parent_snapshot, parent = self._read(parent_id)
        if parent.page_type is not IndexPageType.INTERNAL:
            _fail(errors.INDEX_CORRUPTED, "非根索引页的父页不是内部页")
        children = _children(parent)
        try:
            position = children.index(left_id)
        except ValueError:
            _fail(errors.INDEX_CORRUPTED, "父页没有引用分裂的孩子")
        entries = list(parent.entries)
        entries.insert(position, replace(separator, right_child=right_id))
        candidate = replace(parent, entries=tuple(entries))
        if _fits(candidate, self.codec, parent_id):
            self._write(parent_snapshot, candidate)
            return
        self._split_internal(parent_snapshot, parent, tuple(entries))

    def _new_root(self, left_id: int, right_id: int, separator: IndexEntry) -> None:
        anchor_snapshot, anchor, old_root = self._root()
        if old_root != left_id:
            _fail(errors.INDEX_CORRUPTED, "根分裂目标与锚页不一致")
        root_id = self.manager._buffer.new_page()
        root_snapshot = self.manager._buffer.get_snapshot(root_id)
        root = IndexPage(
            IndexPageType.INTERNAL,
            self.index.index_id,
            self.index.root_page_id,
            left_child=left_id,
            entries=(replace(separator, right_child=right_id),),
        )
        for child_id in (left_id, right_id):
            child_snapshot, child = self._read(child_id)
            self._write(child_snapshot, replace(child, parent_page_id=root_id))
        self._write(root_snapshot, root)
        self._write(anchor_snapshot, replace(anchor, left_child=root_id))

    def _split_internal(
        self, snapshot: PageSnapshot, page: IndexPage, entries: tuple[IndexEntry, ...]
    ) -> None:
        split = _internal_split(self.codec, entries)
        promoted = entries[split]
        right_id = self.manager._buffer.new_page()
        right_snapshot = self.manager._buffer.get_snapshot(right_id)
        left_page = replace(page, entries=entries[:split])
        right_page = IndexPage(
            IndexPageType.INTERNAL,
            self.index.index_id,
            page.parent_page_id,
            left_child=promoted.right_child,
            entries=entries[split + 1 :],
        )
        self._write(right_snapshot, right_page)
        self._write(snapshot, left_page)
        for child_id in _children(right_page):
            child_snapshot, child = self._read(child_id)
            self._write(child_snapshot, replace(child, parent_page_id=right_id))
        self._insert_parent(snapshot.page_id, right_id, promoted)

    def delete(self, entry: IndexEntry) -> None:
        self.manager._require_write("IndexManager.delete")
        key = _entry_key(self.codec, entry)
        leaf_id = self.find_leaf(key)
        snapshot, leaf = self._read(leaf_id)
        position = _lower_bound(self.codec, list(leaf.entries), key)
        if position >= len(leaf.entries) or _entry_key(self.codec, leaf.entries[position]) != key:
            _fail(errors.INDEX_CORRUPTED, "待删除的索引项不存在")
        entries = leaf.entries[:position] + leaf.entries[position + 1 :]
        if entries or leaf.parent_page_id == self.index.root_page_id:
            self._write(snapshot, replace(leaf, entries=entries))
            if position == 0 and entries:
                self._propagate_min(leaf_id, entries[0])
            return
        self._remove_empty_leaf(snapshot, leaf)

    def _propagate_min(self, child_id: int, minimum: IndexEntry) -> None:
        while True:
            _, child = self._read(child_id)
            parent_id = child.parent_page_id
            if parent_id == self.index.root_page_id:
                return
            parent_snapshot, parent = self._read(parent_id)
            children = _children(parent)
            try:
                position = children.index(child_id)
            except ValueError:
                _fail(errors.INDEX_CORRUPTED, "父页缺少孩子引用")
            if position == 0:
                child_id = parent_id
                continue
            old = parent.entries[position - 1]
            replacement = IndexEntry(minimum.value, minimum.row_id, old.right_child)
            entries = parent.entries[: position - 1] + (replacement,) + parent.entries[position:]
            self._write(parent_snapshot, replace(parent, entries=entries))
            return

    def _remove_empty_leaf(self, snapshot: PageSnapshot, leaf: IndexPage) -> None:
        leaf_id = snapshot.page_id
        parent_snapshot, parent = self._read(leaf.parent_page_id)
        children = list(_children(parent))
        try:
            position = children.index(leaf_id)
        except ValueError:
            _fail(errors.INDEX_CORRUPTED, "父页缺少空叶引用")
        predecessor = self._leaf_predecessor(leaf_id)
        if predecessor is not None:
            pred_snapshot, pred = self._read(predecessor)
            self._write(pred_snapshot, replace(pred, right_sibling=leaf.right_sibling))
        new_parent = _remove_child(parent, position)
        self._write(parent_snapshot, new_parent)
        self.manager._buffer.free_page(leaf_id)
        if len(_children(new_parent)) == 1:
            self._collapse_single_child(parent_snapshot.page_id)
        elif position == 0:
            minimum = self._subtree_min(_children(new_parent)[0])
            self._propagate_min(parent_snapshot.page_id, minimum)

    def _collapse_single_child(self, page_id: int) -> None:
        snapshot, page = self._read(page_id)
        only_child = _children(page)[0]
        if page.parent_page_id == self.index.root_page_id:
            anchor_snapshot, anchor, root_id = self._root()
            if root_id != page_id:
                _fail(errors.INDEX_CORRUPTED, "锚页根指针不一致")
            child_snapshot, child = self._read(only_child)
            self._write(child_snapshot, replace(child, parent_page_id=self.index.root_page_id))
            self._write(anchor_snapshot, replace(anchor, left_child=only_child))
            self.manager._buffer.free_page(page_id)
            return
        parent_snapshot, parent = self._read(page.parent_page_id)
        children = list(_children(parent))
        try:
            position = children.index(page_id)
        except ValueError:
            _fail(errors.INDEX_CORRUPTED, "祖父页缺少待降高内部页")
        child_snapshot, child = self._read(only_child)
        self._write(child_snapshot, replace(child, parent_page_id=parent_snapshot.page_id))
        if position == 0:
            updated_parent = replace(parent, left_child=only_child)
        else:
            old = parent.entries[position - 1]
            entries = parent.entries[: position - 1] + (
                replace(old, right_child=only_child),
            ) + parent.entries[position:]
            updated_parent = replace(parent, entries=entries)
        self._write(parent_snapshot, updated_parent)
        self.manager._buffer.free_page(page_id)
        if len(_children(updated_parent)) == 1:
            self._collapse_single_child(parent_snapshot.page_id)

    def _leaf_predecessor(self, target: int) -> int | None:
        leaf = self.find_leaf(None)
        previous = None
        visited = set()
        while leaf != INVALID_PAGE_ID:
            if leaf in visited:
                _fail(errors.INDEX_CORRUPTED, "索引叶链存在环")
            visited.add(leaf)
            if leaf == target:
                return previous
            _, page = self._read(leaf)
            previous, leaf = leaf, page.right_sibling
        _fail(errors.INDEX_CORRUPTED, "空叶不在索引叶链中")

    def _subtree_min(self, page_id: int) -> IndexEntry:
        visited = set()
        while True:
            if page_id in visited:
                _fail(errors.INDEX_CORRUPTED, "查找子树最小键时发现环")
            visited.add(page_id)
            _, page = self._read(page_id)
            if page.page_type is IndexPageType.LEAF:
                if not page.entries:
                    _fail(errors.INDEX_CORRUPTED, "非根子树包含空叶")
                return page.entries[0]
            page_id = page.left_child

    def validate(self) -> IndexCheckReport:
        _, _, root_id = self._root()
        visited: set[int] = set()
        leaves: list[tuple[int, IndexPage]] = []
        entry_count = 0

        def visit(page_id: int, parent_id: int, depth: int) -> tuple[tuple | None, tuple | None, int]:
            nonlocal entry_count
            if page_id in visited:
                _fail(errors.INDEX_CORRUPTED, "索引树存在环或页面被多个父页引用")
            visited.add(page_id)
            _, page = self._read(page_id)
            if page.parent_page_id != parent_id:
                _fail(errors.INDEX_CORRUPTED, "索引页 parent 指针错误")
            if page.page_type is IndexPageType.LEAF:
                leaves.append((page_id, page))
                entry_count += len(page.entries)
                low = _entry_key(self.codec, page.entries[0]) if page.entries else None
                high = _entry_key(self.codec, page.entries[-1]) if page.entries else None
                return low, high, depth
            if page.page_type is not IndexPageType.INTERNAL or not page.entries:
                _fail(errors.INDEX_CORRUPTED, "内部页必须至少有两个孩子")
            children = _children(page)
            ranges = [visit(child, page_id, depth + 1) for child in children]
            if len({item[2] for item in ranges}) != 1:
                _fail(errors.INDEX_CORRUPTED, "B+ 树叶子深度不一致")
            for position, separator in enumerate(page.entries):
                expected = ranges[position + 1][0]
                if expected is None or _entry_key(self.codec, separator) != expected:
                    _fail(errors.INDEX_CORRUPTED, "内部页分隔键不是右子树最小键")
                left_high = ranges[position][1]
                if left_high is not None and left_high >= expected:
                    _fail(errors.INDEX_CORRUPTED, "相邻孩子的键范围重叠")
            return ranges[0][0], ranges[-1][1], ranges[0][2]

        _, _, leaf_depth = visit(root_id, self.index.root_page_id, 1)
        for position, (page_id, leaf) in enumerate(leaves):
            expected = leaves[position + 1][0] if position + 1 < len(leaves) else INVALID_PAGE_ID
            if leaf.right_sibling != expected:
                _fail(errors.INDEX_CORRUPTED, "叶链顺序与树的中序顺序不一致")
        return IndexCheckReport(len(visited) + 1, len(leaves), entry_count, leaf_depth)

    def leaf_entries(self) -> Iterator[IndexEntry]:
        page_id = self.find_leaf(None)
        visited = set()
        previous = None
        while page_id != INVALID_PAGE_ID:
            if page_id in visited:
                _fail(errors.INDEX_CORRUPTED, "索引叶链存在环")
            visited.add(page_id)
            _, page = self._read(page_id)
            if page.page_type is not IndexPageType.LEAF:
                _fail(errors.INDEX_CORRUPTED, "索引叶链包含非叶页")
            for entry in page.entries:
                key = _entry_key(self.codec, entry)
                if previous is not None and key <= previous:
                    _fail(errors.INDEX_CORRUPTED, "索引叶链复合键没有严格递增")
                previous = key
                yield entry
            page_id = page.right_sibling


def _tree_context(manager: IndexManager, index: IndexDef) -> tuple[TableDef, IndexKeyCodec]:
    return manager._context(index)


def _fail(code: str, message: str) -> None:
    stage = errors.ERROR_STAGE_BY_CODE.get(code, errors.ErrorStage.EXECUTION)
    raise errors.DbError(stage, code, message, context={"operation": "IndexManager"})


def _entry_key(codec: IndexKeyCodec, entry: IndexEntry) -> tuple:
    tag, payload = codec.encode(entry.value)
    row_id = entry.row_id
    return tag, payload, row_id.page_id, row_id.slot_id, row_id.generation


def _value_key(codec: IndexKeyCodec, value: object) -> tuple[int, bytes]:
    return codec.encode(value)


def _children(page: IndexPage) -> tuple[int, ...]:
    return (page.left_child,) + tuple(entry.right_child for entry in page.entries)


def _lower_bound(codec: IndexKeyCodec, entries: list[IndexEntry], key: tuple) -> int:
    low, high = 0, len(entries)
    while low < high:
        middle = (low + high) // 2
        if _entry_key(codec, entries[middle]) < key:
            low = middle + 1
        else:
            high = middle
    return low


def _used(codec: IndexKeyCodec, entries: tuple[IndexEntry, ...], *, internal: bool) -> int:
    overhead = 16 if internal else 12
    return HEADER.size + SLOT.size * len(entries) + sum(
        overhead + len(codec.encode(entry.value)[1]) for entry in entries
    )


def _fits(page: IndexPage, codec: IndexKeyCodec, page_id: int) -> bool:
    try:
        page.encode(codec, page_id=page_id)
        return True
    except errors.DbError as exc:
        if exc.code == errors.RESOURCE_LIMIT:
            return False
        raise


def _preflight_entry(codec: IndexKeyCodec, entry: IndexEntry) -> None:
    if not isinstance(entry, IndexEntry) or not isinstance(entry.row_id, RowId):
        raise TypeError("index entry must contain a RowId")
    codec.encode(entry.value)
    # The key limit is checked by the codec. This additionally proves that one
    # leaf item can fit before any tree page is touched.
    if _used(codec, (entry,), internal=False) > PAGE_SIZE:
        _fail(errors.INDEX_KEY_TOO_LARGE, "单个索引项无法放入空叶页")


def _leaf_split(codec: IndexKeyCodec, entries: tuple[IndexEntry, ...]) -> int:
    candidates = [
        (abs(_used(codec, entries[:i], internal=False) - _used(codec, entries[i:], internal=False)), i)
        for i in range(1, len(entries))
        if _used(codec, entries[:i], internal=False) <= PAGE_SIZE
        and _used(codec, entries[i:], internal=False) <= PAGE_SIZE
    ]
    if not candidates:
        _fail(errors.INDEX_KEY_TOO_LARGE, "索引叶页找不到合法分裂边界")
    return min(candidates)[1]


def _internal_split(codec: IndexKeyCodec, entries: tuple[IndexEntry, ...]) -> int:
    candidates = [
        (abs(_used(codec, entries[:i], internal=True) - _used(codec, entries[i + 1 :], internal=True)), i)
        for i in range(1, len(entries) - 1)
        if _used(codec, entries[:i], internal=True) <= PAGE_SIZE
        and _used(codec, entries[i + 1 :], internal=True) <= PAGE_SIZE
    ]
    if not candidates:
        _fail(errors.INDEX_KEY_TOO_LARGE, "索引内部页找不到合法分裂边界")
    return min(candidates)[1]


def _seek_key(codec: IndexKeyCodec, bounds: IndexBounds) -> tuple | None:
    if bounds.null_only or not bounds.has_lower:
        return None
    tag, payload = _value_key(codec, bounds.lower)
    if bounds.lower_inclusive:
        return tag, payload, -1, -1, -1
    return tag, payload, 0xFFFFFFFF, 0xFFFF, 0xFFFFFF


def _in_bounds(codec: IndexKeyCodec, value: object, bounds: IndexBounds) -> int:
    """Return -1 before range, 0 inside range, +1 after range."""
    current = _value_key(codec, value)
    if bounds.null_only:
        return 0 if value is None else 1
    if bounds.has_lower:
        lower = _value_key(codec, bounds.lower)
        if current < lower or (current == lower and not bounds.lower_inclusive):
            return -1
    if bounds.has_upper:
        upper = _value_key(codec, bounds.upper)
        if current > upper or (current == upper and not bounds.upper_inclusive):
            return 1
    return 0


def _remove_child(page: IndexPage, position: int) -> IndexPage:
    if page.page_type is not IndexPageType.INTERNAL:
        _fail(errors.INDEX_CORRUPTED, "只能从内部页摘除孩子")
    if position == 0:
        return replace(page, left_child=page.entries[0].right_child, entries=page.entries[1:])
    return replace(page, entries=page.entries[: position - 1] + page.entries[position:])


__all__ = ["IndexCheckReport", "IndexCursor", "IndexManager"]
