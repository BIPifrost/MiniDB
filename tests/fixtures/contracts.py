"""张振维护的共享样例，对应工作计划第 17.2 节。

本文件提供表、目录行、SQL 以及独立手写的预期 Bound/Plan。
正式 AST 构造函数在 semantic_cases.py，完整 JSON 预期在 contracts_v1.json。
expected_bound/expected_plan 不调用 Semantic 或 Planner，避免被测代码自己生成答案。
源码位置使用 core/source.py 的正式类型。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minidb.compiler.bound import (
    BoundBinary, BoundColumn, BoundCreate, BoundDelete, BoundExpr,
    BoundInsert, BoundLiteral, BoundSelect, BoundStatement,
)
from minidb.compiler.plan import (
    CreateTablePlan, DeletePlan, FilterPlan, InsertPlan, Plan, ProjectPlan, SeqScanPlan,
)
from minidb.core.expressions import ExprOp
from minidb.core.result import ResultColumn
from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, TableRef

if TYPE_CHECKING:
    from minidb.core.source import SourceSpan


STUDENT_SCHEMA = Schema((
    ColumnDef("id", DataType.INT),
    ColumnDef("name", DataType.VARCHAR),
    ColumnDef("age", DataType.INT),
))

STUDENT_TABLE = TableDef(
    ref=TableRef(table_id=1, name="student", root_page_id=2),
    schema=STUDENT_SCHEMA,
)

# 独立手写的七字段预期，不能调用被测转换函数生成标准答案。
STUDENT_CATALOG_ROWS = (
    (1, "student", 2, 3, 0, "id", "INT"),
    (1, "student", 2, 3, 1, "name", "VARCHAR"),
    (1, "student", 2, 3, 2, "age", "INT"),
)


# 五条独立 SQL：重复投影单列为一个样例，两个 id 必须有不同的源码位置。
CASE_SQL = {
    "create": "CREATE TABLE course(cid INT, title VARCHAR);",
    "insert": "INSERT INTO student(name,age,id) VALUES ('Alice',20,1);",
    "select": "SELECT name FROM student WHERE age >= 18;",
    "delete": "DELETE FROM student WHERE id = 1;",
    "select_duplicate": "SELECT id,id FROM student;",
}

COURSE_SCHEMA = Schema((ColumnDef("cid", DataType.INT), ColumnDef("title", DataType.VARCHAR)))
STUDENT_ROW = (1, "Alice", 20)


def span(sql: str, fragment: str | None = None, *, start: int = 0) -> SourceSpan:
    """为本条单行 SQL 的片段构造真实 SourceSpan，结束位置不包含在范围内。"""
    from minidb.core.source import SourcePos, SourceSpan

    offset = 0 if fragment is None else sql.index(fragment, start)
    end = len(sql) if fragment is None else offset + len(fragment)
    return SourceSpan(SourcePos(1, offset + 1, offset), SourcePos(1, end + 1, end), "<test>")


def _expected_predicate(case_name: str) -> BoundExpr | None:
    """只列出两个固定 WHERE 的绑定预期，不根据任意 SQL 做语义分析。"""
    sql = CASE_SQL[case_name]
    if case_name == "select":
        return BoundBinary(
            ExprOp.GE, BoundColumn(2, DataType.INT, span(sql, "age")),
            BoundLiteral(18, DataType.INT, span(sql, "18")), DataType.BOOL,
            span(sql, ">="), span(sql, "age >= 18"),
        )
    if case_name == "delete":
        return BoundBinary(
            ExprOp.EQ, BoundColumn(0, DataType.INT, span(sql, "id")),
            BoundLiteral(1, DataType.INT, span(sql, "1")), DataType.BOOL,
            span(sql, "="), span(sql, "id = 1"),
        )
    return None


def expected_bound(case_name: str) -> BoundStatement:
    """返回固定 SQL 的预期绑定结果；名字必须是 CASE_SQL 中的一个键。"""
    sql = CASE_SQL[case_name]
    location = span(sql)
    if case_name == "create":
        return BoundCreate("course", COURSE_SCHEMA, location)
    if case_name == "insert":
        # SQL 是 (name, age, id)，预期行独立写成表定义的 (id, name, age)。
        return BoundInsert(STUDENT_TABLE, STUDENT_ROW, location)
    if case_name == "select":
        return BoundSelect(STUDENT_TABLE, (1,), (ResultColumn("name", DataType.VARCHAR),),
                           _expected_predicate(case_name), location)
    if case_name == "delete":
        return BoundDelete(STUDENT_TABLE, _expected_predicate(case_name), location)
    # select_duplicate：结果允许重复名字，不能使用要求列名唯一的 Schema。
    return BoundSelect(STUDENT_TABLE, (0, 0),
                       (ResultColumn("id", DataType.INT), ResultColumn("id", DataType.INT)),
                       None, location)


def expected_plan(case_name: str) -> Plan:
    """独立手工搭建预期计划，不调用 Planner，也不从 expected_bound 转换。"""
    sql = CASE_SQL[case_name]
    location = span(sql)
    if case_name == "create":
        return CreateTablePlan("course", COURSE_SCHEMA, location)
    if case_name == "insert":
        return InsertPlan(STUDENT_TABLE, STUDENT_ROW, location)
    if case_name == "select":
        return ProjectPlan(
            FilterPlan(SeqScanPlan(STUDENT_TABLE, location), _expected_predicate(case_name), location),
            (1,), (ResultColumn("name", DataType.VARCHAR),), location,
        )
    if case_name == "delete":
        return DeletePlan(
            STUDENT_TABLE,
            FilterPlan(SeqScanPlan(STUDENT_TABLE, location), _expected_predicate(case_name), location),
            location,
        )
    return ProjectPlan(SeqScanPlan(STUDENT_TABLE, location), (0, 0),
                       (ResultColumn("id", DataType.INT), ResultColumn("id", DataType.INT)), location)
