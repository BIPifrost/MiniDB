# MiniDB

《大型平台软件设计实习》的 Python 数据库项目，依据实习指导书、PPT 和《实训工作计划》。参考资料保留在本地开发目录的上一级，未包含在本仓库中。

张振负责 Schema、表达式类型规则、CatalogRead、Semantic、Bound/Plan、Planner、系统目录与 CatalogManager。源码、样例和测试附有中文注释。当前已接入队友提供的 Lexer、Parser、AST、SourceSpan、公共错误、RowCodec、行与结果类型和存储接口。

## 运行

使用 Python 3.11 或以上，仅依赖标准库。本次验证环境为 Windows、Python 3.12.7。源码和文档使用 UTF-8，源码使用 LF 换行。

在项目根目录运行：

```powershell
python -m minidb
python -m minidb --help
python -m minidb --db data/demo.db
```

CLI 目前显示初始化说明后退出；`--db` 仅接收并显示路径，不打开数据库或执行 SQL。

## 目录与分工

```text
MiniDB/
├── minidb/
│   ├── cli/             # 启动入口
│   ├── core/            # 表、表达式、目录协议、源码位置、行、结果及错误
│   ├── compiler/        # 词法、语法、语义、绑定、计划与优化
│   ├── catalog/         # 内存目录、目录行转换及管理器
│   ├── storage/         # 文件读写、页格式、行编码及存储接口
│   └── engine/          # 执行器与执行上下文
├── examples/            # SQL 样例及 Schema/目录演示
├── tests/               # 模块测试、共享样例与测试专用存储替身
└── data/                # 预留本地数据目录
```

| 成员 | 主要职责 |
|---|---|
| 赵凯航 | 词法、语法、AST、CLI/Session、RowCodec、优化、公共诊断 |
| 张振 | Schema、表达式类型规则、语义、绑定、计划、系统目录 |
| 廖杰 | 文件、物理页、页缓存、替换策略 |
| 周升荣 | 执行器、表达式求值、数据页、记录存储、集成测试 |

## 张振部分的验证

```powershell
python -X utf8 -m unittest discover -s tests -v
```

当前全部 417 项测试通过，无跳过项。真实 SQL 经 Lexer → Parser → Semantic → Planner 的五组交接样例已通过；建表对接测试使用正式 RowCodec 完成目录行预检。

- 语义与计划：名称、列集合、值类型、INT64 边界、错误顺序、AND/OR 两侧检查、嵌套 NOT、投影顺序、删除目标表及循环引用。
- 源码位置：字符偏移与行列一致，子节点属于同一输入及父范围；共享表达式每次调用仅校验一次内部字段，每条父子关系仍单独检查。
- 目录：七字段记录转换、乱序恢复、完整性检查、根页验证、写完再发布、失败后关闭扫描；坏行错误保留页号和槽号，关闭失败不会掩盖原错误。
- 公共接口：错误上下文通过正式接口补充；手工 AST 的复核保持 SEMANTIC 阶段，目录登记错误保持 STORAGE 阶段；执行器调用目录接口提前拒绝重名表，避免额外分配表号和根页。

共享样例位于 `tests/fixtures/`：`contracts.py` 提供 SQL 和独立手写的预期 Bound/Plan，`semantic_cases.py` 提供正式 AST，`contracts_v1.json` 保存完整固定预期。五个样例为 `create`、`insert`、`select`、`delete`、`select_duplicate`，预期结果不由被测代码生成。

已删除正式接口到位后过时的模块探测、跳过分支、位置替身和重复错误包装逻辑。目录调度测试仍用 Mock 检查失败顺序；执行器测试使用已有内存存储替身，过滤测试的 Mock 只验证参数传递。

表达式求值、BufferPool、页分配/回收、真实记录存储和完整 Session 执行链仍待对应成员完成。现有测试通过不代表完整数据库持久化已验证；写入后的同步由 Session 统一调度。
