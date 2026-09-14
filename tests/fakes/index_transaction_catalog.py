"""固定DDL的测试目录；不模拟目录持久化，不能替代真实Catalog验收。"""
from types import SimpleNamespace


class FixedIndexCatalog:
    def __init__(self, storage, table, index):
        self._storage = storage
        self._services = SimpleNamespace(guard=storage.guard)
        self.table, self.index = table, index
        self.generation = 0

    def list_tables(self):
        return [self.table]

    def indexes_for_table(self, table_id):
        return (self.index,) if table_id == self.table.ref.table_id else ()

    def reload_from_storage(self):
        # DDL在这些测试中固定；真实检查恢复后的表根，索引检查交给IndexManager。
        self._storage.validate_table_root(self.table)
        self.generation += 1
