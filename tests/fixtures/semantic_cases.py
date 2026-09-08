"""张振的正式 AST 交接样例：只调用其他成员的类，不实现或替换这些类。

函数内导入依赖；相关正式模块缺失时，对接测试会明确跳过。
这里根据已知 SQL 手工搭树，目的是检查 Semantic，不能当作 Parser 实现。
"""

from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType


def span(sql, fragment=None, *, start=0):
    """为单行固定 SQL 中的真实片段构造 SourceSpan，结束位置不包含在范围内。"""
    from minidb.core.source import SourcePos, SourceSpan

    offset = 0 if fragment is None else sql.index(fragment, start)
    end = len(sql) if fragment is None else offset + len(fragment)
    return SourceSpan(SourcePos(1, offset + 1, offset), SourcePos(1, end + 1, end), "<test>")


def select_case():
    """SELECT name ... WHERE age >= 18：投影第 1 列，条件读取第 2 列。"""
    from minidb.compiler.ast import BinaryExpr, IdentifierExpr, LiteralExpr, NameRef, SelectStmt

    sql = "SELECT name FROM student WHERE age >= 18;"
    where = BinaryExpr(
        ExprOp.GE, IdentifierExpr("age", span(sql, "age")),
        LiteralExpr(18, DataType.INT, span(sql, "18")), span(sql, ">="), span(sql, "age >= 18"),
    )
    return SelectStmt(NameRef("student", span(sql, "student")), False,
                      (NameRef("name", span(sql, "name")),), where, span(sql))


def insert_case():
    """INSERT 的列顺序故意与 Schema 不同，用来检查唯一一次值重排。"""
    from minidb.compiler.ast import InsertStmt, LiteralExpr, NameRef

    sql = "INSERT INTO student(name,age,id) VALUES ('Alice',20,1);"
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

    sql = "DELETE FROM student WHERE id = 1;"
    where = BinaryExpr(
        ExprOp.EQ, IdentifierExpr("id", span(sql, "id")),
        LiteralExpr(1, DataType.INT, span(sql, "1")), span(sql, "="), span(sql, "id = 1"),
    )
    return DeleteStmt(NameRef("student", span(sql, "student")), where, span(sql))


def create_case():
    """创建 course 的手工语法树；分析成功后仍然不应修改 Catalog。"""
    from minidb.compiler.ast import ColumnDecl, CreateTableStmt, NameRef

    sql = "CREATE TABLE course(cid INT, title VARCHAR);"
    columns = tuple(
        ColumnDecl(NameRef(name, span(sql, name)), kind, span(sql, kind.name), span(sql, f"{name} {kind.name}"))
        for name, kind in (("cid", DataType.INT), ("title", DataType.VARCHAR))
    )
    return CreateTableStmt(NameRef("course", span(sql, "course")), columns, span(sql))
