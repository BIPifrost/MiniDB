"""v2依赖未交付时的测试替身。不得导入生产代码，也不代表队友模块已实现。"""
from contextlib import contextmanager, ExitStack
from dataclasses import make_dataclass
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from minidb.compiler import ast
from minidb.core import errors
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.catalog.catalog_manager import CatalogServices

# 只给公共DbError登记测试所需的新code；不替换其冻结、上下文或原有阶段检查。
_NEW_CODES = frozenset("""
INVALID_TYPE_PARAMETER DUPLICATE_CONSTRAINT CONFLICTING_CONSTRAINT DUPLICATE_UPDATE_COLUMN
MISSING_REQUIRED_COLUMN VALUE_TOO_LONG NUMERIC_OUT_OF_RANGE NUMERIC_SCALE_MISMATCH INVALID_DATE
NOT_NULL_VIOLATION PRIMARY_KEY_VIOLATION UNIQUE_VIOLATION INDEX_NOT_FOUND INDEX_EXISTS
INDEX_KEY_TOO_LARGE INDEX_CORRUPTED RESOURCE_LIMIT FORMAT_VERSION_UNSUPPORTED
""".split())

_AST_FIELDS = {
    "TypeDecl": "kind length precision scale span",
    "ConstraintDecl": "kind value span",
    "ColumnDecl": "name type_decl constraints span",
    "LiteralExpr": "value type_spec span",
    "Assignment": "target value span",
    "UpdateStmt": "table assignments predicate span",
    "CreateIndexStmt": "name table column unique span",
    "DescribeStmt": "table span",
    "ExplainStmt": "statement span",
    "IsNullExpr": "operand negated op_span span",
}
_AST_TYPES = {name: make_dataclass(name, fields.split(), frozen=True, slots=True)
              for name, fields in _AST_FIELDS.items()}


@contextmanager
def pending_contracts():
    with ExitStack() as stack:
        stack.enter_context(patch.object(errors, "ALL_ERROR_CODES", errors.ALL_ERROR_CODES | _NEW_CODES))
        stack.enter_context(patch.dict(ast.__dict__, _AST_TYPES))
        yield ast


class SizeCodec:
    """仅模拟encoded_size，完全不实现二进制encode/decode。"""
    def __init__(self, fixed=24):
        self.fixed = fixed
        self.calls = []

    def encoded_size(self, value, spec):
        self.calls.append((value, spec))
        return self.fixed


class FakeScan:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return SimpleNamespace(values=next(self.rows))

    def close(self):
        self.closed = True


class CatalogStorage:
    """仅保留系统目录行及调用记录，不模拟数据页、事务回滚或文件格式。"""
    def __init__(self, state=TransactionState.ACTIVE, version=2):
        self.rows = {0: [], 0xFFFFFFFE: []}
        self.events, self.scans = [], []
        self.fail_write = False
        self.catalog_services = CatalogServices(
            TransactionGuard(state), SizeCodec(), lambda: version,
            self.write_catalog_rows, self.validate_index_root,
        )

    def validate_table_root(self, table):
        self.events.append(("validate_table", table.ref.table_id))

    def initialize_reserved_heap(self, table):
        self.events.append(("initialize", table.ref.table_id))

    def validate_index_root(self, index, table):
        self.events.append(("validate_index", index.index_id))

    def scan_rows(self, table):
        scan = FakeScan(self.rows[table.ref.table_id])
        self.scans.append(scan)
        return scan

    def write_catalog_rows(self, table, rows):
        self.events.append(("write", table.ref.table_id))
        if self.fail_write:
            raise OSError("测试目录写入失败")
        self.rows[table.ref.table_id].extend(rows)


class TokenSession:
    """只记录签发，不实现Session.apply/token消费；相关安全验收仍待真实Session。"""
    def __init__(self):
        self.session_id = uuid4()
        self.registered = []

    def register_validated_token(self, token):
        self.registered.append(token)


class Lookup:
    def __init__(self, hits=None):
        self.hits = hits or {}
        self.calls = []

    def probe(self, index, key):
        self.calls.append((index.index_id, key))
        return iter(self.hits.get((index.index_id, key), ()))


def catalog_view(catalog):
    return SimpleNamespace(generation=0, find_table=catalog.find_table,
                           indexes_for_table=catalog.indexes_for_table)

class ClosingHits:
    def __init__(self, values):
        self.values = iter(values)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.values)

    def close(self):
        self.closed = True
