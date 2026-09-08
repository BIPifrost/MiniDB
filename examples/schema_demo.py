"""在 MiniDB 根目录运行：python -m examples.schema_demo。"""

from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.schema import ColumnDef, DataType, Schema


def main() -> None:
    """依次演示建 Schema、查列和类型规则查询，不解析 SQL。"""
    student = Schema((
        ColumnDef("id", DataType.INT),
        ColumnDef("name", DataType.VARCHAR),
        ColumnDef("age", DataType.INT),
    ))

    print("student 表结构（手工定义，尚未创建数据库表）")
    for index, column in enumerate(student.columns):
        print(f"  第 {index} 列：{column.name} {column.data_type.value}")

    for name in ("AGE", "missing_col"):
        match = student.find_column(name)
        if match is None:
            print(f"查找 {name}：未找到，返回 None")
        else:
            index, column = match
            print(f"查找 {name}：列序号 {index}，类型 {column.data_type.value}")

    cases = (
        ("age >= 18", ExprOp.GE, (DataType.INT, DataType.INT)),
        ("name = '张三'", ExprOp.EQ, (DataType.VARCHAR, DataType.VARCHAR)),
        ("name > '张三'", ExprOp.GT, (DataType.VARCHAR, DataType.VARCHAR)),
        ("age = '18'", ExprOp.EQ, (DataType.INT, DataType.VARCHAR)),
        ("NOT (age >= 18)", ExprOp.NOT, (DataType.BOOL,)),
    )
    print("\n类型规则查询（示例说明由代码写明，尚未解析或执行 SQL）")
    for label, op, operands in cases:
        result = resolve_result_type(op, operands)
        print(f"  {label}：{result.value if result is not None else '不允许这种类型组合'}")


if __name__ == "__main__":
    main()
