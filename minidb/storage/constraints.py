"""关系约束的唯一实现：只读候选和索引，不写表页，也不实现Session或IndexManager。

装配：Session创建 ConstraintValidator(session, catalog, row_codec, key_codec)。
待提供接口：
  session.session_id: UUID
  session.register_validated_token(token: ValidatedWriteToken) -> None
    登记对象身份和三个绑定字段；核对/一次性消费由Session.apply实现，此处不代写。
  key_codec.encoded_size(key, type_spec: TypeSpec) -> int
    返回IndexKeyCodec规范键负载字节数，不包含行前缀；不能改用整行RowCodec。
  lookup.probe(index: IndexDef, key) -> Iterator[RowId]
    由同一会话的IndexManager实现，不能伪造空命中绕过约束。
ValidatedWriteToken、ValidatedInsert、ValidatedUpdate的唯一源文件为本模块。
公共RowUpdate/UpdateBatch仍从core.records导入；其v2 Value支持待原负责人提供。
"""
from dataclasses import dataclass
from collections.abc import Iterator
from typing import Protocol
from uuid import UUID

from minidb.core._v2_contract import fail, require_method
from minidb.core.errors import DbError
from minidb.core.records import RowId, RowUpdate, UpdateBatch
from minidb.core.schema import TableDef, Schema, IndexDef, IndexOrigin, PendingIndexDef
from minidb.core.value_rules import normalize_value

_MAX_ROWS = 10000
_MAX_BYTES = 16 * 1024 * 1024
_TOKEN_AUTHORITY = object()


class ConstraintLookup(Protocol):
    def probe(self, index: IndexDef, key: object) -> Iterator[RowId]: ...


@dataclass(frozen=True, slots=True, init=False)
class ValidatedWriteToken:
    session_id: UUID
    catalog_generation: int
    prepared_id: UUID

    def __init__(self, session_id, catalog_generation, prepared_id, *, _authority=None):
        if _authority is not _TOKEN_AUTHORITY:
            raise TypeError("写入令牌只能由Session持有的ConstraintValidator工厂签发")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "catalog_generation", catalog_generation)
        object.__setattr__(self, "prepared_id", prepared_id)


@dataclass(frozen=True, slots=True)
class ValidatedInsert:
    row: tuple
    token: ValidatedWriteToken


@dataclass(frozen=True, slots=True)
class ValidatedUpdate:
    batch: UpdateBatch
    token: ValidatedWriteToken


def pending_constraint_indexes(schema: Schema) -> tuple[PendingIndexDef, ...]:
    """prepare建表只产生待定定义；不能猜将来的表号、索引号和页号。"""
    if not isinstance(schema, Schema):
        fail("INVALID_ARGUMENT", "自动索引定义需要Schema")
    return tuple(PendingIndexDef(None, i, True,
                                IndexOrigin.PRIMARY_KEY if c.primary_key else IndexOrigin.UNIQUE_CONSTRAINT)
                 for i, c in enumerate(schema.columns) if c.unique)


def _normalize_rows(table, rows):
    """先检查所有值数和类型，再检查非空，最后由调用方检查唯一约束。"""
    columns = table.schema.columns
    for row in rows:
        if type(row) is not tuple or len(row) != len(columns):
            fail("ROW_VALUE_COUNT_MISMATCH", "候选行列数错误", stage="STORAGE",
                 expected=len(columns), actual=len(row) if isinstance(row, tuple) else repr(row))
    normalized = [list(row) for row in rows]
    # 按定义列序检查，错误不依赖INSERT列顺序或索引号顺序。
    for position, column in enumerate(columns):
        for row in normalized:
            try:
                row[position] = normalize_value(row[position], column.type_spec, nullable=True)
            except DbError as error:
                error._update_context(table_name=table.ref.name, column_name=column.name)
                raise
    for position, column in enumerate(columns):
        if not column.nullable and any(row[position] is None for row in normalized):
            fail("NOT_NULL_VIOLATION", "非空列不能保存NULL", stage="STORAGE",
                 table_name=table.ref.name, column_name=column.name)
    return tuple(tuple(row) for row in normalized)


def _key_size(codec, key, spec):
    size = require_method(codec, "encoded_size", "encoded_size(key, type_spec) -> int")(key, spec)
    if type(size) is not int or size < 0:
        fail("INVALID_ARGUMENT", "IndexKeyCodec必须返回非负负载长度")
    if size > 512:
        fail("INDEX_KEY_TOO_LARGE", "索引键负载超过512字节", stage="STORAGE", actual=size, limit=512)
    return size


class ConstraintValidator:
    def __init__(self, session, catalog, row_codec, key_codec):
        self._session, self._catalog = session, catalog
        self._row_codec, self._key_codec = row_codec, key_codec

    def _context(self, table, prepared_id):
        if not isinstance(table, TableDef) or self._catalog.find_table(table.ref.name) != table:
            fail("INVALID_ARGUMENT", "候选表不属于当前目录")
        sid = getattr(self._session, "session_id", None)
        generation = getattr(self._catalog, "generation", None)
        if not isinstance(sid, UUID) or not isinstance(prepared_id, UUID) or type(generation) is not int or generation < 0:
            fail("INVALID_ARGUMENT", "验证需要会话UUID、prepared UUID和有效目录代际")
        require_method(self._session, "register_validated_token", "register_validated_token(token) -> None")
        return sid, generation, prepared_id

    def _issue(self, binding):
        # 校验期间目录如果被调用方更换，不能给旧候选签发新代际token。
        if self._catalog.generation != binding[1] or self._session.session_id != binding[0]:
            fail("INVALID_ARGUMENT", "约束验证期间会话/目录已变化")
        token = ValidatedWriteToken(*binding, _authority=_TOKEN_AUTHORITY)
        self._session.register_validated_token(token)
        return token

    def _check_sizes(self, table, rows):
        total = 0
        encode_size = require_method(self._row_codec, "encoded_size", "encoded_size(row, schema) -> int")
        for row in rows:
            size = encode_size(row, table.schema)
            if type(size) is not int or size < 0:
                fail("INVALID_ARGUMENT", "RowCodec必须返回非负长度")
            if size > 4056:
                fail("ROW_TOO_LARGE", "候选行超过空页容量", stage="STORAGE", actual=size, limit=4056)
            total += size
            if total > _MAX_BYTES:
                fail("RESOURCE_LIMIT", "旧值与新值编码合计超过16MiB", stage="EXECUTION", limit=_MAX_BYTES)

    def _check_unique(self, table, lookup, rows, excluded):
        indexes = self._catalog.indexes_for_table(table.ref.table_id)
        # 自动约束索引必须实际存在；显式UNIQUE索引也必须检查。
        for position, column in enumerate(table.schema.columns):
            if column.unique:
                origin = IndexOrigin.PRIMARY_KEY if column.primary_key else IndexOrigin.UNIQUE_CONSTRAINT
                matches = [i for i in indexes if i.column_index == position and i.origin is origin and i.unique]
                if len(matches) != 1:
                    fail("CATALOG_CORRUPTED", "正式约束缺少自动索引", stage="STORAGE",
                         table_name=table.ref.name, column_name=column.name)
        for index in indexes:
            for row in rows:
                _key_size(self._key_codec, row[index.column_index], table.schema.columns[index.column_index].type_spec)
        unique = sorted((i for i in indexes if i.unique),
                        key=lambda i: (i.origin is not IndexOrigin.PRIMARY_KEY, i.column_index, i.index_id))
        probe = require_method(lookup, "probe", "probe(index: IndexDef, key) -> Iterator[RowId]") if unique else None
        for index in unique:
            seen = set()
            for row in rows:
                key = row[index.column_index]
                if key is None:
                    continue
                if key in seen:
                    self._conflict(table, index)
                seen.add(key)
                hits = probe(index, key)
                error = None
                try:
                    for row_id in hits:
                        if not isinstance(row_id, RowId):
                            fail("INVALID_ARGUMENT", "ConstraintLookup必须返回RowId")
                        if row_id not in excluded:
                            self._conflict(table, index)
                except BaseException as caught:
                    error = caught
                    raise
                finally:
                    # probe可以是普通迭代器；如果提供close，提前冲突时也必须释放。
                    close = getattr(hits, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception as cleanup:
                            if error is None:
                                raise
                            error.add_note(f"索引探测close失败：{cleanup}")

    @staticmethod
    def _conflict(table, index):
        code = "PRIMARY_KEY_VIOLATION" if index.origin is IndexOrigin.PRIMARY_KEY else "UNIQUE_VIOLATION"
        fail(code, "候选最终状态存在重复非空键", stage="STORAGE", table_name=table.ref.name,
             column_name=table.schema.columns[index.column_index].name, index_name=index.name)

    def validate_insert(self, table: TableDef, lookup: ConstraintLookup, row: tuple,
                        prepared_id: UUID) -> ValidatedInsert:
        binding = self._context(table, prepared_id)
        normalized = _normalize_rows(table, (row,))
        self._check_sizes(table, normalized)
        self._check_unique(table, lookup, normalized, set())
        return ValidatedInsert(normalized[0], self._issue(binding))

    def validate_update(self, table: TableDef, lookup: ConstraintLookup, batch: UpdateBatch,
                        prepared_id: UUID) -> ValidatedUpdate:
        binding = self._context(table, prepared_id)
        if not isinstance(batch, UpdateBatch):
            fail("INVALID_ARGUMENT", "更新必须使用core.records.UpdateBatch.items")
        if len(batch.items) > _MAX_ROWS:
            fail("RESOURCE_LIMIT", "UPDATE候选超过10000行", stage="EXECUTION", limit=_MAX_ROWS)
        # expected_old原样保留，不能用归一化后的新对象掩盖旧值检查。
        old = tuple(item.expected_old for item in batch.items)
        normalized = _normalize_rows(table, tuple(item.new_row for item in batch.items))
        self._check_sizes(table, old + normalized)
        excluded = {item.row_id for item in batch.items}
        self._check_unique(table, lookup, normalized, excluded)
        result = UpdateBatch(tuple(RowUpdate(item.row_id, item.expected_old, row)
                                   for item, row in zip(batch.items, normalized)))
        return ValidatedUpdate(result, self._issue(binding))


def validate_unique_stream(table, indexes, rows, key_codec):
    """供建索引/迁移prepare调用的纯校验；rows为调用方的行流，不在这里实现扫描或排序。

    indexes使用PendingIndexDef。合计最多10万个非空查重键、16MiB规范键负载；
    普通INSERT/UPDATE禁止调用本函数代替正式索引probe。
    """
    if not isinstance(table, TableDef):
        fail("INVALID_ARGUMENT", "建索引验证需要TableDef")
    if type(indexes) is not tuple or any(not isinstance(i, PendingIndexDef) for i in indexes):
        fail("INVALID_ARGUMENT", "建索引验证需要tuple[PendingIndexDef]")
    if any(i.column_index >= len(table.schema.columns) for i in indexes):
        fail("INVALID_ARGUMENT", "待定索引列越界")
    seen = [set() for _ in indexes]
    count = total = 0
    for raw in rows:
        row = _normalize_rows(table, (raw,))[0]
        for position, index in enumerate(indexes):
            key = row[index.column_index]
            size = _key_size(key_codec, key, table.schema.columns[index.column_index].type_spec)
            if index.unique and key is not None:
                count += 1
                total += size
                if count > 100000 or total > _MAX_BYTES:
                    fail("RESOURCE_LIMIT", "建索引/迁移查重集合超限", stage="EXECUTION",
                         key_count=count, encoded_size=total)
                if key in seen[position]:
                    ConstraintValidator._conflict(table, index)
                seen[position].add(key)
