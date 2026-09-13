"""会话内页版本；与缓存帧分开保存，淘汰不会丢失版本。"""
from minidb.core import errors
from minidb.core.disk_types import PageSnapshot


class PageVersions:
    def __init__(self):
        self._owner = object()
        self._clock = 0
        self._revisions: dict[int, int] = {}

    def changed(self, page_id: int) -> None:
        self._clock += 1
        self._revisions[page_id] = self._clock

    def snapshot(self, page_id: int, data: bytes) -> PageSnapshot:
        if page_id not in self._revisions:
            self.changed(page_id)
        return PageSnapshot(page_id, data, self._revisions[page_id], _owner=self._owner)

    def check(self, snapshot: PageSnapshot) -> None:
        if not isinstance(snapshot, PageSnapshot):
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.INVALID_ARGUMENT,
                                 'snapshot 必须是 PageSnapshot')
        if (snapshot._owner is not self._owner or
                self._revisions.get(snapshot.page_id) != snapshot.revision):
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.STALE_PAGE,
                                 '页快照已经过期或属于其他缓存，请重新读取',
                                 context={'operation': 'write_if_current',
                                          'page_id': snapshot.page_id,
                                          'expected_revision': self._revisions.get(snapshot.page_id),
                                          'actual_revision': snapshot.revision})

    def invalidate_all(self) -> None:
        # 更换会话令牌使所有已发出的快照失效；版本计数不回退。
        self._owner = object()
        for page_id in self._revisions:
            self.changed(page_id)
