# MiniDB

《大型平台软件设计实习》的最小初始化项目，依据实习指导书 PDF、两份 PPT 和《实训工作计划 v1.1》。术语说明见《专业名词入门解释》。这些参考资料保留在本地开发目录的上一级，未包含在本仓库中。

当前只建立包目录和启动入口。SQL 编译、执行、页式存储和系统目录尚未实现，也尚未完成规划中的 P0 公共类型定义。

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
│   ├── core/             # 待补公共类型：Token、Schema、Row 等
│   ├── compiler/         # 待补词法、语法、语义、计划和优化
│   ├── catalog/          # 待补系统目录
│   ├── storage/          # 待补文件、页、缓存和记录存储
│   └── engine/           # 待补执行器
├── examples/demo.sql     # 后续演示输入，当前不能执行
├── tests/                # 预留测试目录
└── data/                 # 预留本地数据目录
```

各模块目前仅有包说明，具体文件按工作计划第 12、17 节逐步添加。

## 后续开发

| 成员 | 主要职责 |
|---|---|
| 赵凯航 | 词法、语法、AST、CLI、RowCodec、优化 |
| 张振 | Schema、语义、绑定、计划、系统目录 |
| 廖杰 | 文件、物理页、页缓存、替换策略 |
| 周升荣 | 执行器、数据页、记录存储、集成测试 |

下一步先按规划补齐 `core` 公共类型及 AST、Bound、Plan，再开始各模块实现。完整调用顺序为：SQL → Lexer → Parser → Semantic → Planner → Executor → StorageEngine → BufferPool → FileManager；优化器开启时位于 Planner 与 Executor 之间。

本次使用 AI 辅助建立目录、入口和说明，并验证启动与参数处理。尚无 SQL 功能测试；后续使用标准库 `unittest`，在项目根目录运行 `python -m unittest discover -s tests`。
