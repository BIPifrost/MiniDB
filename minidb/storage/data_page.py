"""固定 4096 字节数据页的解析、校验和记录操作。

本模块只负责“一张页里面怎么放记录”，不负责 Row 的编码，也不执行
文件 I/O。RowCodec 把 Row 转成一段 bytes 后交给这里；StorageEngine
再把这里返回的完整页面交给 BufferPool。

``DataPage`` 类是 StorageEngine 使用的主要入口。文件末尾的模块级函数
保留为稳定的“传入 bytes、返回新 bytes”便捷接口，供固定字节测试和其他
不需要持有 DataPage 对象的调用方使用；两套入口共享同一份实现与校验。

页面修改采用副本方式：输入是完整 ``bytes``，输出也是完整 ``bytes``。
这样不会把 BufferPool 返回的旧副本长期保存下来，也不会在修改一半时
把不完整的页面交给其他模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from struct import Struct
from typing import Iterator

from minidb.core import errors
from minidb.core.disk_types import (
    FIRST_ALLOCATABLE_PAGE_ID,
    FORMAT_VERSION,
    INVALID_PAGE_ID,
    MAX_PAGE_ID,
    PAGE_SIZE,
)


DATA_PAGE_MAGIC = b"MDPG"
DATA_PAGE_VERSION = FORMAT_VERSION
DATA_PAGE_TYPE = 1
DATA_PAGE_HEADER_SIZE = 32
RECORD_SLOT_SIZE = 8
MAX_RECORD_SIZE = PAGE_SIZE - DATA_PAGE_HEADER_SIZE - RECORD_SLOT_SIZE

# 显式使用小端序和标准大小，避免不同平台的本机对齐规则改变磁盘格式。
_HEADER_STRUCT = Struct("<4sHHIIHHHHII")
_SLOT_STRUCT = Struct("<HHB3x")


class SlotState(IntEnum):
    """槽中记录的状态；0 只允许出现在 slot_count 之外的未使用区域。"""

    LIVE = 1
    DELETED = 2


@dataclass(frozen=True, slots=True)
class DataPageHeader:
    """数据页头的逻辑字段。

    ``free_start`` 是记录区的右边界，``free_end`` 是槽目录的左边界；
    两者之间的空间就是本页还可以继续使用的连续空间。
    """

    table_id: int
    next_page_id: int
    slot_count: int
    free_start: int
    free_end: int
    live_count: int

    def __post_init__(self) -> None:
        _require_int_range("table_id", self.table_id, 0, 0xFFFFFFFE)
        _require_int_range(
            "next_page_id",
            self.next_page_id,
            FIRST_ALLOCATABLE_PAGE_ID,
            INVALID_PAGE_ID,
        )
        _require_int_range("slot_count", self.slot_count, 0, 0xFFFF)
        _require_int_range("free_start", self.free_start, DATA_PAGE_HEADER_SIZE, PAGE_SIZE)
        _require_int_range("free_end", self.free_end, DATA_PAGE_HEADER_SIZE, PAGE_SIZE)
        _require_int_range("live_count", self.live_count, 0, 0xFFFFFFFF)
        if self.free_end != PAGE_SIZE - self.slot_count * RECORD_SLOT_SIZE:
            raise ValueError("free_end must match the slot directory boundary")
        if self.free_start > self.free_end:
            raise ValueError("free_start must not exceed free_end")
        if self.live_count > self.slot_count:
            raise ValueError("live_count must not exceed slot_count")


@dataclass(frozen=True, slots=True)
class RecordSlot:
    """一个固定 8 字节槽的逻辑字段。"""

    offset: int
    length: int
    state: SlotState

    def __post_init__(self) -> None:
        _require_int_range("offset", self.offset, DATA_PAGE_HEADER_SIZE, 0xFFFF)
        _require_int_range("length", self.length, 1, 0xFFFF)
        if not isinstance(self.state, SlotState):
            raise TypeError("state must be a SlotState")


@dataclass(frozen=True, slots=True)
class ParsedDataPage:
    """已经通过校验的页快照，供调试和只读调用方使用。"""

    header: DataPageHeader
    slots: tuple[RecordSlot, ...]


class DataPage:
    """一张可编辑的数据页。

    构造函数会立即完整校验输入页面。实例只应在一次短暂的读改写操作
    中使用；写回后应丢弃实例，下一次修改重新从 BufferPool 取页。
    """

    def __init__(
        self,
        data: bytes,
        *,
        page_id: int | None = None,
        expected_table_id: int | None = None,
    ) -> None:
        self._page_id = page_id
        self._data = bytearray(
            _validate_page(data, page_id=page_id, expected_table_id=expected_table_id)
        )
        self._parsed = _parse_validated(self._data)

    @classmethod
    def empty(
        cls,
        table_id: int,
        next_page_id: int = INVALID_PAGE_ID,
        *,
        page_id: int | None = None,
    ) -> "DataPage":
        """创建一张没有记录的合法数据页。"""
        _validate_identity(table_id, "table_id", allow_catalog=True)
        _validate_next_page(next_page_id, page_id=page_id)
        header = DataPageHeader(
            table_id, next_page_id, 0, DATA_PAGE_HEADER_SIZE, PAGE_SIZE, 0
        )
        return cls(_encode_header(header), page_id=page_id)

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        page_id: int | None = None,
        expected_table_id: int | None = None,
    ) -> "DataPage":
        """从完整页面 bytes 构造 DataPage；这是构造函数的可读别名。"""
        return cls(data, page_id=page_id, expected_table_id=expected_table_id)

    @property
    def header(self) -> DataPageHeader:
        """返回当前页头快照，不暴露内部 bytearray。"""
        return self._parsed.header

    @property
    def slots(self) -> tuple[RecordSlot, ...]:
        """返回槽的不可变快照。"""
        return self._parsed.slots

    @property
    def page_id(self) -> int | None:
        """返回仅用于错误定位的页号；DataPage 不负责分配页号。"""
        return self._page_id

    def to_bytes(self) -> bytes:
        """返回独立的完整页面副本，并在返回前再次检查页结构。"""
        data = bytes(self._data)
        _validate_page(data, page_id=self._page_id)
        return data

    def available_space(self) -> int:
        """返回记录区和槽区之间尚未使用的连续空间。"""
        return self.header.free_end - self.header.free_start

    def can_insert(self, record: bytes) -> bool:
        """判断一条记录连同新槽能否放入本页，不修改页面。"""
        _validate_record(record)
        return len(record) + RECORD_SLOT_SIZE <= self.available_space()

    def insert(self, record: bytes) -> int | None:
        """追加一条记录并返回新 slot_id；放不下时返回 None。"""
        _validate_record(record)
        header = self.header
        required = len(record) + RECORD_SLOT_SIZE
        if required > self.available_space():
            return None

        slot_id = header.slot_count
        record_offset = header.free_start
        slot_offset = header.free_end - RECORD_SLOT_SIZE
        self._data[record_offset:record_offset + len(record)] = record
        _SLOT_STRUCT.pack_into(
            self._data,
            slot_offset,
            record_offset,
            len(record),
            int(SlotState.LIVE),
        )
        updated = DataPageHeader(
            header.table_id,
            header.next_page_id,
            header.slot_count + 1,
            header.free_start + len(record),
            slot_offset,
            header.live_count + 1,
        )
        _write_header(self._data, updated)
        self._refresh()
        return slot_id

    def record(self, slot_id: int) -> bytes | None:
        """读取一个槽；删除槽返回 None，越界统一报 SLOT_ID_INVALID。"""
        slot = self._slot(slot_id)
        if slot.state is SlotState.DELETED:
            return None
        return bytes(self._data[slot.offset:slot.offset + slot.length])

    def iter_records(
        self, *, include_deleted: bool = False
    ) -> Iterator[tuple[int, bytes, SlotState]]:
        """按 slot_id 顺序返回记录；默认跳过已删除槽。"""
        for slot_id, slot in enumerate(self.slots):
            if slot.state is SlotState.DELETED and not include_deleted:
                continue
            yield (
                slot_id,
                bytes(self._data[slot.offset:slot.offset + slot.length]),
                slot.state,
            )

    def delete(self, slot_id: int) -> bool:
        """把 LIVE 槽标为 DELETED；重复删除返回 False，不移动其他记录。"""
        slot = self._slot(slot_id)
        if slot.state is SlotState.DELETED:
            return False
        slot_offset = _slot_offset(slot_id)
        # 状态在槽的第 5 个字节；保留的后三字节保持原样。
        self._data[slot_offset + 4] = int(SlotState.DELETED)
        header = self.header
        _write_header(
            self._data,
            DataPageHeader(
                header.table_id,
                header.next_page_id,
                header.slot_count,
                header.free_start,
                header.free_end,
                header.live_count - 1,
            ),
        )
        self._refresh()
        return True

    def set_next_page_id(self, next_page_id: int) -> None:
        """修改页链后继；禁止指向自己、保留页或超出页号范围。"""
        _validate_next_page(next_page_id, page_id=self._page_id)
        header = self.header
        _write_header(
            self._data,
            DataPageHeader(
                header.table_id,
                next_page_id,
                header.slot_count,
                header.free_start,
                header.free_end,
                header.live_count,
            ),
        )
        self._refresh()

    def reset_records(self) -> None:
        """清空记录区域和槽目录，但保留 table_id 与 next_page_id。"""
        header = self.header
        self._data[:] = bytes(PAGE_SIZE)
        _write_header(
            self._data,
            DataPageHeader(
                header.table_id,
                header.next_page_id,
                0,
                DATA_PAGE_HEADER_SIZE,
                PAGE_SIZE,
                0,
            ),
        )
        self._refresh()

    def _slot(self, slot_id: int) -> RecordSlot:
        if type(slot_id) is not int or not 0 <= slot_id < len(self.slots):
            _raise(
                errors.SLOT_ID_INVALID,
                "槽号超出当前页面范围",
                "DataPage.slot",
                page_id=self._page_id,
                slot_id=slot_id if type(slot_id) is int else repr(slot_id),
                slot_count=len(self.slots),
            )
        return self.slots[slot_id]

    def _refresh(self) -> None:
        """修改后重新解析，尽早发现实现自身写出的非法页。"""
        self._parsed = _parse_validated(self._data)


def new_page(table_id: int, next_page_id: int = INVALID_PAGE_ID) -> bytes:
    """模块级创建入口，方便不需要长期持有 DataPage 对象的调用方使用。"""
    return DataPage.empty(table_id, next_page_id).to_bytes()


def parse_page(
    data: bytes,
    *,
    page_id: int | None = None,
    expected_table_id: int | None = None,
) -> ParsedDataPage:
    """解析并校验页面，返回不可变的页头和槽快照。"""
    return DataPage(
        data, page_id=page_id, expected_table_id=expected_table_id
    )._parsed


def insert_record(
    data: bytes, record: bytes, *, page_id: int | None = None
) -> tuple[bytes, int | None]:
    """在页面副本中追加记录，返回新页面和槽号。"""
    page = DataPage(data, page_id=page_id)
    slot_id = page.insert(record)
    return page.to_bytes(), slot_id


def delete_record(
    data: bytes, slot_id: int, *, page_id: int | None = None
) -> tuple[bytes, bool]:
    """在页面副本中标记槽删除，返回新页面和是否首次删除。"""
    page = DataPage(data, page_id=page_id)
    deleted = page.delete(slot_id)
    return page.to_bytes(), deleted


def set_next_page_id(
    data: bytes, next_page_id: int, *, page_id: int | None = None
) -> bytes:
    """在页面副本中修改 next_page_id。"""
    page = DataPage(data, page_id=page_id)
    page.set_next_page_id(next_page_id)
    return page.to_bytes()


def reset_records(data: bytes, *, page_id: int | None = None) -> bytes:
    """在页面副本中清空所有记录并保留页身份和后继指针。"""
    page = DataPage(data, page_id=page_id)
    page.reset_records()
    return page.to_bytes()


def _validate_page(
    data: bytes,
    *,
    page_id: int | None,
    expected_table_id: int | None = None,
) -> bytes:
    """严格验证当前页面；所有损坏统一转换为 PAGE_CORRUPTED。"""
    if type(data) is not bytes:
        _raise(
            errors.INVALID_ARGUMENT,
            "页面必须是 bytes",
            "DataPage.parse",
            field="data",
            expected="bytes",
            actual=type(data).__name__,
        )
    if len(data) != PAGE_SIZE:
        _raise(
            errors.PAGE_CORRUPTED,
            "页面长度不是 4096 字节",
            "DataPage.parse",
            page_id=page_id,
            expected=PAGE_SIZE,
            actual=len(data),
        )
    fields = _HEADER_STRUCT.unpack_from(data)
    (
        magic,
        version,
        page_type,
        table_id,
        next_page_id,
        slot_count,
        free_start,
        free_end,
        reserved_short,
        live_count,
        reserved_int,
    ) = fields

    if magic != DATA_PAGE_MAGIC:
        _corrupt(page_id, "magic", DATA_PAGE_MAGIC.hex(), magic.hex())
    if version != DATA_PAGE_VERSION:
        _corrupt(page_id, "version", DATA_PAGE_VERSION, version)
    if page_type != DATA_PAGE_TYPE:
        _corrupt(page_id, "page_type", DATA_PAGE_TYPE, page_type)
    if reserved_short != 0 or reserved_int != 0:
        _corrupt(page_id, "reserved", "all zero", "nonzero")
    try:
        DataPageHeader(
            table_id, next_page_id, slot_count, free_start, free_end, live_count
        )
    except (TypeError, ValueError) as error:
        _corrupt(page_id, "header", "valid data-page bounds", str(error))
    if next_page_id != INVALID_PAGE_ID and next_page_id < FIRST_ALLOCATABLE_PAGE_ID:
        _corrupt(
            page_id,
            "next_page_id",
            "page >= 2 or INVALID_PAGE_ID",
            next_page_id,
        )
    if page_id is not None:
        _validate_page_id(page_id, "page_id")
        if next_page_id == page_id:
            _corrupt(page_id, "next_page_id", "not self", next_page_id)
    if expected_table_id is not None:
        _validate_identity(expected_table_id, "expected_table_id", allow_catalog=True)
        if table_id != expected_table_id:
            _corrupt(page_id, "table_id", expected_table_id, table_id)

    ranges: list[tuple[int, int, int]] = []
    slots: list[RecordSlot] = []
    for slot_id in range(slot_count):
        record_offset, length, raw_state = _SLOT_STRUCT.unpack_from(
            data, _slot_offset(slot_id)
        )
        slot_offset = _slot_offset(slot_id)
        if any(data[slot_offset + 5:slot_offset + RECORD_SLOT_SIZE]):
            _corrupt(
                page_id,
                f"slot[{slot_id}].reserved",
                "all zero",
                "nonzero",
            )
        if raw_state not in (int(SlotState.LIVE), int(SlotState.DELETED)):
            _corrupt(
                page_id,
                f"slot[{slot_id}].state",
                "LIVE 或 DELETED",
                raw_state,
            )
        try:
            slot = RecordSlot(record_offset, length, SlotState(raw_state))
        except (TypeError, ValueError) as error:
            _corrupt(page_id, f"slot[{slot_id}]", "合法记录槽", str(error))
        if record_offset + length > free_start:
            _corrupt(
                page_id,
                f"slot[{slot_id}]",
                "记录不能越过 free_start",
                [record_offset, length, free_start],
            )
        ranges.append((record_offset, record_offset + length, slot_id))
        slots.append(slot)

    ordered_ranges = sorted(ranges)
    for (_, left_end, left_id), (right_start, _, right_id) in zip(
        ordered_ranges, ordered_ranges[1:]
    ):
        if right_start < left_end:
            _corrupt(
                page_id,
                "slot ranges",
                "记录区间不重叠",
                [left_id, right_id],
            )
    if sum(slot.state is SlotState.LIVE for slot in slots) != live_count:
        _corrupt(page_id, "live_count", "等于 LIVE 槽数量", live_count)
    return data


def _parse_validated(data: bytes | bytearray) -> ParsedDataPage:
    """在已验证的页面上构造轻量快照；调用方不得修改传入对象。"""
    raw = bytes(data)
    fields = _HEADER_STRUCT.unpack_from(raw)
    header = DataPageHeader(
        fields[3], fields[4], fields[5], fields[6], fields[7], fields[9]
    )
    slots = tuple(
        RecordSlot(*_unpack_slot(raw, slot_id))
        for slot_id in range(header.slot_count)
    )
    return ParsedDataPage(header, slots)


def _unpack_slot(data: bytes, slot_id: int) -> tuple[int, int, SlotState]:
    offset, length, state = _SLOT_STRUCT.unpack_from(data, _slot_offset(slot_id))
    return offset, length, SlotState(state)


def _encode_header(header: DataPageHeader) -> bytes:
    data = bytearray(PAGE_SIZE)
    _write_header(data, header)
    return bytes(data)


def _write_header(data: bytearray, header: DataPageHeader) -> None:
    _HEADER_STRUCT.pack_into(
        data,
        0,
        DATA_PAGE_MAGIC,
        DATA_PAGE_VERSION,
        DATA_PAGE_TYPE,
        header.table_id,
        header.next_page_id,
        header.slot_count,
        header.free_start,
        header.free_end,
        0,
        header.live_count,
        0,
    )


def _slot_offset(slot_id: int) -> int:
    return PAGE_SIZE - (slot_id + 1) * RECORD_SLOT_SIZE


def _validate_record(record: bytes) -> None:
    if type(record) is not bytes:
        _raise(
            errors.INVALID_ARGUMENT,
            "记录必须是 bytes",
            "DataPage.record",
            field="record",
            expected="bytes",
            actual=type(record).__name__,
        )
    if len(record) == 0:
        _raise(
            errors.INVALID_ARGUMENT,
            "记录不能为空",
            "DataPage.record",
            field="record",
            expected="1 至 4056 字节",
            actual=0,
        )
    if len(record) > MAX_RECORD_SIZE:
        _raise(
            errors.ROW_TOO_LARGE,
            "记录长度超过单页可容纳范围",
            "DataPage.record",
            encoded_size=len(record),
            max_size=MAX_RECORD_SIZE,
        )


def _validate_identity(value: object, field: str, *, allow_catalog: bool) -> None:
    minimum = 0 if allow_catalog else 1
    if type(value) is not int or not minimum <= value <= 0xFFFFFFFE:
        _raise(
            errors.INVALID_ARGUMENT,
            "页身份编号不合法",
            "DataPage.identity",
            field=field,
            expected=f"{minimum}..0xFFFFFFFE",
            actual=repr(value),
        )


def _validate_page_id(value: object, field: str) -> None:
    if type(value) is not int or not 0 <= value <= MAX_PAGE_ID:
        _raise(
            errors.PAGE_ID_INVALID,
            "页号不合法",
            "DataPage.identity",
            field=field,
            expected=f"0..{MAX_PAGE_ID}",
            actual=repr(value),
        )
    if value == 0:
        _raise(
            errors.RESERVED_PAGE,
            "文件头 page 0 不能作为数据页",
            "DataPage.identity",
            page_id=value,
        )


def _validate_next_page(value: object, *, page_id: int | None) -> None:
    if type(value) is not int or not 0 <= value <= INVALID_PAGE_ID:
        _raise(
            errors.PAGE_ID_INVALID,
            "后继页号不合法",
            "DataPage.next_page_id",
            field="next_page_id",
            expected=f"0..{INVALID_PAGE_ID}",
            actual=repr(value),
        )
    if value not in (INVALID_PAGE_ID,) and value < FIRST_ALLOCATABLE_PAGE_ID:
        _raise(
            errors.RESERVED_PAGE,
            "数据页后继不能指向 page 0 或 page 1",
            "DataPage.next_page_id",
            page_id=value,
        )
    if page_id is not None and value == page_id:
        _raise(
            errors.PAGE_CORRUPTED,
            "页不能指向自己",
            "DataPage.next_page_id",
            page_id=page_id,
            next_page_id=value,
        )


def _corrupt(
    page_id: int | None, field: str, expected: object, actual: object
) -> None:
    context = {"field": field, "expected": expected, "actual": actual}
    if page_id is not None:
        context["page_id"] = page_id
    _raise(errors.PAGE_CORRUPTED, "数据页结构损坏", "DataPage.parse", **context)


def _raise(code: str, message: str, operation: str, **context: object) -> None:
    """统一构造 STORAGE 阶段错误，避免向上层泄漏 struct/索引异常。"""
    # DbError.context 不允许 None，因此未知 page_id 直接省略。
    clean = {key: value for key, value in context.items() if value is not None}
    clean["operation"] = operation
    raise errors.DbError(errors.ErrorStage.STORAGE, code, message, None, clean)


def _require_int_range(name: str, value: object, minimum: int, maximum: int) -> None:
    """保留旧值对象的轻量构造校验；页面 bytes 入口使用结构化错误。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


__all__ = [
    "DATA_PAGE_MAGIC",
    "DATA_PAGE_VERSION",
    "DATA_PAGE_TYPE",
    "DATA_PAGE_HEADER_SIZE",
    "RECORD_SLOT_SIZE",
    "MAX_RECORD_SIZE",
    "SlotState",
    "DataPageHeader",
    "RecordSlot",
    "ParsedDataPage",
    "DataPage",
    "new_page",
    "parse_page",
    "insert_record",
    "delete_record",
    "set_next_page_id",
    "reset_records",
]
