"""MiniDB 命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from minidb.cli.session import Session
from minidb.cli.display import format_result
from minidb.core.diagnostics import format_trace
from minidb.core.errors import (
    DbError,
    ErrorStage,
    INPUT_INVALID_UTF8,
    INPUT_READ_FAILED,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniDB 实训项目")
    parser.add_argument("--db", default="data/demo.db", help="数据库文件路径")
    parser.add_argument("--file", help="执行 SQL 文件；不指定时从标准输入读取")
    parser.add_argument("--syntax-check", action="store_true", help="只检查 SQL 语法，不打开数据库")
    parser.add_argument("--trace", action="store_true", help="输出编译阶段 JSON trace")
    parser.add_argument("--storage-log", action="store_true", help="输出存储事件日志")
    parser.add_argument("--buffer-pages", type=int, default=16, help="缓存页数，默认 16")
    parser.add_argument("--policy", choices=("lru", "fifo"), default="lru", help="缓存替换策略")
    parser.add_argument("--optimize", action="store_true", help="启用计划优化")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.syntax_check:
        invalid = []
        if args.file is None:
            invalid.append("--syntax-check 必须同时指定 --file")
        if args.db != "data/demo.db":
            invalid.append("--syntax-check 不能与 --db 同时使用")
        if args.trace or args.storage_log or args.optimize:
            invalid.append("--syntax-check 不能与 --trace、--storage-log 或 --optimize 同时使用")
        if args.buffer_pages != 16 or args.policy != "lru":
            invalid.append("--syntax-check 不能指定缓存参数")
        if invalid:
            parser.error("；".join(invalid))
        return _run_syntax_check(args.file)

    if args.buffer_pages < 1:
        parser.error("--buffer-pages 必须 >= 1")
    _configure_storage_log(args.storage_log)

    session: Session | None = None
    exit_code = 0
    try:
        session = Session.open(
            args.db,
            buffer_pages=args.buffer_pages,
            policy=args.policy,
            optimize=args.optimize,
        )
        sink = _trace_sink(args.trace)
        if args.file:
            # Session.execute_file 保留文件原始换行；trace 时由 execute_text 的同一链路输出。
            if sink is None:
                results = session.execute_file(args.file)
            else:
                text = _read_sql_file(args.file)
                results = session.execute_text(text, source_name=args.file, trace_sink=sink)
        else:
            return _run_interactive(session, trace_sink=sink)
        for result in results:
            _print_result(result, trace=args.trace)
    except DbError as error:
        if not args.trace:
            print(str(error), file=sys.stderr)
        exit_code = 1
        if session is not None and not session.is_closed:
            session.abort()
    finally:
        if session is not None and not session.is_closed:
            try:
                session.close()
            except DbError as error:
                print(str(error), file=sys.stderr)
                exit_code = 1
    return exit_code


def _run_interactive(session: Session, *, trace_sink=None) -> int:
    """运行按提交执行的交互循环。

    一次提交可以跨多行；只有字符串和块注释之外的分号才结束当前提交。
    普通 SQL 错误打印后继续接收下一次输入，存储错误则由 Session 标记会话
    已终止并退出。
    """
    buffer: list[str] = []
    had_incomplete_eof = False
    while True:
        prompt = "MiniDB> " if not buffer else "...> "
        print(prompt, end="", flush=True)
        line = sys.stdin.readline()
        if line == "":
            if buffer and "".join(buffer).strip():
                had_incomplete_eof = True
                try:
                    session.execute_text("".join(buffer), source_name="<stdin>", trace_sink=trace_sink)
                except DbError as error:
                    print(str(error), file=sys.stderr)
            break

        if not buffer and line.strip().lower() in {"quit", "exit"}:
            break
        buffer.append(line)
        if not _has_complete_statement("".join(buffer)):
            continue

        text = "".join(buffer)
        buffer.clear()
        try:
            results = session.execute_text(text, source_name="<stdin>", trace_sink=trace_sink)
            for result in results:
                _print_result(result, trace=trace_sink is not None)
        except DbError as error:
            print(str(error), file=sys.stderr)
            # 语法、语义和执行阶段的普通错误允许下一次提交；存储错误
            # 会使 Session 进入 closed/failed 状态，不能继续使用。
            if session.is_closed:
                return 1
    return 1 if had_incomplete_eof else 0


def _print_result(result, *, trace: bool) -> None:
    """正常模式打印人类可读结果，trace 模式保留稳定 JSON。"""
    print(format_trace(result) if trace else format_result(result))


def _has_complete_statement(text: str) -> bool:
    """判断文本中是否出现字符串和注释之外的分号。"""
    in_string = False
    in_line_comment = False
    in_block_comment = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if char == "'":
                if next_char == "'":
                    index += 2
                else:
                    in_string = False
                    index += 1
            else:
                index += 1
            continue
        if char == "-" and next_char == "-":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue
        if char == "'":
            in_string = True
            index += 1
            continue
        if char == ";":
            return True
        index += 1
    return False


def _run_syntax_check(path: str) -> int:
    try:
        text = _read_sql_file(path)
    except DbError as error:
        print(format_trace(error), file=sys.stderr)
        return 1

    result = Session.check_syntax(text, source_name=path)
    for error in result.errors:
        print(format_trace(error), file=sys.stderr)
    print(format_trace({
        "kind": "SYNTAX_CHECK_RESULT",
        "valid_statement_count": result.valid_statement_count,
        "error_count": len(result.errors),
        "stopped_on_lexical_error": result.stopped_on_lexical_error,
    }))
    return 1 if result.errors else 0


def _read_sql_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as error:
        raise DbError(
            ErrorStage.LEXICAL,
            INPUT_INVALID_UTF8,
            "SQL 文件不是合法 UTF-8",
            context={"operation": "main.read_file", "source_name": path, "reason": str(error)},
        ) from error
    except (OSError, TypeError, ValueError) as error:
        raise DbError(
            ErrorStage.LEXICAL,
            INPUT_READ_FAILED,
            "无法读取 SQL 文件",
            context={"operation": "main.read_file", "source_name": str(path), "cause": str(error)},
        ) from error


def _trace_sink(enabled: bool):
    if not enabled:
        return None

    def write(event: dict[str, object]) -> None:
        print(format_trace(event), file=sys.stderr)

    return write


def _configure_storage_log(enabled: bool) -> None:
    logger = logging.getLogger("minidb.storage.buffer_pool")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG if enabled else logging.CRITICAL + 1)
    if enabled:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


__all__ = ["build_parser", "main"]
