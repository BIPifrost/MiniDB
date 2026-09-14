"""事务协调测试的参与方替身；记录重载顺序，不实现生产目录和索引算法。"""
from types import SimpleNamespace


class StorageParticipant:
    def __init__(self, fm, pool, guard, events):
        self.file_manager, self.buffer_pool, self.guard = fm, pool, guard
        self.events = events
        self.active_scan_count = 0

    def close_scans(self):
        self.events.append('close_scans')
        self.active_scan_count = 0

    def abort_resources(self):
        self.events.append('abort_resources')


class CatalogParticipant:
    def __init__(self, storage, events):
        self._storage, self.events = storage, events
        self._services = SimpleNamespace(guard=storage.guard)
        self.generation = 0

    def reload_from_storage(self):
        self.events.append('catalog_reload')
        self.generation += 1


class IndexParticipant:
    def __init__(self, pool, storage, catalog, guard, events):
        self.buffer_pool, self.storage, self.catalog, self.guard = pool, storage, catalog, guard
        self.events = events

    def reload(self):
        self.events.append('index_reload')
