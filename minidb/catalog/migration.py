"""v1导出TABLE记录的元数据转换。仅转换定义，不读旧库、不导入行、不发布文件。"""
from minidb.core.schema import ColumnDef, DataType, Schema, TypeSpec, _normalize_identifier
from minidb.core._v2_contract import fail


def schema_from_v1_table(record):
    """拟供v2导入器调用：TABLE字典 -> (小写表名, Schema)。

    输入合同：{"kind":"TABLE","table_name":str,"columns":[{"name":str,"type":"INT"|"VARCHAR"}]}。
    JSONL读写及ROW的Value反序列化尚由赵凯航提供，本函数不代写导入器。
    v1不含NULL，故所有列明确设nullable=False；不猜主键，不保留旧RowId或页号。
    """
    if type(record) is not dict or set(record) != {"kind", "table_name", "columns"} or record["kind"] != "TABLE":
        fail("INVALID_ARGUMENT", "需要规范的v1 TABLE记录")
    name = _normalize_identifier(record["table_name"], "migration")
    if name.startswith("_sys_"):
        fail("RESERVED_NAME", "迁移不能创建保留表", table_name=name)
    declarations = record["columns"]
    if type(declarations) is not list or not 1 <= len(declarations) <= 64:
        fail("INVALID_ARGUMENT", "迁移列数必须为1..64")
    columns = []
    for declaration in declarations:
        if (type(declaration) is not dict or set(declaration) != {"name", "type"}
                or declaration["type"] not in ("INT", "VARCHAR")):
            fail("INVALID_ARGUMENT", "v1列只支持INT/VARCHAR")
        columns.append(ColumnDef(_normalize_identifier(declaration["name"], "migration"),
                                 TypeSpec(DataType[declaration["type"]]), nullable=False))
    return name, Schema(tuple(columns))
