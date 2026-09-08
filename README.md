# MiniDB

《大型平台软件设计实习》的 Python 数据库项目，依据实习指导书 PDF、两份 PPT 和《实训工作计划 v1.1》。术语说明见《专业名词入门解释》。这些参考资料保留在本地开发目录的上一级，未包含在本仓库中。

当前在初始化骨架上继续开发张振负责的模块：表结构、表达式类型规则、内存目录、目录行转换、Bound/Plan、Semantic、Planner 和 CatalogManager。张振部分的源码、演示和测试已补中文注释。

远程提交 4d52cae 已提供 Row/RowScan、结果类型、ExecutionContext、StorageEngine 抽象接口及测试专用内存存储。现有执行器已改用张振的正式 Plan 和表定义：建表调用目录登记接口，插入使用已绑定的行，投影使用列序号，删除先关闭扫描再按 RowId 逐行删除。原有临时类型 `_scaffold.py` 已移除，修改处已补中文注释。

远程提交 5b038ad 新增公共磁盘常量、文件头及空闲页编解码、FileManager 文件读写和 LRU/FIFO 排序工具。队友已有的 `storage/_errors.py` 已移到 `core/errors.py`，保留原实现，目录、编译器和文件存储统一引用；表定义和目录检查也已改用公共页常量。

本次只对接已有代码。`expression_eval.py` 仍未实现，仅在过滤入口预留 `evaluate(expr, row)` 调用；源码位置、Token、AST、RowCodec、BufferPool、页分配/回收和真实记录存储仍待对应成员提供。执行器的计划校验还依赖源码位置类型，建表目录预检还依赖 RowCodec，当前不能独立跑通完整执行流程。CLI 保持初始化入口，不能直接执行 SQL。底层文件读写已通过测试，完整数据库的持久化仍未验证。写入后的同步统一由后续 Session 调度。

## 运行

使用 Python 3.11 或以上，仅依赖标准库，无需安装第三方包。本次验证环境：Windows，Python 3.12.7（conda-forge）。源码和文档使用 UTF-8，源码使用 LF 换行。

先进入 `MiniDB` 文件夹，再运行：

```powershell
python -m minidb
python -m minidb --help
python -m minidb --db data/demo.db
```

程序显示初始化说明后退出。`--db` 目前仅接收并显示预留路径，不创建、打开或修改数据库文件。其他规划中的参数待对应功能实现后加入。

## 目录

```text
MiniDB/
├── README.md
├── minidb/
│   ├── __init__.py
│   ├── __main__.py       # python -m minidb 的入口
│   ├── cli/main.py       # 启动参数和提示
│   ├── core/             # 表、表达式、目录协议、行与结果、页常量及公共错误
│   ├── compiler/         # 张振的语义与计划；词法、语法、优化待接入
│   ├── catalog/          # 内存目录、目录行转换及管理器
│   ├── storage/          # 文件读写、页格式、替换排序及存储抽象接口
│   └── engine/           # 执行器已接正式类型；表达式求值待实现
├── examples/             # SQL 输入样例及可运行的 Schema/目录演示
├── tests/                # 模块测试、公共接口样例及测试专用存储替身
└── data/                 # 预留本地数据目录
```

上图列出项目的主要目录。其余文件按工作计划第 12、17 节逐步添加。

## 后续开发

| 成员 | 主要职责 |
|---|---|
| 赵凯航 | 词法、语法、AST、CLI、RowCodec、优化 |
| 张振 | Schema、语义、绑定、计划、系统目录 |
| 廖杰 | 文件、物理页、页缓存、替换策略 |
| 周升荣 | 执行器、数据页、记录存储、集成测试 |

下一步由对应成员补齐剩余公共类型、表达式求值、AST、编码和页存储实现，再继续验证完整链路。完整调用顺序为：SQL → Lexer → Parser → Semantic → Planner → Executor → StorageEngine → BufferPool → FileManager；优化器开启时位于 Planner 与 Executor 之间。

使用标准库 `unittest`，在项目根目录运行 `python -X utf8 -m unittest discover -s tests -v`。当前工作区 179 项测试中 173 项通过、6 项因 AST、源码位置和 Token 接口未提供而跳过。公共错误已接通，相关校验直接验证真实 DbError；文件存储测试使用临时文件验证读写和重新打开。`tests/test_executor.py` 的 9 项对接测试仍使用现有内存存储替身：缺少源码位置时只隔离位置检查，建表测试隔离编码预检，过滤测试用 Mock 检查参数和 RowId 传递。这些结果不代表缺失模块或完整 SQL 持久化链路已实现。
