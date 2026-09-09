"""张振的正式 AST 交接样例：只调用其他成员的类，不实现或替换这些类。

这里根据已知 SQL 手工搭树，目的是检查 Semantic，不能当作 Parser 实现。
"""

from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType
from fixtures.contracts import CASE_SQL, span


def select_case():
    """SELECT name ... WHERE age >= 18：投影第 1 列，条件读取第 2 列。"""
    from minidb.compiler.ast import BinaryExpr, IdentifierExpr, LiteralExpr, NameRef, SelectStmt

    sql = CASE_SQL["select"]
    where = BinaryExpr(
        ExprOp.GE, IdentifierExpr("age", span(sql, "age")),
        LiteralExpr(18, DataType.INT, span(sql, "18")), span(sql, ">="), span(sql, "age >= 18"),
    )
    return SelectStmt(NameRef("student", span(sql, "student")), False,
                      (NameRef("name", span(sql, "name")),), where, span(sql))


def insert_case():
    """INSERT 的列顺序故意与 Schema 不同，用来检查唯一一次值重排。"""
    from minidb.compiler.ast import InsertStmt, LiteralExpr, NameRef

    sql = CASE_SQL["insert"]
    return InsertStmt(
        NameRef("student", span(sql, "student")),
        tuple(NameRef(name, span(sql, name)) for name in ("name", "age", "id")),
        (LiteralExpr("Alice", DataType.VARCHAR, span(sql, "'Alice'")),
         LiteralExpr(20, DataType.INT, span(sql, "20")),
         LiteralExpr(1, DataType.INT, span(sql, "1", start=sql.index("VALUES")))),
        span(sql),
    )


def delete_case():
    """DELETE ... WHERE id = 1：删除计划的输入必须保留扫描记录位置。"""
    from minidb.compiler.ast import BinaryExpr, DeleteStmt, IdentifierExpr, LiteralExpr, NameRef

    sql = CASE_SQL["delete"]
    where = BinaryExpr(
        ExprOp.EQ, IdentifierExpr("id", span(sql, "id")),
        LiteralExpr(1, DataType.INT, span(sql, "1")), span(sql, "="), span(sql, "id = 1"),
    )
    return DeleteStmt(NameRef("student", span(sql, "student")), where, span(sql))


def create_case():
    """创建 course 的手工语法树；分析成功后仍然不应修改 Catalog。"""
    from minidb.compiler.ast import ColumnDecl, CreateTableStmt, NameRef

    sql = CASE_SQL["create"]
    columns = tuple(
        ColumnDecl(NameRef(name, span(sql, name)), kind, span(sql, kind.name), span(sql, f"{name} {kind.name}"))
        for name, kind in (("cid", DataType.INT), ("title", DataType.VARCHAR))
    )
    return CreateTableStmt(NameRef("course", span(sql, "course")), columns, span(sql))


def select_duplicate_case():
    """SELECT id,id：保留两次列引用，并分别记录第一个、第二个 id 的位置。"""
    from minidb.compiler.ast import NameRef, SelectStmt

    sql = CASE_SQL["select_duplicate"]
    columns = (NameRef("id", span(sql, "id")),
               NameRef("id", span(sql, "id", start=sql.index(",") + 1)))
    return SelectStmt(NameRef("student", span(sql, "student")), False, columns, None, span(sql))


# 其他成员按相同样例名取 AST，再与 contracts.py 的预期 Bound/Plan 对照。
AST_CASES = {
    "create": create_case,
    "insert": insert_case,
    "select": select_case,
    "delete": delete_case,
    "select_duplicate": select_duplicate_case,
}
