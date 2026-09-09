from minidb.cli.main import main


# python -m minidb 会运行这个文件；普通 import minidb 不会启动命令行。
if __name__ == "__main__":
    raise SystemExit(main())
