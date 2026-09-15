 # 一条 SQL 的处理过程示例

本文用一条查询语句说明 MiniDB 编译链中每一步产生什么内容。

## 输入

假设目录中已经有下列表结构：

```text
student(id INT, name VARCHAR, age INT)
```

其中列序号从 0 开始：`id` 是 0，`name` 是 1，`age` 是 2。

输入 SQL：

```sql
SELECT name FROM student WHERE 1 = 1 AND age >= 18;
```

这条语句的意思是：从 `student` 表中找出年龄至少 18 岁的记录，只显示姓名。
其中 `1 = 1` 永远成立，特意保留它用于展示优化器的作用。

## 1. Token：拆分文字

Lexer 逐个读取字符，把 SQL 拆成 Token，并给每个 Token 记录原始文字和位置。

```text
KW_SELECT          SELECT
IDENT              name
KW_FROM            FROM
IDENT              student
KW_WHERE           WHERE
INTEGER_LITERAL    1
EQ                 =
INTEGER_LITERAL    1
KW_AND             AND
IDENT              age
GE                 >=
INTEGER_LITERAL    18
SEMICOLON          ;
EOF                
```

此时程序只知道哪些片段是关键字、名字、数字和运算符；还没有确认 `student` 表或 `age` 列是否存在。

## 2. AST：按 SQL 语法组织

Parser 根据文法把 Token 组成抽象语法树（AST）。简化表示如下：

```text
SelectStmt
|- table_name: NameRef("student")
|- select_all: False
|- columns: [NameRef("name")]
`- where: BinaryExpr(AND)
   |- left: BinaryExpr(EQ)
   |  |- LiteralExpr(1, INT)
   |  `- LiteralExpr(1, INT)
   `- right: BinaryExpr(GE)
      |- IdentifierExpr("age")
      `- LiteralExpr(18, INT)
```

AST 已经表达了语法结构，例如 `AND` 的左边是 `1 = 1`，右边是 `age >= 18`。但 AST 仍保留用户写下的列名 `age` 和 `name`，不会自己查询目录。

## 3. Bound：确认表、列和类型

Semantic 读取目录，确认 `student` 表存在，确认 `name`、`age` 都是该表的列，并把列名换成列序号。

```text
BoundSelect
|- table: student(id INT, name VARCHAR, age INT)
|- projection: (1,)
|- output_columns: [name VARCHAR]
`- predicate: BoundBinary(AND, BOOL)
   |- left: BoundBinary(EQ, BOOL)
   |  |- BoundLiteral(1, INT)
   |  `- BoundLiteral(1, INT)
   `- right: BoundBinary(GE, BOOL)
      |- BoundColumn(index=2, INT)    # age 是第 2 列
      `- BoundLiteral(18, INT)
```

这一阶段还会拒绝错误输入。例如表不存在、列名拼错、`age` 与字符串做不允许的比较，都会在这里报错，后面的执行器不会收到错误的查询。

## 4. Plan：安排执行顺序

Planner 把 Bound 组织成执行计划。数据从下往上流动：

```text
ProjectPlan(columns=(1,), output=[name VARCHAR])
`- FilterPlan(predicate=(1 = 1 AND age >= 18))
   `- SeqScanPlan(table=student)
```

含义是：

1. `SeqScanPlan` 扫描 `student` 表的完整记录；
2. `FilterPlan` 保留年龄至少 18 岁的记录；
3. `ProjectPlan` 最后只取第 1 列，即 `name`。

先过滤、后投影很重要：条件使用 `age`，即使结果只显示 `name`，过滤时仍需要完整行。

## 5. 优化前 Plan

刚由 Planner 生成的计划就是优化前 Plan：

```text
Project
`- Filter(1 = 1 AND age >= 18)
   `- SeqScan(student)
```

此计划正确，但每一行都会重复判断一次 `1 = 1`。

## 6. 优化后 Plan

Optimizer 先把 `1 = 1` 折叠成内部 BOOL 常量 `True`，再用布尔规则：

```text
True AND x  ->  x
```

得到：

```text
Project
`- Filter(age >= 18)
   `- SeqScan(student)
```

优化没有改变查询结果：它只删除了永远成立、不会过滤任何记录的条件。原始 Plan 仍会保留，便于 `--trace` 展示优化前后的区别。

## 7. Executor 将来执行的结果

如果表中有：

```text
(1, "Alice", 20)
(2, "张三", 17)
(3, "Bob", 18)
```

那么过滤后留下 Alice 和 Bob，最终投影结果为：

```text
name
-----
Alice
Bob
```

完整链路可以概括为：

```text
SQL 文字
  -> Token：切分字符
  -> AST：确认语法结构
  -> Bound：确认表、列、类型并绑定列序号
  -> Plan：确定扫描、过滤、投影顺序
  -> Optimized Plan：去掉不会影响结果的多余条件
  -> Executor：读取记录并返回查询结果
```
