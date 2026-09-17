# -*- coding: utf-8 -*-
"""双库同 SQL 严格控制对比（现场演示，秒级）。

同一句 SQL：SELECT id FROM bench WHERE b = 5000;
  - data\\verify\\bench.db       ：b 无索引   → 全表扫 SeqScan
  - data\\verify\\bench_ctrl.db  ：b 有索引   → 索引扫 IndexScan（与 bench.db 数据完全相同，复制而来）

唯一变量 = b 上的索引；数据、SQL、命中数、机器、缓存状态一致。
这正是"控制变量"最严格的演示：同一句查询，只加一个索引。

幂等：副本不存在时自动复制；副本缺 idx_b 时自动建（约 75 秒，仅首次）。
用法：python -X utf8 data\\verify\\show_control.py
"""
import os
import shutil
import statistics
import sys
import time

ROOT = r"D:\CODE\Java\MiniDB"
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "data", "verify", "bench.db")        # 原库：b 无索引
CTRL = os.path.join(ROOT, "data", "verify", "bench_ctrl.db")  # 副本：b 有索引
ROUNDS = 25


def ensure_ctrl():
    """保证两个库就绪：原库存在、副本存在、副本带 idx_b 索引。"""
    if not os.path.exists(SRC):
        raise SystemExit("缺少 bench.db：请先运行 data\\verify\\bench_index.py 造表（约 20 分钟）。")
    if not os.path.exists(CTRL):
        shutil.copy2(SRC, CTRL)
        print("已复制副本 bench_ctrl.db（数据与原库完全相同）")

    from minidb.cli.session import Session
    s = Session.open(CTRL, optimize=True)
    try:
        table = s.catalog.find_table("bench")
        has_idx = any(i.name == "idx_b" for i in s.catalog.indexes_for_table(table.ref.table_id))
        if not has_idx:
            print("副本缺少 idx_b 索引，正在创建（约 75 秒，仅首次）……")
            t0 = time.perf_counter()
            s.execute_text("CREATE INDEX idx_b ON bench(b);", materialize=True)
            print(f"  建索引完成，用时 {time.perf_counter() - t0:.1f}s")
    finally:
        s.close()


def scan_path(session, sql):
    """执行 EXPLAIN，返回扫描方式（IndexScan / SeqScan）。"""
    rows = session.execute_text(sql, materialize=True)[0].rows
    return [r[0] for r in rows if "Scan" in str(r[0])][0]


def median_ms(session, sql, rounds=ROUNDS):
    """执行查询 rounds 次，返回中位耗时（毫秒）。"""
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        session.execute_text(sql, materialize=True)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def main():
    ensure_ctrl()
    from minidb.cli.session import Session

    sql = "SELECT id FROM bench WHERE b = 5000;"
    print(f"\n同一句 SQL：{sql}\n")

    results = {}
    for name, db in (("无索引（全表扫）", SRC), ("有索引（索引扫）", CTRL)):
        s = Session.open(db, optimize=True)
        try:
            path = scan_path(s, "EXPLAIN " + sql)
            ms = median_ms(s, sql)
            results[name] = ms
            print(f"  {name:<18}路径：{path}")
            print(f"  {'':18}时间：{ms:.2f} ms（{ROUNDS} 轮中位数）")
        finally:
            s.close()

    fast = results["有索引（索引扫）"]
    slow = results["无索引（全表扫）"]
    print(f"\n  结论：同一句 SQL、数据完全相同，只差一个索引："
          f"{slow:.1f} ms → {fast:.1f} ms，快了 {slow / fast:.1f} 倍")


if __name__ == "__main__":
    main()
