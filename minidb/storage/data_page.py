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
from minidb.core.records import RowSlot


DATA_PAGE_MAGIC = b"MDPG"
LEGACY_DATA_PAGE_VERSION = 1
DATA_PAGE_VERSION = FORMAT_VERSION
DATA_PAGE_VERSION_V2 = 2
DATA_PAGE_TYPE = 1
DATA_PAGE_HEADER_SIZE = 32
RECORD_SLOT_SIZE = 8
MAX_RECORD_SIZE = PAGE_SIZE - DATA_PAGE_HEADER_SIZE - RECORD_SLOT_SIZE
MAX_SLOT_GENERATION = 0xFFFFFF

# 显式使用小端序和标准大小，避免不同平台的本机对齐规则改变磁盘格式。
_HEADER_STRUCT = Struct("<4sHHIIHHHHII")
_SLOT_PREFIX_STRUCT = Struct("<HHB")


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
    version: int = DATA_PAGE_VERSION

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
        if self.version not in (LEGACY_DATA_PAGE_VERSION, DATA_PAGE_VERSION_V2):
            raise ValueError("version must identify a supported data-page format")
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
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, SlotState):
            raise TypeError("state must be a SlotState")
        _require_int_range("generation", self.generation, 0, MAX_SLOT_GENERATION)
        if self.state is SlotState.DELETED and self.offset == self.length == 0:
            return
        _require_int_range("offset", self.offset, DATA_PAGE_HEADER_SIZE, 0xFFFF)
        _require_int_range("length", self.length, 1, 0xFFFF)


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
        version: int = DATA_PAGE_VERSION,
    ) -> "DataPage":
        """创建一张没有记录的合法数据页。"""
        _validate_identity(table_id, "table_id", allow_catalog=True)
        _validate_next_page(next_page_id, page_id=page_id)
        _validate_data_page_version(version)
        header = DataPageHeader(
            table_id, next_page_id, 0, DATA_PAGE_HEADER_SIZE, PAGE_SIZE, 0, version
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
        """判断记录能否在必要的压缩后放入本页，不修改页面。"""
        _validate_record(record)
        reusable = self._reusable_slot_id()
        slot_bytes = 0 if reusable is not None else RECORD_SLOT_SIZE
        if len(record) + slot_bytes <= self.available_space():
            return True
        if self.header.version != DATA_PAGE_VERSION_V2:
            return False
        live_bytes = sum(
            slot.length for slot in self.slots if slot.state is SlotState.LIVE
        )
        compact_space = self.header.free_end - DATA_PAGE_HEADER_SIZE - live_bytes
        return len(record) + slot_bytes <= compact_space

    def insert(self, record: bytes) -> RowSlot | None:
        """插入记录并返回slot_id；v2优先复用删除槽并在需要时压缩。"""
        _validate_record(record)
        if not self.can_insert(record):
            return None
        candidate = DataPage(self.to_bytes(), page_id=self._page_id)
        reusable = candidate._reusable_slot_id()
        required = len(record) + (0 if reusable is not None else RECORD_SLOT_SIZE)
        if required > candidate.available_space():
            candidate.compact()
        row_slot = candidate._insert_contiguous(record, reusable)
        self._data = candidate._data
        self._parsed = candidate._parsed
        return row_slot

    def record(self, slot_id: int, generation: int | None = None) -> bytes | None:
        """读取一个槽；generation用于拒绝已经失效的物理定位。"""
        slot = self._slot(slot_id)
        self._check_generation(slot_id, slot, generation)
        if slot.state is SlotState.DELETED:
            if generation is not None and self.header.version == DATA_PAGE_VERSION_V2:
                self._stale_row(slot_id, generation, slot.generation, "record")
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

    def delete(self, slot_id: int, generation: int | None = None) -> bool:
        """标记删除；携带generation的重复或过期删除统一报STALE_ROW。"""
        if self.header.version == DATA_PAGE_VERSION_V2 and generation is None:
            _raise(
                errors.INVALID_ARGUMENT,
                "v2数据页删除必须提供generation",
                "DataPage.delete",
                field="generation",
                expected="1..0xFFFFFF",
                actual="None",
            )
        slot = self._slot(slot_id)
        self._check_generation(slot_id, slot, generation)
        if slot.state is SlotState.DELETED:
            if generation is not None and self.header.version == DATA_PAGE_VERSION_V2:
                self._stale_row(slot_id, generation, slot.generation, "delete")
            return False
        slot_offset = _slot_offset(slot_id)
        # 状态在槽的第5个字节，generation保持不变供后续安全复用。
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
                header.version,
            ),
        )
        self._refresh()
        return True

    def replace_record(self, slot_id: int, generation: int, record: bytes) -> bool:
        """替换活动记录；空间不足时返回False且原页面不变。"""
        _validate_record(record)
        if self.header.version != DATA_PAGE_VERSION_V2:
            _raise(
                errors.INVALID_ARGUMENT,
                "v1数据页不支持原槽替换",
                "DataPage.replace_record",
                version=self.header.version,
            )
        slot = self._slot(slot_id)
        self._check_generation(slot_id, slot, generation)
        if slot.state is SlotState.DELETED:
            self._stale_row(slot_id, generation, slot.generation, "replace_record")
        live_bytes = sum(
            len(record)
            if index == slot_id
            else candidate.length
            for index, candidate in enumerate(self.slots)
            if candidate.state is SlotState.LIVE
        )
        if DATA_PAGE_HEADER_SIZE + live_bytes > self.header.free_end:
            return False

        records = {
            index: (record if index == slot_id else self.record(index))
            for index, candidate in enumerate(self.slots)
            if candidate.state is SlotState.LIVE
        }
        self._rebuild_compacted(records)
        return True

    def compact(self) -> None:
        """压紧活动记录并清除删除负载，不改变slot_id和generation。"""
        if self.header.version != DATA_PAGE_VERSION_V2:
            _raise(
                errors.INVALID_ARGUMENT,
                "v1数据页不支持页内压缩",
                "DataPage.compact",
                version=self.header.version,
            )
        records = {
            index: self.record(index)
            for index, slot in enumerate(self.slots)
            if slot.state is SlotState.LIVE
        }
        self._rebuild_compacted(records)

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
                header.version,
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
                header.version,
            ),
        )
        self._refresh()

    def _reusable_slot_id(self) -> int | None:
        if self.header.version != DATA_PAGE_VERSION_V2:
            return None
        return next(
            (
                slot_id
                for slot_id, slot in enumerate(self.slots)
                if slot.state is SlotState.DELETED
                and slot.generation < MAX_SLOT_GENERATION
            ),
            None,
        )

    def _insert_contiguous(self, record: bytes, reusable: int | None) -> RowSlot:
        header = self.header
        slot_id = header.slot_count if reusable is None else reusable
        generation = 0
        if header.version == DATA_PAGE_VERSION_V2:
            generation = 1 if reusable is None else self.slots[reusable].generation + 1
        record_offset = header.free_start
        if reusable is None:
            slot_offset = header.free_end - RECORD_SLOT_SIZE
            slot_count = header.slot_count + 1
            free_end = slot_offset
        else:
            slot_offset = _slot_offset(reusable)
            slot_count = header.slot_count
            free_end = header.free_end
        self._data[record_offset:record_offset + len(record)] = record
        _pack_slot(
            self._data,
            slot_offset,
            record_offset,
            len(record),
            SlotState.LIVE,
            generation,
            header.version,
        )
        _write_header(
            self._data,
            DataPageHeader(
                header.table_id,
                header.next_page_id,
                slot_count,
                header.free_start + len(record),
                free_end,
                header.live_count + 1,
                header.version,
            ),
        )
        self._refresh()
        return RowSlot(slot_id, generation)

    def _rebuild_compacted(self, records: dict[int, bytes | None]) -> None:
        header = self.header
        rebuilt = bytearray(PAGE_SIZE)
        cursor = DATA_PAGE_HEADER_SIZE
        for slot_id, slot in enumerate(self.slots):
            value = records.get(slot_id)
            if slot.state is SlotState.LIVE:
                if value is None:
                    raise AssertionError("live slot record is missing during compaction")
                rebuilt[cursor:cursor + len(value)] = value
                offset, length = cursor, len(value)
                cursor += len(value)
            else:
                offset, length = 0, 0
            _pack_slot(
                rebuilt,
                _slot_offset(slot_id),
                offset,
                length,
                slot.state,
                slot.generation,
                header.version,
            )
        _write_header(
            rebuilt,
            DataPageHeader(
                header.table_id,
                header.next_page_id,
                header.slot_count,
                cursor,
                header.free_end,
                header.live_count,
                header.version,
            ),
        )
        _validate_page(bytes(rebuilt), page_id=self._page_id)
        self._data = rebuilt
        self._refresh()

    def _check_generation(
        self, slot_id: int, slot: RecordSlot, generation: int | None
    ) -> None:
        if generation is None:
            return
        _require_generation_argument(generation)
        if generation != slot.generation:
            self._stale_row(slot_id, generation, slot.generation, "generation_check")

    def _stale_row(
        self,
        slot_id: int,
        generation: int,
        actual_generation: int,
        operation: str,
    ) -> None:
        _raise(
            errors.STALE_ROW,
            "记录位置已经失效",
            f"DataPage.{operation}",
            page_id=self._page_id,
            slot_id=slot_id,
            expected_generation=generation,
            actual_generation=actual_generation,
        )

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


def new_page(
    table_id: int,
    next_page_id: int = INVALID_PAGE_ID,
    *,
    version: int = DATA_PAGE_VERSION,
) -> bytes:
    """模块级创建入口，方便不需要长期持有 DataPage 对象的调用方使用。"""
    return DataPage.empty(table_id, next_page_id, version=version).to_bytes()


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
) -> tuple[bytes, RowSlot | None]:
    """在页面副本中追加记录，返回新页面和槽号。"""
    page = DataPage(data, page_id=page_id)
    row_slot = page.insert(record)
    return page.to_bytes(), row_slot


def delete_record(
    data: bytes,
    slot_id: int,
    *,
    generation: int | None = None,
    page_id: int | None = None,
) -> tuple[bytes, bool]:
    """在页面副本中标记槽删除，返回新页面和是否首次删除。"""
    page = DataPage(data, page_id=page_id)
    deleted = page.delete(slot_id, generation)
    return page.to_bytes(), deleted


def replace_record(
    data: bytes,
    slot_id: int,
    generation: int,
    record: bytes,
    *,
    page_id: int | None = None,
) -> tuple[bytes, bool]:
    """在页面副本中替换记录，空间不足时原样返回。"""
    page = DataPage(data, page_id=page_id)
    replaced = page.replace_record(slot_id, generation, record)
    return page.to_bytes(), replaced


def compact_page(data: bytes, *, page_id: int | None = None) -> bytes:
    """在页面副本中压紧活动记录并清除删除负载。"""
    page = DataPage(data, page_id=page_id)
    page.compact()
    return page.to_bytes()


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
    if version not in (LEGACY_DATA_PAGE_VERSION, DATA_PAGE_VERSION_V2):
        _corrupt(
            page_id,
            "version",
            [LEGACY_DATA_PAGE_VERSION, DATA_PAGE_VERSION_V2],
            version,
        )
    if page_type != DATA_PAGE_TYPE:
        _corrupt(page_id, "page_type", DATA_PAGE_TYPE, page_type)
    if reserved_short != 0 or reserved_int != 0:
        _corrupt(page_id, "reserved", "all zero", "nonzero")
    try:
        DataPageHeader(
            table_id,
            next_page_id,
            slot_count,
            free_start,
            free_end,
            live_count,
            version,
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
        record_offset, length, raw_state, generation = _unpack_slot_fields(
            data, slot_id, version
        )
        slot_offset = _slot_offset(slot_id)
        if version == LEGACY_DATA_PAGE_VERSION and generation != 0:
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
            slot = RecordSlot(
                record_offset,
                length,
                SlotState(raw_state),
                generation,
            )
        except (TypeError, ValueError) as error:
            _corrupt(page_id, f"slot[{slot_id}]", "合法记录槽", str(error))
        if version == DATA_PAGE_VERSION_V2 and generation == 0:
            _corrupt(
                page_id,
                f"slot[{slot_id}].generation",
                "1..0xFFFFFF",
                generation,
            )
        has_offset = record_offset != 0
        has_length = length != 0
        if has_offset != has_length:
            _corrupt(
                page_id,
                f"slot[{slot_id}]",
                "offset和length同时为零或同时非零",
                [record_offset, length],
            )
        if not has_offset:
            if version == LEGACY_DATA_PAGE_VERSION:
                _corrupt(
                    page_id,
                    f"slot[{slot_id}]",
                    "v1槽必须保留原记录位置",
                    [record_offset, length],
                )
            if slot.state is not SlotState.DELETED:
                _corrupt(
                    page_id,
                    f"slot[{slot_id}]",
                    "只有DELETED槽可以清除负载位置",
                    raw_state,
                )
            slots.append(slot)
            continue
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
        fields[3], fields[4], fields[5], fields[6], fields[7], fields[9], fields[1]
    )
    slots = tuple(
        RecordSlot(*_unpack_slot(raw, slot_id, header.version))
        for slot_id in range(header.slot_count)
    )
    return ParsedDataPage(header, slots)


def _unpack_slot(
    data: bytes, slot_id: int, version: int
) -> tuple[int, int, SlotState, int]:
    offset, length, state, generation = _unpack_slot_fields(data, slot_id, version)
    return offset, length, SlotState(state), generation


def _unpack_slot_fields(
    data: bytes, slot_id: int, version: int
) -> tuple[int, int, int, int]:
    slot_offset = _slot_offset(slot_id)
    offset, length, state = _SLOT_PREFIX_STRUCT.unpack_from(data, slot_offset)
    generation = int.from_bytes(data[slot_offset + 5:slot_offset + 8], "little")
    return offset, length, state, generation


def _pack_slot(
    data: bytearray,
    slot_offset: int,
    offset: int,
    length: int,
    state: SlotState,
    generation: int,
    version: int,
) -> None:
    _SLOT_PREFIX_STRUCT.pack_into(data, slot_offset, offset, length, int(state))
    stored_generation = generation if version == DATA_PAGE_VERSION_V2 else 0
    data[slot_offset + 5:slot_offset + 8] = stored_generation.to_bytes(3, "little")


def _encode_header(header: DataPageHeader) -> bytes:
    data = bytearray(PAGE_SIZE)
    _write_header(data, header)
    return bytes(data)


def _write_header(data: bytearray, header: DataPageHeader) -> None:
    _HEADER_STRUCT.pack_into(
        data,
        0,
        DATA_PAGE_MAGIC,
        header.version,
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


def _validate_data_page_version(version: object) -> None:
    if type(version) is not int or version not in (
        LEGACY_DATA_PAGE_VERSION,
        DATA_PAGE_VERSION_V2,
    ):
        _raise(
            errors.INVALID_ARGUMENT,
            "数据页版本不受支持",
            "DataPage.empty",
            field="version",
            expected=[LEGACY_DATA_PAGE_VERSION, DATA_PAGE_VERSION_V2],
            actual=repr(version),
        )


def _require_generation_argument(generation: object) -> None:
    if type(generation) is not int or not 0 <= generation <= MAX_SLOT_GENERATION:
        _raise(
            errors.INVALID_ARGUMENT,
            "generation不合法",
            "DataPage.generation",
            field="generation",
            expected="0..0xFFFFFF",
            actual=repr(generation),
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
    "LEGACY_DATA_PAGE_VERSION",
    "DATA_PAGE_VERSION",
    "DATA_PAGE_VERSION_V2",
    "DATA_PAGE_TYPE",
    "DATA_PAGE_HEADER_SIZE",
    "RECORD_SLOT_SIZE",
    "MAX_RECORD_SIZE",
    "MAX_SLOT_GENERATION",
    "SlotState",
    "DataPageHeader",
    "RecordSlot",
    "ParsedDataPage",
    "DataPage",
    "new_page",
    "parse_page",
    "insert_record",
    "delete_record",
    "replace_record",
    "compact_page",
    "set_next_page_id",
    "reset_records",
]
