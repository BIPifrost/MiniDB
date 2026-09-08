"""STABLE P0 data-page fields; serialization algorithms are still pending.

Zhou Shengrong owns the record header and slot layout defined here.  The value
objects and constants are available for Liao Jie and Zhao Kaihang to review.

Waiting interfaces:

* ``core/disk_types.py`` now supplies shared page size, page-ID boundaries
  and format version. The header value checks use those definitions.
* ``storage/row_codec.py`` from Zhao Kaihang will provide encoded row bytes.
* ``storage/buffer_pool.py`` from Liao Jie will provide immutable 4096-byte
  page copies and accept complete updated pages.

Once those arrive, this module should add parsing, validation, insertion and
deletion behavior around these same fields.  It must not perform file I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


from minidb.core.disk_types import (
    PAGE_SIZE, FORMAT_VERSION, INVALID_PAGE_ID, CATALOG_ROOT_PAGE_ID,
)


DATA_PAGE_MAGIC = b"MDPG"
DATA_PAGE_VERSION = FORMAT_VERSION
DATA_PAGE_TYPE = 1
DATA_PAGE_HEADER_SIZE = 32
RECORD_SLOT_SIZE = 8


class SlotState(IntEnum):
    """On-disk state stored in the fifth byte of a record slot."""

    LIVE = 1
    DELETED = 2


@dataclass(frozen=True, slots=True)
class DataPageHeader:
    """Decoded logical fields from the fixed 32-byte data-page header."""

    table_id: int
    next_page_id: int
    slot_count: int
    free_start: int
    free_end: int
    live_count: int

    def __post_init__(self) -> None:
        _require_int_range("table_id", self.table_id, 0, 0xFFFFFFFE)
        _require_int_range("next_page_id", self.next_page_id, CATALOG_ROOT_PAGE_ID, INVALID_PAGE_ID)
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
    """Decoded logical fields from one fixed 8-byte record slot."""

    offset: int
    length: int
    state: SlotState

    def __post_init__(self) -> None:
        _require_int_range("offset", self.offset, DATA_PAGE_HEADER_SIZE, 0xFFFF)
        _require_int_range("length", self.length, 1, 0xFFFF)
        if not isinstance(self.state, SlotState):
            raise TypeError("state must be a SlotState")


def _require_int_range(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
