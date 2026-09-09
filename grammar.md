# MiniDB SQL 文法

本文档是 MiniDB 当前递归下降 Parser 的文法说明。文法、Token 名称和
错误处理规则与 `minidb/compiler/lexer.py`、`minidb/compiler/parser.py`
及《实训工作计划》保持一致。

## 1. 范围与记号

本期支持四类 SQL：

- `CREATE TABLE`
- `INSERT INTO ... VALUES`
- `SELECT ... FROM`
- `DELETE FROM`

文法记号含义如下：

- `:=` 表示“定义为”；
- `|` 表示任选其一；
- `[]` 表示可选一次；
- `{}` 表示重复零次或多次；
- 全大写单词表示关键字 Token；
- `IDENT`、`INTEGER`、`STRING` 表示 Token 类别；
- `EOF` 表示输入结束。

Parser 接收 Lexer 产生的 Token 流。Lexer 保留原始 `lexeme` 和源码位置，
Parser 只负责组织语法树，不查询 Catalog、不检查表列是否存在，也不执行 SQL。

## 2. 完整文法

```text
program       := { statement | ';' } EOF

statement     := create_stmt
               | insert_stmt
               | select_stmt
               | delete_stmt

create_stmt   := CREATE TABLE IDENT '(' column_def { ',' column_def } ')' ';'
column_def    := IDENT data_type
data_type     := INT | VARCHAR

insert_stmt   := INSERT INTO IDENT '(' id_list ')' VALUES '(' value_list ')' ';'
select_stmt   := SELECT select_list FROM IDENT [ WHERE expression ] ';'
delete_stmt   := DELETE FROM IDENT [ WHERE expression ] ';'

select_list   := '*' | id_list
id_list       := IDENT { ',' IDENT }
value_list    := literal { ',' literal }
literal       := [ '-' ] INTEGER | STRING

expression    := or_expr
or_expr       := and_expr { OR and_expr }
and_expr      := not_expr { AND not_expr }
not_expr      := NOT not_expr | comparison
comparison    := primary [ comp_op primary ]
primary       := IDENT | literal | '(' expression ')'
comp_op       := '=' | '==' | '!=' | '<>' | '<' | '<=' | '>' | '>='
```

`INTEGER` 对应 `INTEGER_LITERAL` Token，Lexer 保存无符号数字文本；前置
`-` 由 Parser 与整数合并，并检查 INT64 范围。`STRING` 对应
`STRING_LITERAL` Token，字符串内容由 Lexer 解码连续的两个单引号，Parser
将其放入 `LiteralExpr`。

`DECIMAL_LITERAL`、算术运算和扩展关键字虽然由 Lexer 识别，但不属于当前
成功文法。它们进入 Parser 后报告 `UNSUPPORTED_FEATURE`，不会进入 AST。

## 3. 表达式优先级和结合性

从高到低的层次为：

| 层次 | 非终结符 | 结合方式 |
|---|---|---|
| 最高 | `primary`、比较运算 | 一条比较最多一个比较运算符 |
|  | `NOT` | 右递归，可连续出现 |
|  | `AND` | 从左到右 |
| 最低 | `OR` | 从左到右 |

因此：

```text
a = 1 OR b = 2 AND c = 3
```

等价于：

```text
(a = 1) OR ((b = 2) AND (c = 3))
```

```text
NOT age = 18
```

等价于：

```text
NOT (age = 18)
```

括号先计算其中的完整表达式，再作为一个 `primary` 返回：

```text
NOT (age = 18 OR age = 20)
```

表示先计算括号内的 `age = 18 OR age = 20`，再执行 `NOT`。

当前不支持链式比较：

```text
a < b < c
```

Parser 只接受 `primary comp_op primary`。第二个 `<` 不属于比较结果的合法
后继位置，因此会报告语法错误。表达式进入 Executor 后，`AND` 和 `OR` 才按
从左到右规则短路求值；Semantic 仍必须检查完整表达式的两侧。

## 4. FIRST 集合

FIRST 集合表示一个非终结符能够开始的 Token。当前文法中没有空产生式的
表达式非终结符，因此这些集合可以直接用于选择递归下降分支。

| 非终结符 | FIRST 集合 |
|---|---|
| `program` | `KW_CREATE`, `KW_INSERT`, `KW_SELECT`, `KW_DELETE`, `SEMICOLON`, `EOF` |
| `statement` | `KW_CREATE`, `KW_INSERT`, `KW_SELECT`, `KW_DELETE` |
| `create_stmt` | `KW_CREATE` |
| `insert_stmt` | `KW_INSERT` |
| `select_stmt` | `KW_SELECT` |
| `delete_stmt` | `KW_DELETE` |
| `select_list` | `STAR`, `IDENT` |
| `id_list` | `IDENT` |
| `value_list` | `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL` |
| `literal` | `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL` |
| `expression` | `NOT`, `IDENT`, `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL`, `LPAREN` |
| `or_expr` | `NOT`, `IDENT`, `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL`, `LPAREN` |
| `and_expr` | `NOT`, `IDENT`, `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL`, `LPAREN` |
| `not_expr` | `NOT`, `IDENT`, `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL`, `LPAREN` |
| `comparison` | `IDENT`, `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL`, `LPAREN` |
| `primary` | `IDENT`, `INTEGER_LITERAL`, `MINUS`, `STRING_LITERAL`, `LPAREN` |

`program` 中的 `SEMICOLON` 是空语句，Parser 会消费并跳过，不生成 AST。

## 5. FOLLOW 集合

FOLLOW 集合表示一个非终结符后面可以出现的 Token。它们用于判断可选项
是否结束，以及在语法检查模式中定位语句边界。

| 非终结符 | FOLLOW 集合 |
|---|---|
| `statement` | `KW_CREATE`, `KW_INSERT`, `KW_SELECT`, `KW_DELETE`, `SEMICOLON`, `EOF` |
| `select_list` | `KW_FROM` |
| `id_list` | `RPAREN` |
| `value_list` | `RPAREN` |
| `literal` | `COMMA`, `RPAREN` |
| `expression` | `RPAREN`, `SEMICOLON` |
| `or_expr` | `RPAREN`, `SEMICOLON` |
| `and_expr` | `KW_OR`, `RPAREN`, `SEMICOLON` |
| `not_expr` | `KW_AND`, `KW_OR`, `RPAREN`, `SEMICOLON` |
| `comparison` | `KW_AND`, `KW_OR`, `RPAREN`, `SEMICOLON` |
| `primary` | `EQ`, `NE`, `LT`, `LE`, `GT`, `GE`, `KW_AND`, `KW_OR`, `RPAREN`, `SEMICOLON` |

例如，`select_list` 看到 `KW_FROM` 时，说明列列表已经结束；`expression`
看到 `RPAREN` 或 `SEMICOLON` 时，说明当前表达式已经结束。Parser 的循环
结构正是利用这些后继 Token 实现的。

## 6. 递归下降分支表

Parser 使用一个 Token 前瞻，按 FIRST 集合选择函数分支：

| 当前 Token | 选择的规则或动作 |
|---|---|
| `KW_CREATE` | 解析 `create_stmt` |
| `KW_INSERT` | 解析 `insert_stmt` |
| `KW_SELECT` | 解析 `select_stmt` |
| `KW_DELETE` | 解析 `delete_stmt` |
| `SEMICOLON` | 跳过空语句，继续读取下一条 |
| `STAR` | `select_list := '*'` |
| `IDENT` | `id_list`、`primary` 或表/列名称 |
| `NOT` | `not_expr := NOT not_expr` |
| `INTEGER_LITERAL`、`MINUS`、`STRING_LITERAL` | 解析 `literal` |
| `LPAREN` | 解析括号内的 `expression` |
| `EOF` | 结束整个 Token 流 |

在名称、数据类型、分号等位置，当前 Token 不符合预期时报告
`UNEXPECTED_TOKEN` 或 `UNEXPECTED_EOF`。如果当前 Token 是小数或已识别的
扩展关键字，则优先报告 `UNSUPPORTED_FEATURE`。

## 7. 为什么选择递归下降

本项目使用递归下降，而不是再实现一个 LL(1) 表驱动 Parser 或 LR Parser：

1. 每个文法非终结符都有直观的 Python 函数，例如 `_parse_select`、
   `_parse_expression`、`_parse_not` 和 `_parse_primary`，便于课堂讲解。
2. 原始文法已经按优先级拆成 `or_expr`、`and_expr`、`not_expr` 和
   `comparison`，不含左递归，可以直接自顶向下调用。
3. 一个 Token 前瞻足以区分四类语句、星号投影、名称、字面量和括号，
   不需要额外的分析表或生成工具。
4. Parser 以 Token 流为输入，并通过 `iter_statements` 惰性地产生语句，
   第一条语句完成后才继续读取后续输入，适合交互输入和多语句文件。

## 8. 语法检查与错误恢复

`Parser.check_syntax(tokens)` 使用与正常解析相同的语法函数，但不生成可执行
计划，也不访问数据库。

- 语法错误会记录一个 `DbError`，然后丢弃当前残缺语句；
- Parser 跳过 Token，直到第一个真正的 `SEMICOLON` 或 `EOF`；
- 找到分号后消费它，继续检查下一条语句；
- 字符串和注释内部的分号由 Lexer 消化，不会成为同步点；
- 词法错误会被记录，并设置 `stopped_on_lexical_error=True`，停止后续检查；
- 空分号不计入成功语句数；
- 成功解析的非空语句数量、错误顺序和词法终止标记保存在不可变的
  `SyntaxCheckResult` 中。

例如：

```sql
SELECT FROM t;
SELECT * FROM t;
SELECT FROM t;
```

结果是 1 条成功语句和 2 条 `UNEXPECTED_TOKEN`，第二条成功语句不会因为
前后存在错误而被跳过。

## 9. Parser 与后续模块的边界

Parser 输出 `compiler.ast` 中的不可变节点，后续处理顺序为：

```text
Lexer → Parser → AST → Semantic → Bound → Planner → Plan → Optimizer
```

- Parser 保留表名、列名、字面量、操作符和源码位置；
- Semantic 负责表是否存在、列是否存在、类型是否匹配及完整表达式检查；
- Planner 负责把 Bound 转成逻辑计划；
- Optimizer 只对已通过 Semantic/Planner 校验的 Plan 做结构优化。

因此，Parser 不应把“表不存在”当成语法错误，也不应通过优化器修正语法或
语义错误。扩展语法可以被 Lexer 识别，但在当前范围内必须明确拒绝，不能
偷偷生成不完整 AST。
