"""张振模块的统一边界校验；与业务值错误区分，损坏计划统一INVALID_PLAN。"""
from minidb.core.schema import TypeSpec, Schema, TableDef, MAX_USER_TABLE_ID, _IDENTIFIER
from minidb.core._v2_contract import fail
from minidb.core.value_rules import normalize_value


class Check:
    def __init__(self, operation, *, plan=False):
        self.operation, self.plan = operation, plan

    def require(self, valid, field, expected, actual):
        if not valid:
            fail("INVALID_PLAN" if self.plan else "INVALID_ARGUMENT", f"{self.operation} 的 {field} 不合法",
                 stage="PLAN" if self.plan else "SEMANTIC", operation=self.operation,
                 field=field, expected=repr(expected), actual=repr(actual))

    def span(self, value, field="span", parent=None):
        from minidb.core.source import SourcePos, SourceSpan
        self.require(isinstance(value, SourceSpan), field, "SourceSpan", type(value).__name__)
        self.require(type(value.source_name) is str, field, "str source_name", value.source_name)
        for pos in (value.start, value.end):
            self.require(isinstance(pos, SourcePos), field, "SourcePos", type(pos).__name__)
            for key, minimum in (("line", 1), ("column", 1), ("offset", 0)):
                number = getattr(pos, key)
                self.require(type(number) is int and number >= minimum, field, key, number)
        start = (value.start.line, value.start.column, value.start.offset)
        end = (value.end.line, value.end.column, value.end.offset)
        self.require(_positions_in_order(start, end), field, "行列偏移一致", value)
        if parent is not None:
            self.require(isinstance(parent, SourceSpan), field, "父SourceSpan", parent)
            self.require(value.source_name == parent.source_name
                         and _positions_in_order((parent.start.line, parent.start.column, parent.start.offset), start)
                         and _positions_in_order(end, (parent.end.line, parent.end.column, parent.end.offset)),
                         field, "同源且包含于父范围", value)

    def schema(self, schema):
        self.require(isinstance(schema, Schema), "schema", "Schema", type(schema).__name__)

    def table(self, table):
        self.require(isinstance(table, TableDef), "table", "TableDef", type(table).__name__)
        self.require(1 <= table.ref.table_id <= MAX_USER_TABLE_ID, "table", "用户表", table.ref.name)

    def name(self, name):
        self.require(type(name) is str and _IDENTIFIER.fullmatch(name) is not None,
                     "name", "ASCII标识符", name)
        self.require(name == name.lower() and not name.startswith("_sys_"), "name", "用户小写名称", name)

    def value(self, value, type_spec, field="value", *, nullable=True):
        from minidb.core.errors import DbError
        self.require(isinstance(type_spec, TypeSpec) or value is None and type_spec is None,
                     field, "TypeSpec或未定型NULL", type_spec)
        if type_spec is None:
            return
        try:
            normalized = normalize_value(value, type_spec, nullable=nullable)
        except (DbError, NotImplementedError):
            # 值规则已判定非法；即使对应v2业务错误码未登记，损坏计划仍须
            # 使用现有INVALID_PLAN分类，不能泄漏为“校验功能尚未实现”。
            self.require(False, field, "符合类型的标准值", value)
        self.require(type(value) is type(normalized) and value == normalized, field, "已归一化值", value)
        if hasattr(value, "as_tuple"):
            self.require(value.as_tuple() == normalized.as_tuple(), field, "标准DECIMAL精度", value)

    def row(self, row, schema):
        self.require(type(row) is tuple and len(row) == len(schema.columns),
                     "row", "Schema顺序的完整tuple", row)
        for index, (value, column) in enumerate(zip(row, schema.columns)):
            self.value(value, column.type_spec, f"row[{index}]", nullable=column.nullable)

    def projection(self, table, indexes, columns):
        from minidb.core.result import ResultColumn
        self.require(type(indexes) is tuple and bool(indexes), "projection", "非空tuple", indexes)
        self.require(type(columns) is tuple and len(indexes) == len(columns),
                     "output_columns", "投影长度", columns)
        for index, output in zip(indexes, columns):
            self.require(type(index) is int and 0 <= index < len(table.schema.columns),
                         "projection", "有效列号", index)
            self.require(isinstance(output, ResultColumn), "output_columns", "ResultColumn", type(output).__name__)
            column = table.schema.columns[index]
            self.require(output.name == column.name and output.data_type is column.type_spec.kind,
                         "output_columns", "对应列名和类型", output)


def _positions_in_order(start, end):
    if any(offset < line + column - 2 for line, column, offset in (start, end)):
        return False
    if any(line == 1 and offset != column - 1 for line, column, offset in (start, end)):
        return False
    line_delta, offset_delta = end[0] - start[0], end[2] - start[2]
    if line_delta < 0 or offset_delta < 0:
        return False
    return end[1] - start[1] == offset_delta if line_delta == 0 else offset_delta >= line_delta + end[1] - 1
