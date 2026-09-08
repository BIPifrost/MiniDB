"""Internal cache replacement ordering; no disk I/O, counters or logging.

BufferPool validates public arguments using DbError before calling this module.
Call record_access only after successful get/write or admission of a new page.
Do not call it for flush/stats. victim only peeks: after a dirty victim has
been written successfully, call remove. Failed writeback must not remove it.
"""

from collections import OrderedDict
from minidb.core.disk_types import MAX_PAGE_ID, PageId


class ReplacementPolicy:
    """Track resident page IDs, ordered from first to last eviction candidate.

    This is an internal helper, not the public BufferPool API. Invalid helper
    arguments raise standard programming errors; FileManager remains responsible
    for checking whether a page is allocated. Page 1 is allowed, page 0 is not.
    """

    def __init__(self, policy: str = 'lru') -> None:
        if type(policy) is not str or policy not in ('lru', 'fifo'):
            raise ValueError("policy must be 'lru' or 'fifo'")
        self._policy = policy
        self._pages: OrderedDict[PageId, None] = OrderedDict()

    @property
    def policy(self) -> str:
        return self._policy

    @staticmethod
    def _validate(page_id: PageId) -> None:
        if type(page_id) is not int:
            raise TypeError('page_id must be an int (not bool)')
        if not 1 <= page_id <= MAX_PAGE_ID:
            raise ValueError('page_id must be an ordinary cacheable page ID')

    def record_access(self, page_id: PageId) -> None:
        """Admit a new page or record a successful access to a resident page."""
        self._validate(page_id)
        if page_id not in self._pages:
            self._pages[page_id] = None
        elif self._policy == 'lru':
            self._pages.move_to_end(page_id)

    def victim(self) -> PageId | None:
        """Return the oldest candidate without removing or reordering it."""
        return next(iter(self._pages), None)

    def remove(self, page_id: PageId) -> bool:
        """Forget an evicted/freed page. A valid absent ID returns False."""
        self._validate(page_id)
        if page_id not in self._pages:
            return False
        del self._pages[page_id]
        return True

    def snapshot(self) -> tuple[PageId, ...]:
        """Return an immutable order snapshot without recording an access."""
        return tuple(self._pages)

    def __len__(self) -> int:
        return len(self._pages)
