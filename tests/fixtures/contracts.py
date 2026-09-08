"""张振维护的固定样例，逐步补齐工作计划第 17.2 节。

当前提供 student 表定义及独立手写的目录行。AST、Bound、Plan 样例在相关正式类可用后补齐，
不使用字典或临时私有类型替代。
"""

from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, TableRef


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
