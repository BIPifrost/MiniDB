"""v2 索引页的纯内存编解码（优化方案 9.6）。

不执行 B+ 树分裂、不分配页、不写缓存。调用方用 dataclasses.replace
构造新页，encode 完整校验后用 BufferPool.write_if_current 提交原快照。
单页只检查本页结构；跨页归属、可达性、分隔键和叶链环由 B+ 树检查。
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import IntEnum
from struct import Struct

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, INVALID_PAGE_ID, V2_MAX_PAGE_COUNT
from minidb.core.records import RowId
from minidb.core.schema import DataType, TypeSpec
from minidb.core.value_rules import normalize_value

HEADER = Struct('<4sHHIIIHHHHIII')
SLOT = Struct('<HHHH')
KEY_HEADER = Struct('<BH')
ROW_PREFIX = Struct('<IH')
CHILD = Struct('<I')
MAX_KEY_PAYLOAD = 512


class IndexPageType(IntEnum):
    LEAF = 2
    INTERNAL = 3
    ANCHOR = 4


def _fail(message, code=errors.INDEX_CORRUPTED):
    stage = errors.ErrorStage.EXECUTION if code == errors.RESOURCE_LIMIT else errors.ErrorStage.STORAGE
    raise errors.DbError(stage, code, message, context={'field': 'index_page', 'reason': message})


def _page_id(value):
    return type(value) is int and 3 <= value < V2_MAX_PAGE_COUNT


@dataclass(frozen=True, slots=True)
class IndexKeyCodec:
    """同一索引绑定一种 TypeSpec；只比较 canonical payload，不比较长度前缀。"""
    type_spec: TypeSpec

    def __post_init__(self):
        if not isinstance(self.type_spec, TypeSpec):
            _fail('索引键需要 TypeSpec', errors.INVALID_ARGUMENT)

    def encode(self, value):
        value = normalize_value(value, self.type_spec, nullable=True)
        if value is None:
            return 0, b''
        kind = self.type_spec.kind
        if kind is DataType.INT:
            payload = (value + (1 << 63)).to_bytes(8, 'big')
        elif kind is DataType.DATE:
            payload = ((value - date(1970, 1, 1)).days + (1 << 31)).to_bytes(4, 'big')
        elif kind is DataType.BOOL:
            payload = bytes([value])
        elif kind is DataType.DECIMAL:
            # normalize_value 已产生固定 scale；不用受全局精度影响的乘法。
            sign, digits, _ = value.as_tuple()
            integer = int(''.join(map(str, digits))) * (-1 if sign else 1)
            payload = (integer + (1 << 63)).to_bytes(8, 'big')
        else:
            payload = value.encode('utf-8')
        if len(payload) > MAX_KEY_PAYLOAD:
            _fail('索引键负载超过 512 字节，不能截断', errors.INDEX_KEY_TOO_LARGE)
        return 1, payload

    def decode(self, tag, payload):
        if type(tag) is not int or type(payload) is not bytes or len(payload) > MAX_KEY_PAYLOAD:
            _fail('索引键编码无效')
        if tag == 0:
            if payload:
                _fail('NULL 键负载必须为空')
            return None
        if tag != 1:
            _fail('未知索引键 tag')
        kind = self.type_spec.kind
        lengths = {DataType.INT: 8, DataType.DATE: 4, DataType.BOOL: 1, DataType.DECIMAL: 8}
        if kind in lengths and len(payload) != lengths[kind]:
            _fail('索引键固定长度错误')
        try:
            if kind is DataType.INT:
                value = int.from_bytes(payload, 'big') - (1 << 63)
            elif kind is DataType.DATE:
                value = date(1970, 1, 1) + timedelta(days=int.from_bytes(payload, 'big') - (1 << 31))
            elif kind is DataType.BOOL:
                if payload[0] > 1:
                    _fail('BOOL 索引键不是 00/01')
                value = bool(payload[0])
            elif kind is DataType.DECIMAL:
                integer = int.from_bytes(payload, 'big') - (1 << 63)
                value = Decimal((int(integer < 0), tuple(map(int, str(abs(integer)))), -self.type_spec.scale))
            else:
                value = payload.decode('utf-8', errors='strict')
            if self.encode(value) != (tag, payload):
                _fail('非 canonical 索引键')
            return value
        except (ValueError, OverflowError, errors.DbError, NotImplementedError) as exc:
            # 公共归一化规则的错误属于磁盘损坏；不把损坏值当 SQL 参数错误。
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.INDEX_CORRUPTED,
                                 '索引键不符合列类型或 canonical 编码') from exc


@dataclass(frozen=True, slots=True)
class IndexEntry:
    value: object
    row_id: RowId
    right_child: int = INVALID_PAGE_ID


@dataclass(frozen=True, slots=True)
class IndexPage:
    page_type: IndexPageType
    index_id: int
    parent_page_id: int
    right_sibling: int = INVALID_PAGE_ID
    left_child: int = INVALID_PAGE_ID
    entries: tuple[IndexEntry, ...] = ()

    def _records(self, codec, page_id):
        if not isinstance(codec, IndexKeyCodec) or not _page_id(page_id):
            _fail('必须提供 IndexKeyCodec 和实际索引页号', errors.INVALID_ARGUMENT)
        if not isinstance(self.page_type, IndexPageType):
            _fail('未知索引页类型')
        if type(self.index_id) is not int or not 1 <= self.index_id <= 0xFFFFFFFE:
            _fail('索引编号无效')
        if type(self.entries) is not tuple:
            _fail('索引项必须使用不可变 tuple', errors.INVALID_ARGUMENT)
        if any(type(v) is not int for v in (self.parent_page_id, self.right_sibling, self.left_child)):
            _fail('索引页指针必须是整数')
        anchor = self.page_type is IndexPageType.ANCHOR
        leaf = self.page_type is IndexPageType.LEAF
        if anchor:
            if self.parent_page_id != INVALID_PAGE_ID or self.entries:
                _fail('锚页不能有父页或索引项')
        elif not _page_id(self.parent_page_id) or self.parent_page_id == page_id:
            _fail('父页号无效或指向自身')
        if self.right_sibling != INVALID_PAGE_ID:
            if not leaf or not _page_id(self.right_sibling) or self.right_sibling in (page_id, self.parent_page_id):
                _fail('右兄弟页无效')
        if leaf:
            if self.left_child != INVALID_PAGE_ID:
                _fail('叶页不能有孩子')
        elif not _page_id(self.left_child) or self.left_child in (page_id, self.parent_page_id):
            _fail('最左孩子或锚页根指针无效')
        children = {self.left_child}
        result, previous = [], None
        for entry in self.entries:
            if not isinstance(entry, IndexEntry) or not isinstance(entry.row_id, RowId):
                _fail('索引项或 RowId 类型错误')
            if type(entry.right_child) is not int:
                _fail('索引项孩子指针必须是整数')
            rid = entry.row_id
            if (not _page_id(rid.page_id) or type(rid.slot_id) is not int or
                    not 0 <= rid.slot_id <= 0xFFFF or type(rid.generation) is not int or
                    not 1 <= rid.generation <= 0xFFFFFF):
                _fail('索引 RowId 超出 v2 范围')
            tag, payload = codec.encode(entry.value)
            order = (tag, payload, rid.page_id, rid.slot_id, rid.generation)
            if previous is not None and order <= previous:
                _fail('索引项未严格递增或复合键重复')
            previous = order
            record = KEY_HEADER.pack(tag, len(payload)) + payload
            record += ROW_PREFIX.pack(rid.page_id, rid.slot_id) + rid.generation.to_bytes(3, 'little')
            if leaf:
                if entry.right_child != INVALID_PAGE_ID:
                    _fail('叶项不能有右孩子')
            else:
                child = entry.right_child
                if not _page_id(child) or child in children or child in (page_id, self.parent_page_id):
                    _fail('内部页孩子无效或重复')
                children.add(child)
                record += CHILD.pack(child)
            result.append(record)
        return result

    def encode(self, codec: IndexKeyCodec, *, page_id: int) -> bytes:
        """完整校验并生成紧凑整页；调用方随后提交 PageSnapshot。"""
        try:
            return self._encode(codec, page_id=page_id)
        except errors.DbError as exc:
            exc._update_context(index_id=repr(self.index_id), page_id=repr(page_id))
            raise

    def _encode(self, codec: IndexKeyCodec, *, page_id: int) -> bytes:
        """构造紧凑的完整页；放不下报 RESOURCE_LIMIT，由树算法先行分裂。"""
        records = self._records(codec, page_id)
        free_start = HEADER.size + sum(map(len, records))
        free_end = PAGE_SIZE - SLOT.size * len(records)
        if free_start > free_end:
            _fail('索引页空间不足，需要分裂', errors.RESOURCE_LIMIT)
        data = bytearray(PAGE_SIZE)
        HEADER.pack_into(data, 0, b'MIDX', 2, int(self.page_type), self.index_id,
                         self.parent_page_id, self.right_sibling, len(records),
                         free_start, free_end, 0, self.left_child, 0, 0)
        offset = HEADER.size
        for i, record in enumerate(records):
            data[offset:offset + len(record)] = record
            SLOT.pack_into(data, PAGE_SIZE - (i + 1) * SLOT.size, offset, len(record), 0, 0)
            offset += len(record)
        return bytes(data)

    @classmethod
    def decode(cls, data: bytes, codec: IndexKeyCodec, *, page_id: int,
               expected_index_id: int):
        try:
            return cls._decode(data, codec, page_id=page_id, expected_index_id=expected_index_id)
        except errors.DbError as exc:
            exc._update_context(index_id=repr(expected_index_id), page_id=repr(page_id))
            raise

    @classmethod
    def _decode(cls, data: bytes, codec: IndexKeyCodec, *, page_id: int,
                expected_index_id: int):
        """验证单页结构和预期归属；不进行磁盘 I/O，不修复损坏页。"""
        if not isinstance(codec, IndexKeyCodec) or not _page_id(page_id):
            _fail('必须提供 IndexKeyCodec 和实际索引页号', errors.INVALID_ARGUMENT)
        if type(expected_index_id) is not int or not 1 <= expected_index_id <= 0xFFFFFFFE:
            _fail('预期索引编号无效', errors.INVALID_ARGUMENT)
        if type(data) is not bytes or len(data) != PAGE_SIZE:
            _fail('索引页必须恰好 4096 字节')
        magic, version, kind, identity, parent, sibling, count, start, end, r16, left, ra, rb = HEADER.unpack_from(data)
        if magic != b'MIDX' or version != 2 or identity != expected_index_id or any((r16, ra, rb)):
            _fail('索引页标识、版本、归属或保留字段错误')
        try:
            page_type = IndexPageType(kind)
        except ValueError:
            _fail('未知索引页类型')
        if not HEADER.size <= start <= end <= PAGE_SIZE or end != PAGE_SIZE - count * SLOT.size:
            _fail('索引页空闲区或槽目录越界')
        if page_type is IndexPageType.ANCHOR and (count or start != HEADER.size or any(data[HEADER.size:])):
            _fail('锚页头部之后必须全零')
        entries, spans = [], []
        for i in range(count):
            offset, length, flags, reserved = SLOT.unpack_from(data, PAGE_SIZE - (i + 1) * SLOT.size)
            minimum = 12 if page_type is IndexPageType.LEAF else 16
            if flags or reserved or offset < HEADER.size or length < minimum or offset + length > start:
                _fail('索引槽越界、长度错误或保留字段非零')
            spans.append((offset, offset + length))
            tag, key_length = KEY_HEADER.unpack_from(data, offset)
            if length != minimum + key_length:
                _fail('索引项长度与键长度不一致')
            pos = offset + KEY_HEADER.size
            value = codec.decode(tag, data[pos:pos + key_length])
            pos += key_length
            row_page, slot_id = ROW_PREFIX.unpack_from(data, pos)
            generation = int.from_bytes(data[pos + 6:pos + 9], 'little')
            if not _page_id(row_page) or not generation:
                _fail('索引项 RowId 无效')
            child = CHILD.unpack_from(data, pos + 9)[0] if page_type is IndexPageType.INTERNAL else INVALID_PAGE_ID
            entries.append(IndexEntry(value, RowId(row_page, slot_id, generation), child))
        for a, b in zip(sorted(spans), sorted(spans)[1:]):
            if a[1] > b[0]:
                _fail('索引记录区域重叠')
        page = cls(page_type, identity, parent, sibling, left, tuple(entries))
        page._records(codec, page_id)
        return page
