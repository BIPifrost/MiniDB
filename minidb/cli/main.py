"""最小启动入口，后续在这里接入数据库会话。"""

import argparse


def main() -> None:
    """解析预留 --db 参数并打印初始化说明；这里尚未接入数据库会话。"""
    parser = argparse.ArgumentParser(description="MiniDB 实训项目（初始化版本）")
    parser.add_argument("--db", default="data/demo.db", help="预留数据库路径，目前仅显示")
    # argparse 把命令行的 --db 内容存进 args.db，并自动提供 --help。
    args = parser.parse_args()

    print("MiniDB 初始化版本")
    print(f"预留数据库路径：{args.db}")
    print("SQL 编译、执行和存储功能待实现；本次启动不会读写数据库。")
