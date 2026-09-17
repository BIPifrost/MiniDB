# -*- coding: utf-8 -*-
"""MiniDB 综合演示：CRUD + 报错，同一个数据库，输出自带讲解。

用法：python -X utf8 data\\verify\\demo_full.py
流程：
  第一部分 CRUD：建表 → 插入 → 查询 → 更新 → 删除 → 验证
  第二部分 报错：表不存在 / 主键重复 / 类型不匹配 / 不支持语法 / 语法错误
每步先打印讲解，再打印真实输出。
"""
import os, subprocess, sys, tempfile

ROOT = r"D:\CODE\Java\MiniDB"
DB = os.path.join(ROOT, "data", "verify", "demo_all.db")


def run_sql(sql, extra=None):
    """单独子进程跑一条 SQL（或一段连续 SQL），返回 (exit, 输出文本)。"""
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write(sql)
        path = f.name
    try:
        args = [sys.executable, "-X", "utf8", "-m", "minidb", "--db", DB, "--file", path]
        if extra:
            args += extra
        proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        out = (proc.stdout + proc.stderr).replace("\r\n", "\n").strip()
        # 去掉临时文件路径，让位置显示更干净
        out = out.replace(path, "<sql>")
        return proc.returncode, out
    finally:
        os.unlink(path)


def show(step, talk, sql, extra=None):
    print("━" * 68)
    print(f"▶ {step}")
    print(f"  讲解：{talk}")
    print(f"  SQL ：{sql}")
    code, out = run_sql(sql, extra)
    print("  ── 真实输出 ──")
    for line in out.splitlines():
        print("   " + line)
    print()


def fresh_db():
    for p in (DB, DB + ".mdb2-lock", DB + ".mdb2-journal"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def main():
    print("=" * 68)
    print("MiniDB 综合演示：CRUD → 报错（同一个数据库）")
    print("=" * 68)
    fresh_db()

    # ---------- 第一部分：CRUD ----------
    print("\n【第一部分 · 基本功能 CRUD】\n")
    show("① 建表", "创建学生表：id 主键 + 姓名 + 年龄",
         "CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR(64), age INT);")
    show("② 插入", "写入两行学生数据",
         "INSERT INTO student(id,name,age) VALUES (1,'Alice',20);\n"
         "INSERT INTO student(id,name,age) VALUES (2,'Bob',22);")
    show("③ 查询", "读出表中全部数据",
         "SELECT id,name,age FROM student;")
    show("④ 更新", "把 id=1 的年龄改为 21",
         "UPDATE student SET age = 21 WHERE id = 1;")
    show("⑤ 删除", "删掉 id=2 那一行",
         "DELETE FROM student WHERE id = 2;")
    show("⑥ 验证", "再查一次：确认更新生效、删除生效",
         "SELECT id,name,age FROM student;")

    # ---------- 第二部分：报错 ----------
    print("\n【第二部分 · 错误处理演示（故意写错，看系统怎么报）】\n")
    show("⑦ 表不存在", "查一张不存在的表：系统报 TABLE_NOT_FOUND，并指出表名",
         "SELECT * FROM nope;")
    show("⑧ 主键重复", "插入重复的主键 id=1：系统报 PRIMARY_KEY_VIOLATION，指出索引名",
         "INSERT INTO student(id,name,age) VALUES (1,'Dup',18);")
    show("⑨ 类型不匹配", "id 列传字符串 'abc'：系统报 TYPE_MISMATCH，指出列名",
         "INSERT INTO student(id,name,age) VALUES ('abc','X',18);")
    show("⑩ 不支持语法", "ORDER BY 当前版本未实现：系统报 UNSUPPORTED_FEATURE，指出是哪个特性",
         "SELECT id FROM student ORDER BY id;")
    show("⑪ 语法错误", "把 SELECT 拼成 SELEC：系统报 UNEXPECTED_TOKEN，并列出此处允许的关键字",
         "SELEC id FROM student;")

    # ---------- 结尾 ----------
    print("=" * 68)
    print("演示结束。数据库文件：data\\verify\\demo_all.db")
    print("=" * 68)


if __name__ == "__main__":
    main()
