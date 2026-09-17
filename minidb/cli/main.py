"""MiniDB 命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from minidb.cli.display import StreamFormatter, format_result, format_trace_readable
from minidb.cli.session import Session
from minidb.core.diagnostics import format_trace
from minidb.core.errors import (
    DbError,
    ErrorStage,
    INPUT_INVALID_UTF8,
    INPUT_READ_FAILED,
)
from minidb.core.result import ResultCursor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniDB 实训项目")
    parser.add_argument("--db", default="data/demo.db", help="数据库文件路径")
    parser.add_argument("--file", help="执行 SQL 文件；不指定时从标准输入读取")
    parser.add_argument("--syntax-check", action="store_true", help="只检查 SQL 语法，不打开数据库")
    parser.add_argument("--trace", action="store_true", help="输出编译阶段 JSON trace")
    parser.add_argument("--trace-readable", action="store_true", help="输出适合人阅读的分阶段 trace")
    parser.add_argument("--storage-log", action="store_true", help="输出存储事件日志")
    parser.add_argument("--buffer-pages", type=int, default=16, help="缓存页数，默认 16")
    parser.add_argument("--policy", choices=("lru", "fifo"), default="lru", help="缓存替换策略")
    parser.add_argument("--optimize", action="store_true", help="启用计划优化")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    #校验
    if args.syntax_check:
        invalid = []
        if args.file is None:
            invalid.append("--syntax-check 必须同时指定 --file")
        if args.db != "data/demo.db":
            invalid.append("--syntax-check 不能与 --db 同时使用")
        if args.trace or args.trace_readable or args.storage_log or args.optimize:
            invalid.append("--syntax-check 不能与 --trace、--trace-readable、--storage-log 或 --optimize 同时使用")
        if args.buffer_pages != 16 or args.policy != "lru":
            invalid.append("--syntax-check 不能指定缓存参数")
        if invalid:
            parser.error("；".join(invalid))
        return _run_syntax_check(args.file)

    if args.trace and args.trace_readable:
        parser.error("--trace 与 --trace-readable 不能同时使用")

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
        trace_mode = "readable" if args.trace_readable else ("json" if args.trace else None)
        sink = _trace_sink(trace_mode)
        if args.file:
            # 文件先整体读入以保留原始换行；随后仍按语句流式执行。
            text = _read_sql_file(args.file)
            exit_code = _run_script(
                session, text, source_name=args.file, sink=sink, trace_json=args.trace
            )
        else:
            exit_code = _run_interactive(
                session,
                trace_sink=sink,
                readable_trace=args.trace_readable,
                trace_json=args.trace,
            )
    except DbError as error:
        if not (args.trace or args.trace_readable):
            print(str(error), file=sys.stderr)
        exit_code = 1
        if session is not None and not session.is_closed:
            session.abort()
    except KeyboardInterrupt:
        exit_code = 130
        _close_quietly(session)
    except BrokenPipeError:
        # 下游提前关闭管道：关闭游标后正常退出，不再写任何输出。
        exit_code = 0
        _close_quietly(session)
    finally:
        if session is not None and not session.is_closed:
            try:
                session.close()
            except DbError as error:
                print(str(error), file=sys.stderr)
                exit_code = 1
    return exit_code


def _run_script(session: Session, text: str, *, source_name: str, sink, trace_json: bool) -> int:
    """执行一段 SQL；SELECT 逐行输出，读取中途失败返回非零。"""
    stream = session.iter_results(text, source_name=source_name, trace_sink=sink)
    exit_code = 0
    try:
        for result in stream:
            exit_code = max(exit_code, _emit_result(result, trace=trace_json))
            if exit_code:
                # 与 execute_text 一致：首次失败后不再执行本输入中的后续语句。
                break
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return exit_code


def _run_interactive(
    session: Session,
    *,
    trace_sink=None,
    readable_trace: bool = False,
    trace_json: bool = False,
) -> int:
    """运行按提交执行的交互循环。

    一次提交可以跨多行；只有字符串和块注释之外的分号才结束当前提交。
    普通 SQL 错误打印后继续接收下一次输入，存储错误则由 Session 标记会话
    已终止并退出。
    """
    buffer: list[str] = []
    had_incomplete_eof = False
    exit_code = 0
    while True:
        prompt = "MiniDB> " if not buffer else "...> "
        print(prompt, end="", flush=True)
        line = sys.stdin.readline()
        if line == "":
            if buffer and "".join(buffer).strip():
                had_incomplete_eof = True
                try:
                    exit_code = max(exit_code, _run_script(
                        session,
                        "".join(buffer),
                        source_name="<stdin>",
                        sink=trace_sink,
                        trace_json=trace_json and not readable_trace,
                    ))
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
            exit_code = max(exit_code, _run_script(
                session,
                text,
                source_name="<stdin>",
                sink=trace_sink,
                trace_json=trace_json and not readable_trace,
            ))
        except DbError as error:
            print(str(error), file=sys.stderr)
            # 语法、语义和执行阶段的普通错误允许下一次提交；存储错误
            # 会使 Session 进入 closed/failed 状态，不能继续使用。
            if session.is_closed:
                return 1
    return max(exit_code, 1 if had_incomplete_eof else 0)


def _emit_result(result, *, trace: bool) -> int:
    """输出一条语句结果；流式 SELECT 返回非零表示结果不完整。"""
    if isinstance(result, ResultCursor):
        return _emit_cursor(result, trace=trace)
    print(format_trace(result) if trace else format_result(result))
    return 0


def _emit_cursor(cursor: ResultCursor, *, trace: bool) -> int:
    """逐行消费 SELECT 游标；中途失败时报告 result_complete=false。"""
    formatter = StreamFormatter(cursor.columns)
    header_printed = False
    try:
        for row in cursor:
            if not trace and not header_printed:
                print(formatter.header())
                header_printed = True
            if not trace:
                print(formatter.row(row))
            else:
                formatter.row(row)
    except (KeyboardInterrupt, BrokenPipeError):
        cursor.close()
        raise
    except DbError as error:
        cursor.close()
        _report_incomplete_result(error, formatter.count)
        return 1
    finally:
        if not cursor.closed:
            cursor.close()

    if trace:
        print(format_trace({
            "kind": "STREAM_RESULT",
            "columns": [column.name for column in cursor.columns],
            "row_count": formatter.count,
            "result_complete": True,
        }))
    else:
        # count==0 时 footer() 输出 Empty set，与物化入口的显示保持一致。
        print(formatter.footer())
    return 0


def _report_incomplete_result(error: DbError, rows_shown: int) -> None:
    """流式读取第 N 行失败：已显示的行保留，并明确标记结果不完整。"""
    print(str(error), file=sys.stderr)
    print(f"result_complete=false; rows_shown={rows_shown}", file=sys.stderr)


def _close_quietly(session: Session | None) -> None:
    """中断路径优先关闭活动游标；清理失败不能盖住原始中断。"""
    if session is None or session.is_closed:
        return
    try:
        session.abort()
    except BaseException:
        pass


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


def _trace_sink(mode: str | None):
    if mode is None:
        return None

    def write(event: dict[str, object]) -> None:
        output = format_trace_readable(event) if mode == "readable" else format_trace(event)
        print(output, file=sys.stderr)

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
