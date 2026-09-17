# -*- coding: utf-8 -*-
"""真实存储账本：t100 / t250 两张演示表的行数、行大小、页数与成本。

与 Optimizer 完全同路径：Session → storage.scan_rows 数行 → collect_table_stats
推导页数/树高 → cost_seq / cost_index。输出用于对照 EXPLAIN 的 cost 列，
并在下方给出每条 cost 的计算公式（与数字一一对应）。
"""
import sys, os
sys.path.insert(0, r"D:\CODE\Java\MiniDB")
from minidb.cli.session import Session
from minidb.compiler.statistics import (
    collect_table_stats, estimate_row_size, cost_seq, cost_index,
)
from minidb.storage.row_codec import MAX_ROW_SIZE

DBs = (("t100", r"data\verify\cbo\t100.db"), ("t250", r"data\verify\cbo\t250.db"))

rows_data = []
print(f"{'库':<6}{'行数':>6}{'行大小':>8}{'每页行数':>8}{'表页数':>8}{'树高':>6}"
      f"{'全表cost':>10}{'点查索引cost':>14}{'范围索引cost':>14}")
for name, path in DBs:
    s = Session.open(path)
    try:
        table = s.catalog.find_table("t")
        rows = sum(1 for _ in s.storage.scan_rows(table))
        stats = collect_table_stats(rows, table.schema)
        row_size = estimate_row_size(table.schema)
        per_page = MAX_ROW_SIZE // row_size
        point = cost_index(stats, 1.0 / max(1, rows))
        rng = cost_index(stats, 1 / 3)
        rows_data.append((name, rows, stats.pages, stats.height, point, rng))
        print(f"{name:<6}{rows:>6}{row_size:>8}{per_page:>8}{stats.pages:>8}{stats.height:>6}"
              f"{cost_seq(stats):>10.2f}{point:>14.2f}{rng:>14.2f}")
    finally:
        s.close()

print()
print("每条 cost 的计算公式（与上表数字一一对应）：")
print("  全表cost     = 表页数")
for name, rows, pages, height, point, rng in rows_data:
    print(f"                 {name}: {pages} 页 = {pages}.00")
print("  点查索引cost = 树高 + 表页数 × (1 ÷ 行数) × 2")
for name, rows, pages, height, point, rng in rows_data:
    print(f"                 {name}: {height} + {pages}×(1/{rows})×2 = {point:.2f}")
print("  范围索引cost = 树高 + 表页数 × (1/3) × 2   （1/3 = 范围条件的估算选择率）")
for name, rows, pages, height, point, rng in rows_data:
    print(f"                 {name}: {height} + {pages}×(1/3)×2 = {rng:.2f}")

print()
print("行格式：v2 行 = 前缀4B + NULL位图1B + 每列8B(INT)  →  t(id,a) 两列 = 4+1+8+8 = 21B")
print(f"页容量：数据区 {MAX_ROW_SIZE}B / 21B = {MAX_ROW_SIZE // 21} 行/页")
print("系数说明：随机读惩罚 = 2（经验假设）；点查选择率 1/N 精确，范围选择率 1/3 估算")
