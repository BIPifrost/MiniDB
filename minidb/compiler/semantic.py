"""v2语义与绑定：只查目录、不读业务页、不签发写token、不执行SQL。"""
from dataclasses import replace
from datetime import date
import re

from minidb.compiler._ast_validation import validate_ast, is_ast, literal_type
from minidb.compiler._checks import Check
from minidb.compiler.bound import (
    BoundColumn, BoundLiteral, BoundUnary, BoundBinary, BoundIsNull, BoundAssignment,
    BoundCreate, BoundInsert, BoundSelect, BoundDelete, BoundUpdate,
    BoundCreateIndex, BoundDescribe, BoundExplain,
)
from minidb.compiler.bound_validation import validate_bound
from minidb.core.schema import (
    TypeSpec, DataType, ColumnDef, Schema, DefaultSpec, NO_DEFAULT, MAX_USER_TABLES, _normalize_identifier,
)
from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.value_rules import normalize_value, assignment_allowed
from minidb.core._v2_contract import fail, require_method


class Semantic:
    def analyze(self, stmt, catalog):
        """先完整检查结构，再按最新目录绑定；EXPLAIN只递归绑定内部语句。"""
        validate_ast(stmt)
        if is_ast(stmt, "ExplainStmt"):
            result = BoundExplain(self.analyze(stmt.statement, catalog), stmt.span)
        else:
            table_name = stmt.table if any(is_ast(stmt, kind) for kind in
                                          ("UpdateStmt", "CreateIndexStmt", "DescribeStmt")) else stmt.table_name
            name = _name(table_name.text, table_name.span, table=True)
            table = catalog.find_table(name)
            if is_ast(stmt, "CreateTableStmt"):
                if table is not None:
                    _error("TABLE_EXISTS", "表已存在", table_name.span, table_name=name)
                if len(catalog.list_tables()) >= MAX_USER_TABLES:
                    fail("RESOURCE_LIMIT", "用户表超过128张", stage="EXECUTION", span=stmt.span,
                         limit=MAX_USER_TABLES)
                result = self._create(stmt, name)
            else:
                if table is None:
                    _error("TABLE_NOT_FOUND", "表不存在", table_name.span, table_name=name)
                Check("Semantic.analyze").table(table)
                if is_ast(stmt, "InsertStmt"):
                    result = self._insert(stmt, table)
                elif is_ast(stmt, "SelectStmt"):
                    result = self._select(stmt, table)
                elif is_ast(stmt, "DeleteStmt"):
                    result = BoundDelete(table, self._where(stmt.where, table), stmt.span)
                elif is_ast(stmt, "UpdateStmt"):
                    result = self._update(stmt, table)
                elif is_ast(stmt, "DescribeStmt"):
                    result = BoundDescribe(table, stmt.span)
                else:
                    index_name = _name(stmt.name.text, stmt.name.span, table=True)
                    if require_method(catalog, "find_index", "find_index(name: str) -> IndexDef | None")(index_name):
                        _error("INDEX_EXISTS", "索引名已存在", stmt.name.span, index=index_name)
                    index, _ = _column(table, _name(stmt.column.text, stmt.column.span), stmt.column.span)
                    result = BoundCreateIndex(index_name, table, index, stmt.unique, stmt.span)
        validate_bound(result)
        return result

    def _create(self, stmt, name):
        if not 1 <= len(stmt.columns) <= 64:
            _error("UNSUPPORTED_FEATURE", "每表要求1至64列", stmt.span, actual=len(stmt.columns))
        columns, seen = [], set()
        for declaration in stmt.columns:
            column_name = _name(declaration.name.text, declaration.name.span)
            if column_name in seen:
                _error("DUPLICATE_COLUMN", "重复列名", declaration.name.span, column_name=column_name)
            seen.add(column_name)
            if hasattr(declaration, "type_decl"):
                decl = declaration.type_decl
                typ = _located(decl.span, TypeSpec, decl.kind, decl.length, decl.precision, decl.scale)
                constraints = declaration.constraints
            else:
                typ, constraints = TypeSpec(declaration.data_type), ()
            settings = {}
            for constraint in constraints:
                # 待前端提供ConstraintDecl.kind；约定枚举名/字符串统一为这些大写名字。
                kind = getattr(constraint.kind, "name", constraint.kind)
                if kind not in ("PRIMARY_KEY", "UNIQUE", "NOT_NULL", "NULL", "DEFAULT"):
                    _error("UNSUPPORTED_FEATURE", "未知列约束", constraint.span, constraint=repr(kind))
                if kind in settings:
                    _error("DUPLICATE_CONSTRAINT", "列约束重复", constraint.span,
                           column=column_name, constraint=kind)
                if kind != "DEFAULT" and constraint.value is not None:
                    _error("INVALID_ARGUMENT", "只有DEFAULT约束可以携带值", constraint.span)
                settings[kind] = constraint
            if "NULL" in settings and ("NOT_NULL" in settings or "PRIMARY_KEY" in settings):
                _error("CONFLICTING_CONSTRAINT", "NULL与非空/主键冲突", declaration.span, column=column_name)
            primary = "PRIMARY_KEY" in settings
            nullable = not (primary or "NOT_NULL" in settings)
            default = NO_DEFAULT
            if "DEFAULT" in settings:
                value = settings["DEFAULT"].value
                if value is None:
                    _error("INVALID_ARGUMENT", "DEFAULT缺少LiteralExpr", settings["DEFAULT"].span)
                default = DefaultSpec(True, self._assignment_literal(value, typ, nullable, column_name).value)
            columns.append(_located(declaration.span, ColumnDef, column_name, typ, nullable, default,
                                    primary, primary or "UNIQUE" in settings))
        return BoundCreate(name, _located(stmt.span, Schema, tuple(columns)), stmt.span)

    def _insert(self, stmt, table):
        selected, seen = [], set()
        # v2检查顺序：先列重复/存在，再值数；之后统一按表定义顺序补齐行。
        for name_ref in stmt.columns:
            name = _name(name_ref.text, name_ref.span)
            if name in seen:
                _error("DUPLICATE_INSERT_COLUMN", "INSERT重复列", name_ref.span, column_name=name)
            seen.add(name)
            index, column = _column(table, name, name_ref.span)
            selected.append((index, column))
        if len(selected) != len(stmt.values):
            _error("VALUE_COUNT_MISMATCH", "INSERT列数和值数不一致", stmt.span,
                   expected=len(selected), actual=len(stmt.values))
        provided = {index: literal for (index, _), literal in zip(selected, stmt.values)}
        row = []
        for index, column in enumerate(table.schema.columns):
            if index in provided:
                value = self._assignment_literal(provided[index], column.type_spec, column.nullable, column.name).value
            elif column.default.has_default:
                value = column.default.value
            elif column.nullable:
                value = None
            else:
                _error("MISSING_REQUIRED_COLUMN", "缺少无默认值的非空列", stmt.span,
                       table=table.ref.name, column=column.name)
            row.append(value)
        return BoundInsert(table, tuple(row), stmt.span)

    def _select(self, stmt, table):
        from minidb.core.result import ResultColumn
        if stmt.select_all:
            indexes = tuple(range(len(table.schema.columns)))
        else:
            indexes = tuple(_column(table, _name(name.text, name.span), name.span)[0] for name in stmt.columns)
        # ResultColumn现有签名仍为(name,data_type)。待赵凯航升级结果元数据时补type_spec/nullable，
        # 此处不创建第二个ResultColumn类，完整类型可通过table+projection获取。
        columns = tuple(ResultColumn(table.schema.columns[i].name, table.schema.columns[i].data_type) for i in indexes)
        return BoundSelect(table, indexes, columns, self._where(stmt.where, table), stmt.span)

    def _update(self, stmt, table):
        targets, seen = [], set()
        for assignment in stmt.assignments:
            name = _name(assignment.target.text, assignment.target.span)
            if name in seen:
                _error("DUPLICATE_UPDATE_COLUMN", "UPDATE重复赋值列", assignment.target.span, column=name)
            seen.add(name)
            index, column = _column(table, name, assignment.target.span)
            targets.append((assignment, index, column))
        assignments = []
        for assignment, index, column in targets:
            if is_ast(assignment.value, "LiteralExpr"):
                value = self._assignment_literal(assignment.value, column.type_spec, column.nullable, column.name)
            else:
                value = self._expression(assignment.value, table)
                if not assignment_allowed(value.type_spec, column.type_spec):
                    _error("TYPE_MISMATCH", "列引用不能赋给目标类型", assignment.value.span, column=column.name)
                # 可空列赋给非空列、VARCHAR缩短及DECIMAL变精度，在prepare按每条旧行复核。
            assignments.append(BoundAssignment(index, value, assignment.span))
        return BoundUpdate(table, tuple(assignments), self._where(stmt.predicate, table), stmt.span)

    def _assignment_literal(self, node, target, nullable, column):
        value = self._literal(node)
        if not assignment_allowed(value.type_spec, target):
            _error("TYPE_MISMATCH", "常量不能赋给目标列", node.span, column=column)
        from minidb.core.errors import DbError
        try:
            normalized = normalize_value(value.value, target, nullable=nullable)
        except DbError as error:
            # 保留公共错误code/stage，构造同类带SQL位置的新错误，不改只读字段。
            raise DbError(error.stage, error.code, error.message, node.span,
                          {**error.context, "column": column}) from error
        return BoundLiteral(normalized, target, node.span)

    def _literal(self, node):
        typ, value = literal_type(node), node.value
        if typ is not None:
            if typ.kind is DataType.DATE and type(value) is str:
                try:
                    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
                        raise ValueError("要求YYYY-MM-DD")
                    value = date.fromisoformat(value)
                except ValueError:
                    _error("INVALID_DATE", "无效DATE字面量", node.span, text=value, reason="无效公历日期")
            value = _located(node.span, normalize_value, value, typ, nullable=True)
        return BoundLiteral(value, typ, node.span)

    def _where(self, expr, table):
        if expr is None:
            return None
        result = self._expression(expr, table)
        if isinstance(result, BoundLiteral) and result.type_spec is None:
            result = replace(result, type_spec=TypeSpec(DataType.BOOL))
        if result.type_spec != TypeSpec(DataType.BOOL):
            _error("CONDITION_NOT_BOOL", "WHERE要求BOOL或NULL", expr.span)
        return result

    def _expression(self, expr, table):
        # validate_ast已经排除循环；后序绑定不依赖Python递归深度，也不跳过短路分支。
        pending, completed = [(expr, False)], {}
        while pending:
            node, leaving = pending.pop()
            if id(node) in completed:
                continue
            if is_ast(node, "LiteralExpr"):
                result = self._literal(node)
            elif is_ast(node, "IdentifierExpr"):
                index, column = _column(table, _name(node.name, node.span), node.span)
                result = BoundColumn(index, column.type_spec, node.span, column.nullable)
            elif not leaving:
                pending.append((node, True))
                children = (node.left, node.right) if is_ast(node, "BinaryExpr") else (node.operand,)
                pending.extend((child, False) for child in reversed(children))
                continue
            elif is_ast(node, "IsNullExpr"):
                operand = completed[id(node.operand)]
                if isinstance(operand, BoundLiteral) and operand.type_spec is None:
                    operand = replace(operand, type_spec=TypeSpec(DataType.BOOL))
                result = BoundIsNull(operand, node.negated, node.op_span, node.span)
            else:
                children = [completed[id(child)] for child in
                            ((node.left, node.right) if is_ast(node, "BinaryExpr") else (node.operand,))]
                # 逻辑NULL绑定BOOL；比较NULL从另一侧取得完整TypeSpec。
                context = TypeSpec(DataType.BOOL) if node.op in (ExprOp.AND, ExprOp.OR, ExprOp.NOT) else next(
                    (child.type_spec for child in children if child.type_spec is not None), None)
                children = [replace(child, type_spec=context)
                            if isinstance(child, BoundLiteral) and child.type_spec is None else child for child in children]
                if resolve_result_type(node.op, tuple(child.type_spec for child in children)) is None:
                    _error("UNSUPPORTED_COMPARISON", "操作数类型不支持该运算", node.op_span, operator=node.op.name)
                nullable = any(child.nullable for child in children)
                typ = TypeSpec(DataType.BOOL)
                result = (BoundUnary(node.op, children[0], typ, node.op_span, node.span, nullable)
                          if len(children) == 1 else
                          BoundBinary(node.op, *children, typ, node.op_span, node.span, nullable))
            completed[id(node)] = result
        return completed[id(expr)]


def _name(text, span, *, table=False):
    if type(text) is str and len(text) > 64:
        _error("IDENTIFIER_TOO_LONG", "标识符超过64字符", span, name=text, limit=64)
    name = _normalize_identifier(text, "Semantic.analyze")
    if table and name.startswith("_sys_") or _is_keyword(name):
        _error("RESERVED_NAME", "保留名称不可用于用户对象", span, name=name)
    return name


def _is_keyword(name):
    from minidb.core.tokens import TokenKind
    return "KW_" + name.upper() in TokenKind.__members__


def _column(table, name, span):
    found = table.schema.find_column(name)
    if found is None:
        _error("COLUMN_NOT_FOUND", "列不存在", span, table_name=table.ref.name, column_name=name)
    return found


def _error(code, message, span, **context):
    fail(code, message, span=span, operation="Semantic.analyze", **context)


def _located(span, function, *args, **kwargs):
    """类型和目录规则不依赖SQL；在语义入口保留错误码并补上源位置。"""
    from minidb.core.errors import DbError
    try:
        return function(*args, **kwargs)
    except DbError as error:
        raise DbError(error.stage, error.code, error.message, span, dict(error.context)) from error
