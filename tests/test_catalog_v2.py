"""张振v2规则回归。依赖替身统一位于tests/fakes，不宣称端到端v2验收通过。"""
import unittest
from dataclasses import replace, FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import uuid4

from tests.fakes.v2_catalog_contracts import (
    pending_contracts, SizeCodec, CatalogStorage, TokenSession, Lookup, catalog_view,
)
from minidb.compiler import ast
from minidb.compiler.semantic import Semantic
from minidb.compiler.planner import Planner
from minidb.compiler.bound import (
    BoundColumn, BoundLiteral, BoundBinary, BoundIsNull, BoundAssignment, BoundUpdate,
    BoundExplain, BoundDescribe,
)
from minidb.compiler.plan import (
    validate_plan, SeqScanPlan, FilterPlan, ProjectPlan, UpdatePlan, DescribePlan,
    ExplainPlan, CreateIndexPlan, IndexScanPlan,
)
from minidb.compiler.index_selection import choose_index_scan
from minidb.core.expressions import ExprOp
from minidb.core.schema import (
    TypeSpec, DataType, DefaultSpec, ColumnDef, Schema, TableRef, TableDef,
    IndexDef, IndexOrigin, PendingIndexDef, IndexBounds,
)
from minidb.core.value_rules import normalize_value, comparison_allowed, assignment_allowed
from minidb.core.source import SourcePos, SourceSpan
from minidb.core.result import ResultColumn
from minidb.core.records import RowId, RowUpdate, UpdateBatch
from minidb.core.errors import DbError
from minidb.core.transaction import TransactionState
from minidb.catalog.catalog import Catalog, SYSTEM_CATALOG_TABLE
from minidb.catalog.catalog_rows import table_to_catalog_rows, index_to_catalog_row, catalog_from_rows
from minidb.catalog.catalog_manager import CatalogManager
from minidb.catalog.migration import schema_from_v1_table
from minidb.storage.constraints import (
    ConstraintValidator, ValidatedWriteToken, pending_constraint_indexes, validate_unique_stream,
)

SPAN = SourceSpan(SourcePos(1, 1, 0), SourcePos(1, 101, 100), "<v2-test>")
INT, BOOL = TypeSpec(DataType.INT), TypeSpec(DataType.BOOL)


def table_of(*columns):
    return TableDef(TableRef(1, "users", 3), Schema(columns))


def index_of(position=0, *, unique=True, origin=IndexOrigin.USER, identity=1):
    name = ("_sys_pk_1_" if origin is IndexOrigin.PRIMARY_KEY else "_sys_uq_1_") + str(position)
    if origin is IndexOrigin.USER:
        name = "ix_" + str(identity)
    return IndexDef(identity, name, 1, position, 3 + identity, unique, origin)


def name(text):
    return ast.NameRef(text, SPAN)


def literal(value, spec=INT):
    return ast.LiteralExpr(value, spec, SPAN)


class V2Case(unittest.TestCase):
    def setUp(self):
        self.contracts = pending_contracts()
        self.contracts.__enter__()
        self.addCleanup(self.contracts.__exit__, None, None, None)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(DbError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class TypeRulesTests(V2Case):
    def test_type_parameters_and_immutable(self):
        self.assertEqual(TypeSpec(DataType.VARCHAR).length, 1024)
        for params in ((DataType.INT, 1), (DataType.VARCHAR, 0), (DataType.VARCHAR, True),
                       (DataType.DECIMAL, None, 19, 0), (DataType.DECIMAL, None, 3, 4)):
            self.assertCode("INVALID_TYPE_PARAMETER", TypeSpec, *params)
        with self.assertRaises(FrozenInstanceError):
            INT.kind = DataType.BOOL

    def test_exact_python_types(self):
        for value, spec in ((True, INT), (1, BOOL), (datetime(2026, 1, 1), TypeSpec(DataType.DATE)),
                            ("2026-01-01", TypeSpec(DataType.DATE)), (1.2, TypeSpec(DataType.DECIMAL, precision=3, scale=1))):
            self.assertCode("TYPE_MISMATCH", normalize_value, value, spec, nullable=True)
        self.assertEqual(normalize_value(date(2024, 2, 29), TypeSpec(DataType.DATE), nullable=False), date(2024, 2, 29))

    def test_decimal_independent_of_context(self):
        spec = TypeSpec(DataType.DECIMAL, precision=18, scale=2)
        with localcontext() as context:
            context.prec = 2
            actual = normalize_value(Decimal("1234567890123456.7800"), spec, nullable=False)
        self.assertEqual(actual.as_tuple(), Decimal("1234567890123456.78").as_tuple())
        self.assertEqual(normalize_value(Decimal("-0.000"), spec, nullable=False).as_tuple(), Decimal("0.00").as_tuple())
        self.assertEqual(normalize_value(12, spec, nullable=False), Decimal("12.00"))

    def test_decimal_rejects_rounding_overflow_nonfinite(self):
        spec = TypeSpec(DataType.DECIMAL, precision=4, scale=2)
        self.assertCode("NUMERIC_SCALE_MISMATCH", normalize_value, Decimal("1.001"), spec, nullable=True)
        for value in ("100.00", "NaN", "Infinity", "1E+1000000"):
            self.assertCode("NUMERIC_OUT_OF_RANGE", normalize_value, Decimal(value), spec, nullable=True)

    def test_integer_bounds_and_varchar_characters(self):
        for value in (-(1 << 63), (1 << 63) - 1):
            self.assertEqual(normalize_value(value, INT, nullable=False), value)
        self.assertCode("NUMERIC_OUT_OF_RANGE", normalize_value, 1 << 63, INT, nullable=False)
        spec = TypeSpec(DataType.VARCHAR, 2)
        self.assertEqual(normalize_value("你好", spec, nullable=False), "你好")
        self.assertCode("VALUE_TOO_LONG", normalize_value, "你好呀", spec, nullable=False)

    def test_null_and_default_distinction(self):
        self.assertNotEqual(DefaultSpec(False, None), DefaultSpec(True, None))
        self.assertIsNone(normalize_value(None, INT, nullable=True))
        self.assertCode("NOT_NULL_VIOLATION", normalize_value, None, INT, nullable=False)
        self.assertCode("VALUE_TOO_LONG", ColumnDef, "value", TypeSpec(DataType.VARCHAR),
                        default=DefaultSpec(True, "中" * 86))

    def test_assignment_and_comparison_matrix(self):
        dec = TypeSpec(DataType.DECIMAL, precision=5, scale=2)
        self.assertTrue(assignment_allowed(INT, dec))
        self.assertFalse(assignment_allowed(dec, INT))
        self.assertTrue(comparison_allowed(ExprOp.LT, INT, dec))
        self.assertFalse(comparison_allowed(ExprOp.LT, BOOL, BOOL))
        self.assertTrue(comparison_allowed(ExprOp.EQ, None, None))
        self.assertFalse(comparison_allowed(ExprOp.EQ, INT, BOOL))

    def test_reserved_v2_roots_and_ids(self):
        self.assertCode("INVALID_ARGUMENT", TableRef, 1, "users", 2)
        self.assertCode("INVALID_ARGUMENT", TableRef, 0xFFFFFFFE, "users", 3)


class SemanticPlanTests(V2Case):
    def setUp(self):
        super().setUp()
        self.table = table_of(ColumnDef("id", INT, nullable=False),
                              ColumnDef("name", TypeSpec(DataType.VARCHAR, 10), default=DefaultSpec(True, "匿名")),
                              ColumnDef("flag", BOOL))
        self.catalog = Catalog((self.table,))

    def analyze(self, statement):
        return Semantic().analyze(statement, self.catalog)

    def test_partial_insert_defaults_and_null(self):
        result = self.analyze(ast.InsertStmt(name("users"), (name("id"),), (literal(7),), SPAN))
        self.assertEqual(result.row, (7, "匿名", None))
        self.assertEqual(Planner().build(result).row, result.row)

    def test_missing_required_and_duplicate_insert(self):
        stmt = ast.InsertStmt(name("users"), (name("name"),), (literal("x", TypeSpec(DataType.VARCHAR)),), SPAN)
        self.assertCode("MISSING_REQUIRED_COLUMN", self.analyze, stmt)
        stmt = ast.InsertStmt(name("users"), (name("id"), name("ID")), (literal(1),), SPAN)
        self.assertCode("DUPLICATE_INSERT_COLUMN", self.analyze, stmt)

    def test_create_constraints_and_automatic_index_pending(self):
        decl = ast.ColumnDecl(name("id"), ast.TypeDecl(DataType.INT, None, None, None, SPAN),
                              (ast.ConstraintDecl("PRIMARY_KEY", None, SPAN),), SPAN)
        bound = Semantic().analyze(ast.CreateTableStmt(name("books"), (decl,), SPAN), self.catalog)
        column = bound.schema.columns[0]
        self.assertTrue(column.primary_key and column.unique)
        self.assertFalse(column.nullable)
        pending = pending_constraint_indexes(bound.schema)
        self.assertIsNone(pending[0].name)
        self.assertFalse(hasattr(pending[0], "index_id"))

    def test_duplicate_and_conflicting_constraints(self):
        for constraints, code in ((("UNIQUE", "UNIQUE"), "DUPLICATE_CONSTRAINT"),
                                  (("PRIMARY_KEY", "NULL"), "CONFLICTING_CONSTRAINT")):
            decl = ast.ColumnDecl(name("id"), ast.TypeDecl(DataType.INT, None, None, None, SPAN),
                                  tuple(ast.ConstraintDecl(c, None, SPAN) for c in constraints), SPAN)
            self.assertCode(code, Semantic().analyze, ast.CreateTableStmt(name("books"), (decl,), SPAN), self.catalog)

    def test_where_null_and_is_null(self):
        result = self.analyze(ast.SelectStmt(name("users"), True, (), literal(None, None), SPAN))
        self.assertEqual(result.predicate.type_spec, BOOL)
        self.assertTrue(result.predicate.nullable)
        expr = ast.IsNullExpr(ast.IdentifierExpr("name", SPAN), False, SPAN, SPAN)
        result = self.analyze(ast.SelectStmt(name("users"), True, (), expr, SPAN))
        self.assertFalse(result.predicate.nullable)
        validate_plan(Planner().build(result))

    def test_null_comparison_and_incompatible_bool(self):
        expr = ast.BinaryExpr(ExprOp.EQ, literal(None, None), literal(None, None), SPAN, SPAN)
        result = self.analyze(ast.SelectStmt(name("users"), True, (), expr, SPAN))
        self.assertTrue(result.predicate.nullable)
        expr = ast.BinaryExpr(ExprOp.LT, literal(True, BOOL), literal(False, BOOL), SPAN, SPAN)
        self.assertCode("UNSUPPORTED_COMPARISON", self.analyze, ast.SelectStmt(name("users"), True, (), expr, SPAN))

    def test_invalid_date_literal(self):
        expr = ast.BinaryExpr(ExprOp.EQ, literal("2025-02-29", TypeSpec(DataType.DATE)),
                              literal(date(2025, 3, 1), TypeSpec(DataType.DATE)), SPAN, SPAN)
        self.assertCode("INVALID_DATE", self.analyze, ast.SelectStmt(name("users"), True, (), expr, SPAN))

    def test_update_binding_and_duplicate_targets(self):
        assignment = ast.Assignment(name("id"), ast.IdentifierExpr("id", SPAN), SPAN)
        bound = self.analyze(ast.UpdateStmt(name("users"), (assignment,), None, SPAN))
        plan = Planner().build(bound)
        self.assertIsInstance(plan, UpdatePlan)
        self.assertIsInstance(plan.child, SeqScanPlan)
        self.assertEqual(plan.assignments[0].value.index, 0)
        self.assertCode("DUPLICATE_UPDATE_COLUMN", self.analyze,
                        ast.UpdateStmt(name("users"), (assignment, assignment), None, SPAN))

    def test_describe_explain_and_create_index(self):
        self.assertIsInstance(Planner().build(self.analyze(ast.DescribeStmt(name("users"), SPAN))), DescribePlan)
        select = ast.SelectStmt(name("users"), True, (), None, SPAN)
        self.assertIsInstance(Planner().build(self.analyze(ast.ExplainStmt(select, SPAN))), ExplainPlan)
        create = ast.CreateIndexStmt(name("users_id"), name("users"), name("id"), True, SPAN)
        self.assertIsInstance(Planner().build(self.analyze(create)), CreateIndexPlan)

    def test_handmade_update_disallows_arithmetic_tree_and_type_mismatch(self):
        expr = BoundBinary(ExprOp.EQ, BoundLiteral(1, INT, SPAN), BoundLiteral(2, INT, SPAN),
                           BOOL, SPAN, SPAN)
        plan = UpdatePlan(self.table, SeqScanPlan(self.table, SPAN), (BoundAssignment(0, expr, SPAN),), SPAN)
        self.assertCode("INVALID_PLAN", validate_plan, plan)
        plan = replace(plan, assignments=(BoundAssignment(0, BoundColumn(1, self.table.schema.columns[1].type_spec,
                                                                       SPAN, True), SPAN),))
        self.assertCode("INVALID_PLAN", validate_plan, plan)

    def test_plan_cycle_and_wrong_nullability(self):
        predicate = BoundColumn(2, BOOL, SPAN, False)
        stream = FilterPlan(SeqScanPlan(self.table, SPAN), predicate, SPAN)
        plan = ProjectPlan(stream, (0,), (ResultColumn("id", DataType.INT),), SPAN)
        self.assertCode("INVALID_PLAN", validate_plan, plan)
        object.__setattr__(stream, "child", stream)
        self.assertCode("INVALID_PLAN", validate_plan, plan)


class CatalogTests(V2Case):
    def setUp(self):
        super().setUp()
        self.table = table_of(ColumnDef("id", INT, nullable=False, primary_key=True, unique=True),
                              ColumnDef("amount", TypeSpec(DataType.DECIMAL, precision=6, scale=2),
                                        default=DefaultSpec(True, Decimal("12.30"))))
        self.index = index_of(origin=IndexOrigin.PRIMARY_KEY)

    def test_directory_roundtrip_and_canonical_defaults(self):
        rows = table_to_catalog_rows(self.table)
        self.assertEqual(len(rows[0]), 15)
        self.assertEqual(rows[1][-2:], ("DECIMAL", "12.30"))
        result = catalog_from_rows(reversed(rows), (index_to_catalog_row(self.index),))
        self.assertEqual(result.tables, (self.table,))
        self.assertEqual(result.find_index(self.index.name), self.index)
        self.assertEqual(result.indexes_for_table(1), (self.index,))

    def test_corrupt_metadata_and_missing_auto_index(self):
        rows = table_to_catalog_rows(self.table)
        self.assertCode("CATALOG_CORRUPTED", catalog_from_rows, rows)
        self.assertCode("CATALOG_CORRUPTED", catalog_from_rows, rows + (rows[0],), (index_to_catalog_row(self.index),))
        bad = list(rows[1])
        bad[-1] = "12.300"
        self.assertCode("CATALOG_CORRUPTED", catalog_from_rows, (rows[0], tuple(bad)), (index_to_catalog_row(self.index),))
        self.assertCode("CATALOG_CORRUPTED", catalog_from_rows, (rows[0],), (index_to_catalog_row(self.index),))

    def test_index_anchor_must_not_overlap_heap(self):
        self.assertCode("INVALID_ARGUMENT", Catalog, (self.table,), (replace(self.index, root_page_id=3),))

    def test_max_user_tables(self):
        tables = tuple(TableDef(TableRef(i, "t" + str(i), i + 2), Schema((ColumnDef("id", INT),)))
                       for i in range(1, 130))
        self.assertCode("RESOURCE_LIMIT", Catalog, tables)

    def test_register_guard_and_failed_write_no_publish(self):
        storage = CatalogStorage(TransactionState.IDLE)
        manager = CatalogManager(storage, Catalog())
        self.assertCode("INVALID_TRANSACTION_STATE", manager.persist_and_register, self.table)
        self.assertFalse(storage.events)
        storage.catalog_services.guard.transition_to(TransactionState.PREPARING)
        storage.catalog_services.guard.transition_to(TransactionState.ACTIVE)
        storage.fail_write = True
        with self.assertRaises(OSError):
            manager.persist_and_register(self.table)
        self.assertIsNone(manager.find_table("users"))

    def test_register_table_index_then_reload_generation(self):
        storage = CatalogStorage()
        manager = CatalogManager(storage, Catalog())
        manager.persist_and_register(self.table)
        manager.persist_and_register_index(self.index)
        manager.validate_integrity()
        storage.catalog_services.guard.transition_to(TransactionState.COMMITTING)
        storage.catalog_services.guard.transition_to(TransactionState.IDLE)
        manager.reload_from_storage()
        self.assertEqual(manager.generation, 1)
        self.assertEqual(manager.find_table("users"), self.table)
        self.assertTrue(all(scan.closed for scan in storage.scans))

    def test_failed_reload_keeps_snapshot_and_generation(self):
        storage = CatalogStorage(TransactionState.IDLE)
        manager = CatalogManager(storage, Catalog((self.table,), (self.index,)))
        storage.rows[0] = [("bad",)]
        self.assertCode("CATALOG_CORRUPTED", manager.reload_from_storage)
        self.assertEqual(manager.generation, 0)
        self.assertEqual(manager.find_table("users"), self.table)
        self.assertTrue(all(scan.closed for scan in storage.scans))

    def test_new_file_initializes_both_reserved_roots(self):
        storage = CatalogStorage(TransactionState.BOOTSTRAP)
        CatalogManager.bootstrap_or_load(storage, True)
        self.assertEqual([event for event in storage.events if event[0] == "initialize"],
                         [("initialize", 0), ("initialize", 0xFFFFFFFE)])

    def test_version_gate_and_missing_services_no_io(self):
        storage = CatalogStorage(version=1)
        self.assertCode("FORMAT_VERSION_UNSUPPORTED", CatalogManager.bootstrap_or_load, storage, True)
        self.assertFalse(storage.events)
        with self.assertRaisesRegex(NotImplementedError, "尚未提供"):
            CatalogManager.bootstrap_or_load(SimpleNamespace(), True)

    def test_migration_metadata_v1_nonnull_no_invented_constraints(self):
        name_value, schema = schema_from_v1_table({"kind": "TABLE", "table_name": "USERS",
                                                  "columns": [{"name": "ID", "type": "INT"},
                                                              {"name": "TEXT", "type": "VARCHAR"}]})
        self.assertEqual(name_value, "users")
        self.assertEqual(schema.columns[1].type_spec.length, 1024)
        self.assertTrue(all(not c.nullable and not c.unique and not c.default.has_default for c in schema.columns))


class ConstraintTests(V2Case):
    def setUp(self):
        super().setUp()
        self.table = table_of(ColumnDef("id", INT, nullable=False, primary_key=True, unique=True),
                              ColumnDef("other", INT, unique=True))
        self.pk = index_of(origin=IndexOrigin.PRIMARY_KEY)
        self.uq = index_of(1, origin=IndexOrigin.UNIQUE_CONSTRAINT, identity=2)
        self.catalog = catalog_view(Catalog((self.table,), (self.pk, self.uq)))
        self.session, self.codec, self.key_codec = TokenSession(), SizeCodec(), SizeCodec(8)
        self.validator = ConstraintValidator(self.session, self.catalog, self.codec, self.key_codec)

    def test_insert_normalized_and_token_binding(self):
        pid = uuid4()
        result = self.validator.validate_insert(self.table, Lookup(), (1, None), pid)
        self.assertEqual(result.row, (1, None))
        self.assertIs(self.session.registered[0], result.token)
        self.assertEqual((result.token.session_id, result.token.catalog_generation, result.token.prepared_id),
                         (self.session.session_id, 0, pid))

    def test_token_cannot_be_constructed_or_changed(self):
        with self.assertRaises(TypeError):
            ValidatedWriteToken(uuid4(), 0, uuid4())
        token = self.validator.validate_insert(self.table, Lookup(), (1, 2), uuid4()).token
        with self.assertRaises(FrozenInstanceError):
            token.prepared_id = uuid4()

    def test_insert_conflicts_and_null_unique_skips_probe(self):
        lookup = Lookup({(1, 1): (RowId(3, 0, 1),)})
        self.assertCode("PRIMARY_KEY_VIOLATION", self.validator.validate_insert, self.table, lookup, (1, None), uuid4())
        self.assertFalse(self.session.registered)
        lookup = Lookup()
        self.validator.validate_insert(self.table, lookup, (2, None), uuid4())
        self.assertEqual(lookup.calls, [(1, 2)])

    def test_update_swap_and_self_assignment(self):
        a, b = RowId(3, 0, 1), RowId(3, 1, 1)
        batch = UpdateBatch((RowUpdate(a, (1, 11), (2, 22)), RowUpdate(b, (2, 22), (1, 11))))
        lookup = Lookup({(1, 1): (a,), (1, 2): (b,), (2, 11): (a,), (2, 22): (b,)})
        result = self.validator.validate_update(self.table, lookup, batch, uuid4())
        self.assertEqual(result.batch, batch)
        self.validator.validate_update(self.table, lookup, UpdateBatch((RowUpdate(a, (1, 11), (1, 11)),)), uuid4())

    def test_update_batch_and_external_conflicts(self):
        a, b, outside = RowId(3, 0, 1), RowId(3, 1, 1), RowId(3, 2, 1)
        batch = UpdateBatch((RowUpdate(a, (1, 11), (7, 11)), RowUpdate(b, (2, 22), (7, 22))))
        self.assertCode("PRIMARY_KEY_VIOLATION", self.validator.validate_update, self.table, Lookup(), batch, uuid4())
        batch = UpdateBatch((RowUpdate(a, (1, 11), (7, 11)),))
        self.assertCode("PRIMARY_KEY_VIOLATION", self.validator.validate_update, self.table,
                        Lookup({(1, 7): (outside,)}), batch, uuid4())

    def test_type_before_not_null_and_no_tokens_on_failure(self):
        self.assertCode("TYPE_MISMATCH", self.validator.validate_insert, self.table, Lookup(), (None, True), uuid4())
        self.assertCode("NOT_NULL_VIOLATION", self.validator.validate_insert, self.table, Lookup(), (None, 2), uuid4())
        self.assertFalse(self.session.registered)

    def test_missing_index_rejected(self):
        self.catalog.indexes_for_table = lambda tid: ()
        self.assertCode("CATALOG_CORRUPTED", self.validator.validate_insert, self.table, Lookup(), (1, 2), uuid4())

    def test_explicit_unique_index_on_boolean(self):
        table = table_of(ColumnDef("flag", BOOL))
        index = index_of()
        validator = ConstraintValidator(self.session, catalog_view(Catalog((table,), (index,))), self.codec, self.key_codec)
        self.assertCode("UNIQUE_VIOLATION", validator.validate_insert, table,
                        Lookup({(1, True): (RowId(3, 1, 1),)}), (True,), uuid4())

    def test_row_key_and_batch_resource_limits(self):
        self.key_codec.fixed = 513
        self.assertCode("INDEX_KEY_TOO_LARGE", self.validator.validate_insert, self.table, Lookup(), (1, 2), uuid4())
        self.key_codec.fixed, self.codec.fixed = 8, 4057
        self.assertCode("ROW_TOO_LARGE", self.validator.validate_insert, self.table, Lookup(), (1, 2), uuid4())
        self.codec.fixed = 4056
        batch = UpdateBatch(tuple(RowUpdate(RowId(3, i, 1), (i, i), (i, i)) for i in range(2100)))
        self.assertCode("RESOURCE_LIMIT", self.validator.validate_update, self.table, Lookup(), batch, uuid4())
        batch = UpdateBatch(tuple(RowUpdate(RowId(3, i, 1), (i, i), (i, i)) for i in range(10001)))
        self.assertCode("RESOURCE_LIMIT", self.validator.validate_update, self.table, Lookup(), batch, uuid4())

    def test_empty_update_batch_and_stale_generation(self):
        self.assertEqual(self.validator.validate_update(self.table, Lookup(), UpdateBatch(()), uuid4()).batch.items, ())
        def changing_probe(index, key):
            self.catalog.generation += 1
            return iter(())
        self.assertCode("INVALID_ARGUMENT", self.validator.validate_insert, self.table,
                        SimpleNamespace(probe=changing_probe), (1, 2), uuid4())

    def test_build_stream_duplicate_and_key_limit(self):
        pending = pending_constraint_indexes(self.table.schema)
        validate_unique_stream(self.table, pending, iter(((1, None), (2, None))), self.key_codec)
        self.assertCode("PRIMARY_KEY_VIOLATION", validate_unique_stream, self.table, pending,
                        iter(((1, 2), (1, 3))), self.key_codec)
        self.key_codec.fixed = 513
        self.assertCode("INDEX_KEY_TOO_LARGE", validate_unique_stream, self.table, pending, iter(((1, 2),)), self.key_codec)


class IndexSelectionTests(V2Case):
    def setUp(self):
        super().setUp()
        self.table = table_of(ColumnDef("a", INT), ColumnDef("b", INT))
        self.ix1 = index_of(unique=False)
        self.ix2 = index_of(1, identity=2)

    def comparison(self, index, op, value):
        return BoundBinary(op, BoundColumn(index, INT, SPAN, True), BoundLiteral(value, INT, SPAN),
                           BOOL, SPAN, SPAN, True)

    def test_merge_range_and_deterministic_equality_priority(self):
        lower, upper = self.comparison(0, ExprOp.GE, 10), self.comparison(0, ExprOp.LT, 20)
        predicate = BoundBinary(ExprOp.AND, lower, upper, BOOL, SPAN, SPAN, True)
        chosen = choose_index_scan(self.table, predicate, (self.ix1,), SPAN)
        self.assertEqual((chosen.lower.value, chosen.upper.value, chosen.lower_inclusive, chosen.upper_inclusive),
                         (10, 20, True, False))
        eq = self.comparison(1, ExprOp.EQ, 7)
        predicate = BoundBinary(ExprOp.AND, predicate, eq, BOOL, SPAN, SPAN, True)
        self.assertEqual(choose_index_scan(self.table, predicate, (self.ix1, self.ix2), SPAN).index, self.ix2)

    def test_null_index_and_retained_filter_plan(self):
        predicate = BoundIsNull(BoundColumn(0, INT, SPAN, True), False, SPAN, SPAN)
        chosen = choose_index_scan(self.table, predicate, (self.ix1,), SPAN)
        self.assertTrue(chosen.null_only)
        plan = ProjectPlan(FilterPlan(chosen, predicate, SPAN), (0,), (ResultColumn("a", DataType.INT),), SPAN)
        validate_plan(plan)

    def test_or_ne_and_column_comparisons_stay_sequential(self):
        a, b = self.comparison(0, ExprOp.EQ, 1), self.comparison(0, ExprOp.EQ, 2)
        for predicate in (BoundBinary(ExprOp.OR, a, b, BOOL, SPAN, SPAN, True),
                          self.comparison(0, ExprOp.NE, 1),
                          BoundBinary(ExprOp.EQ, BoundColumn(0, INT, SPAN, True),
                                      BoundColumn(1, INT, SPAN, True), BOOL, SPAN, SPAN, True)):
            self.assertIsNone(choose_index_scan(self.table, predicate, (self.ix1,), SPAN))

    def test_wrong_index_type_and_invalid_flags_fail_plan(self):
        chosen = choose_index_scan(self.table, self.comparison(0, ExprOp.EQ, 1), (self.ix1,), SPAN)
        bad = replace(chosen, lower=BoundLiteral(True, BOOL, SPAN))
        plan = ProjectPlan(bad, (0,), (ResultColumn("a", DataType.INT),), SPAN)
        self.assertCode("INVALID_PLAN", validate_plan, plan)
        self.assertCode("INVALID_ARGUMENT", IndexBounds, False, 1, False, False, None, False, False)



class AdditionalV2Tests(V2Case):
    def test_all_new_types_and_defaults_roundtrip(self):
        declarations = (
            ast.ColumnDecl(name("flag"), ast.TypeDecl(DataType.BOOL, None, None, None, SPAN),
                           (ast.ConstraintDecl("DEFAULT", literal(True, BOOL), SPAN),), SPAN),
            ast.ColumnDecl(name("born"), ast.TypeDecl(DataType.DATE, None, None, None, SPAN),
                           (ast.ConstraintDecl("DEFAULT", literal("2000-02-29", TypeSpec(DataType.DATE)), SPAN),), SPAN),
            ast.ColumnDecl(name("price"), ast.TypeDecl(DataType.DECIMAL, None, 6, 2, SPAN),
                           (ast.ConstraintDecl("DEFAULT", literal(12, INT), SPAN),), SPAN),
        )
        bound = Semantic().analyze(ast.CreateTableStmt(name("goods"), declarations, SPAN), Catalog())
        table = TableDef(TableRef(1, "goods", 3), bound.schema)
        expected = (
            (1, "goods", 3, 3, 0, "flag", "BOOL", -1, -1, -1, True, False, False, "BOOL", "TRUE"),
            (1, "goods", 3, 3, 1, "born", "DATE", -1, -1, -1, True, False, False, "DATE", "2000-02-29"),
            (1, "goods", 3, 3, 2, "price", "DECIMAL", -1, 6, 2, True, False, False, "DECIMAL", "12.00"),
        )
        self.assertEqual(table_to_catalog_rows(table), expected)
        self.assertEqual(catalog_from_rows(expected).tables, (table,))

    def test_explicit_null_does_not_use_default(self):
        table = table_of(ColumnDef("id", INT), ColumnDef("amount", INT, default=DefaultSpec(True, 8)))
        stmt = ast.InsertStmt(name("users"), (name("id"), name("amount")), (literal(1), literal(None, None)), SPAN)
        self.assertEqual(Semantic().analyze(stmt, Catalog((table,))).row, (1, None))

    def test_duplicate_update_reports_second_name_span(self):
        table = table_of(ColumnDef("id", INT))
        first = SourceSpan(SourcePos(1, 11, 10), SourcePos(1, 13, 12), "<v2-test>")
        second = SourceSpan(SourcePos(1, 31, 30), SourcePos(1, 33, 32), "<v2-test>")
        assignments = (ast.Assignment(ast.NameRef("id", first), literal(1), SPAN),
                       ast.Assignment(ast.NameRef("ID", second), literal(2), SPAN))
        error = self.assertCode("DUPLICATE_UPDATE_COLUMN", Semantic().analyze,
                                ast.UpdateStmt(name("users"), assignments, None, SPAN), Catalog((table,)))
        self.assertEqual(error.span, second)

    def test_type_parameter_error_has_sql_span(self):
        declaration = ast.ColumnDecl(name("text"), ast.TypeDecl(DataType.VARCHAR, 0, None, None, SPAN), (), SPAN)
        error = self.assertCode("INVALID_TYPE_PARAMETER", Semantic().analyze,
                                ast.CreateTableStmt(name("goods"), (declaration,), SPAN), Catalog())
        self.assertEqual(error.span, SPAN)

    def test_nondefault_constraint_payload_not_silently_ignored(self):
        declaration = ast.ColumnDecl(name("id"), ast.TypeDecl(DataType.INT, None, None, None, SPAN),
                                     (ast.ConstraintDecl("UNIQUE", literal(1), SPAN),), SPAN)
        self.assertCode("INVALID_ARGUMENT", Semantic().analyze,
                        ast.CreateTableStmt(name("goods"), (declaration,), SPAN), Catalog())

    def test_all_three_valued_logical_combinations_bind(self):
        table = table_of(ColumnDef("id", INT))
        catalog = Catalog((table,))
        # 这里只验类型与NULL传播；9格真值的实际求值由周升荣的evaluate验收。
        for op in (ExprOp.AND, ExprOp.OR):
            for left in (True, False, None):
                for right in (True, False, None):
                    expr = ast.BinaryExpr(op, literal(left, BOOL if left is not None else None),
                                          literal(right, BOOL if right is not None else None), SPAN, SPAN)
                    bound = Semantic().analyze(ast.SelectStmt(name("users"), True, (), expr, SPAN), catalog)
                    self.assertEqual(bound.predicate.type_spec, BOOL)
                    self.assertEqual(bound.predicate.nullable, left is None or right is None)
                    validate_plan(Planner().build(bound))

    def test_untyped_null_must_acquire_comparison_context(self):
        table = table_of(ColumnDef("id", INT))
        expr = BoundBinary(ExprOp.EQ, BoundColumn(0, INT, SPAN, True), BoundLiteral(None, None, SPAN),
                           BOOL, SPAN, SPAN, True)
        plan = ProjectPlan(FilterPlan(SeqScanPlan(table, SPAN), expr, SPAN), (0,),
                           (ResultColumn("id", DataType.INT),), SPAN)
        self.assertCode("INVALID_PLAN", validate_plan, plan)

    def test_explicit_unique_and_index_tie_break(self):
        table = table_of(ColumnDef("id", INT))
        column = BoundColumn(0, INT, SPAN, True)
        expr = BoundBinary(ExprOp.EQ, column, BoundLiteral(3, INT, SPAN), BOOL, SPAN, SPAN, True)
        indexes = (index_of(identity=3), index_of(identity=2), index_of(identity=1, unique=False))
        self.assertEqual(choose_index_scan(table, expr, indexes, SPAN).index.index_id, 2)

    def test_decimal_index_exact_int_boundary_and_nonrepresentable_fallback(self):
        spec = TypeSpec(DataType.DECIMAL, precision=5, scale=2)
        table = table_of(ColumnDef("price", spec))
        column = BoundColumn(0, spec, SPAN, True)
        expr = BoundBinary(ExprOp.EQ, column, BoundLiteral(12, INT, SPAN), BOOL, SPAN, SPAN, True)
        chosen = choose_index_scan(table, expr, (index_of(),), SPAN)
        self.assertEqual(chosen.lower.value.as_tuple(), Decimal("12.00").as_tuple())
        expr = replace(expr, right=BoundLiteral(Decimal("1.001"), TypeSpec(DataType.DECIMAL, precision=4, scale=3), SPAN))
        self.assertIsNone(choose_index_scan(table, expr, (index_of(),), SPAN))

    def test_catalog_rejects_noncanonical_missing_varchar_length(self):
        table = table_of(ColumnDef("text", TypeSpec(DataType.VARCHAR, 10)))
        row = list(table_to_catalog_rows(table)[0])
        row[7] = -1
        self.assertCode("CATALOG_CORRUPTED", catalog_from_rows, (tuple(row),))

    def test_unique_stream_limits_key_count_and_total_bytes(self):
        table = table_of(ColumnDef("id", INT))
        pending = (PendingIndexDef("ix_id", 0, True, IndexOrigin.USER),)
        self.assertCode("RESOURCE_LIMIT", validate_unique_stream, table, pending,
                        ((i,) for i in range(100001)), SizeCodec(8))
        self.assertCode("RESOURCE_LIMIT", validate_unique_stream, table, pending,
                        ((i,) for i in range(33000)), SizeCodec(512))

    def test_index_probe_close_on_conflict(self):
        from tests.fakes.v2_catalog_contracts import ClosingHits
        table = table_of(ColumnDef("id", INT, nullable=False, primary_key=True, unique=True))
        index = index_of(origin=IndexOrigin.PRIMARY_KEY)
        hits = ClosingHits((RowId(3, 0, 1),))
        lookup = SimpleNamespace(probe=lambda index, key: hits)
        session = TokenSession()
        validator = ConstraintValidator(session, catalog_view(Catalog((table,), (index,))), SizeCodec(), SizeCodec(8))
        self.assertCode("PRIMARY_KEY_VIOLATION", validator.validate_insert, table, lookup, (1,), uuid4())
        self.assertTrue(hits.closed)
        self.assertFalse(session.registered)


class RealContractTests(unittest.TestCase):
    """不用v2 AST/错误码替身：验证现有前端子集和实际依赖缺口。"""
    def test_existing_parser_to_new_semantic_and_planner(self):
        from minidb.compiler.lexer import Lexer
        from minidb.compiler.parser import Parser
        from minidb.core.source import SourceText
        source = SourceText("<real>", "CREATE TABLE users (id INT, name VARCHAR);")
        stmt = next(Parser().iter_statements(Lexer().scan(source)))
        bound = Semantic().analyze(stmt, Catalog())
        self.assertEqual(bound.schema.columns[1].type_spec.length, 1024)
        self.assertTrue(bound.schema.columns[0].nullable)
        validate_plan(Planner().build(bound))

    def test_missing_public_error_code_is_explicit_dependency_failure(self):
        from minidb.core import errors
        if "NUMERIC_SCALE_MISMATCH" in errors.ALL_ERROR_CODES:
            self.skipTest("公共错误码已经由负责人接齐")
        with self.assertRaisesRegex(NotImplementedError, "core.errors.*NUMERIC_SCALE_MISMATCH"):
            normalize_value(Decimal("1.001"), TypeSpec(DataType.DECIMAL, precision=5, scale=2), nullable=True)

    def test_production_storage_not_silently_treated_as_v2(self):
        from minidb.storage.storage_engine import StorageEngine
        if hasattr(StorageEngine, "catalog_services"):
            self.skipTest("StorageEngine的v2装配接口已经提供")
        with self.assertRaisesRegex(NotImplementedError, "catalog_services"):
            CatalogManager.bootstrap_or_load(object.__new__(StorageEngine), True)

    def test_actual_public_update_batch_integer_contract(self):
        table = table_of(ColumnDef("id", INT, nullable=False))
        session = TokenSession()
        validator = ConstraintValidator(session, catalog_view(Catalog((table,))), SizeCodec(), SizeCodec())
        batch = UpdateBatch((RowUpdate(RowId(3, 0, 1), (1,), (2,)),))
        self.assertEqual(validator.validate_update(table, Lookup(), batch, uuid4()).batch, batch)

if __name__ == "__main__":
    unittest.main()
