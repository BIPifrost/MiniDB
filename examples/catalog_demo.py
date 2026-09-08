"""在 MiniDB 根目录运行：python -m examples.catalog_demo。"""

from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, Catalog
from minidb.core.catalog_protocol import CatalogRead
from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, TableRef


def main() -> None:
    """手工建立两张表的目录，演示大小写查询、排序和列表副本。"""
    student = TableDef(
        ref=TableRef(table_id=1, name="student", root_page_id=2),
        schema=Schema((
            ColumnDef("id", DataType.INT),
            ColumnDef("name", DataType.VARCHAR),
            ColumnDef("age", DataType.INT),
        )),
    )
    course = TableDef(
        ref=TableRef(table_id=2, name="course", root_page_id=3),
        schema=Schema((ColumnDef("cid", DataType.INT),)),
    )
    catalog: CatalogRead = Catalog((course, student))

    print("内存目录演示：只使用手工表定义，未创建数据库文件。")
    print("用户表按表号排列：")
    for table in catalog.list_tables():
        print(f"  表号 {table.ref.table_id}：{table.ref.name}，根页号 {table.ref.root_page_id}")

    table = catalog.find_table("STUDENT")
    if table is not None:
        match = table.schema.find_column("AGE")
        if match is not None:
            index, column = match
            print(f"STUDENT.AGE：第 {index} 列，类型 {column.data_type.value}")
    print(f"查找不存在的表：{catalog.find_table('missing_table')}")

    returned = catalog.list_tables()
    returned.clear()
    print(f"清空返回的列表后，目录中仍有 {len(catalog.list_tables())} 张用户表。")

    system = SYSTEM_CATALOG_TABLE
    print(f"系统目录固定定义：{system.ref.name}，表号 {system.ref.table_id}，根页号 {system.ref.root_page_id}")
    print("  字段：" + ", ".join(column.name for column in system.schema.columns))
    print(f"系统目录不出现在用户查表结果中：{catalog.find_table(system.ref.name)}")


if __name__ == "__main__":
    main()
