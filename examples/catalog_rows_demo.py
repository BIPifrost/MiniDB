"""在 MiniDB 根目录运行：python -m examples.catalog_rows_demo。"""

from minidb.catalog.catalog_rows import catalog_from_rows, table_to_catalog_rows
from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, TableRef


def main() -> None:
    """将表定义转为目录行，再把倒序目录行恢复为同一张表。"""
    student = TableDef(
        TableRef(1, "student", 2),
        Schema((
            ColumnDef("id", DataType.INT),
            ColumnDef("name", DataType.VARCHAR),
            ColumnDef("age", DataType.INT),
        )),
    )
    rows = table_to_catalog_rows(student)
    print("目录行转换演示：仅处理内存记录，未读写数据库文件。")
    print("字段顺序：表号、表名、根页号、列数、列序号、列名、列类型")
    for row in rows:
        print(row)

    restored = catalog_from_rows(reversed(rows))
    table = restored.find_table("STUDENT")
    assert table == student
    print("\n将目录行倒序输入后，恢复出的列顺序：")
    for index, column in enumerate(table.schema.columns):
        print(f"  {index}: {column.name} {column.data_type.value}")
    print("恢复的完整表定义与原表一致。")


if __name__ == "__main__":
    main()
