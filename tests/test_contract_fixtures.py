"""核对张振的共享样例：固定 JSON、正式对象和被测模块分别对照。

JSON 是独立写出的预期字段及位置，没有调用 Semantic、Planner 或 trace 工具生成。
本文件只比较字段，不实现其他成员负责的 AST、源码位置或 JSON trace 输出工具。
"""

import json
import unittest
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path

from fixtures import contracts
from fixtures.semantic_cases import AST_CASES
from minidb.catalog.catalog import Catalog
from minidb.compiler.bound_validation import validate_bound
from minidb.compiler.lexer import Lexer
from minidb.compiler.parser import Parser
from minidb.compiler.plan import validate_plan
from minidb.compiler.planner import Planner
from minidb.compiler.semantic import Semantic
from minidb.core.source import SourceText


def json_nodes(value):
    """遍历固定 JSON 里的节点，供位置检查使用，不把字典转成运行时 AST。"""
    if isinstance(value, dict):
        if "node_type" in value:
            yield value
        for child in value.values():
            yield from json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_nodes(child)


class ContractFixtureTests(unittest.TestCase):
    """用正式对象核对固定位置、Bound/Plan 字段及列顺序。"""

    def setUp(self):
        """用 UTF-8 读取独立编写的固定预期。"""
        path = Path(__file__).parent / "fixtures" / "contracts_v1.json"
        self.fixture = json.loads(path.read_text(encoding="utf-8"))
        self.cases = self.fixture["cases"]

    def assert_snapshot_matches(self, actual, expected):
        """逐字段对照独立 JSON；不调用任何被测序列化或 trace 函数。"""
        if is_dataclass(actual):
            self.assertEqual(type(actual).__name__, expected["node_type"])
            names = {field.name for field in fields(actual)}
            self.assertEqual(names, set(expected) - {"node_type"})
            for name in sorted(names):
                self.assert_snapshot_matches(getattr(actual, name), expected[name])
        elif isinstance(actual, Enum):
            self.assertEqual(actual.name, expected)
        elif isinstance(actual, tuple):
            self.assertIsInstance(expected, list)
            self.assertEqual(len(actual), len(expected))
            for actual_item, expected_item in zip(actual, expected):
                self.assert_snapshot_matches(actual_item, expected_item)
        else:
            # 同时比类型，防止把 Python 的 True 当成数字 1 通过比较。
            self.assertIs(type(actual), type(expected))
            self.assertEqual(actual, expected)

    def test_fixture_contains_all_five_cases_and_three_stages(self):
        """五个交接样例分别保存自己的 SQL、AST、Bound 和 Plan。"""
        names = {"create", "insert", "select", "delete", "select_duplicate"}
        self.assertEqual(self.fixture["format_version"], 1)
        self.assertEqual(set(self.cases), names)
        self.assertEqual(set(contracts.CASE_SQL), names)
        self.assertEqual(set(AST_CASES), names)
        for case_name, sample in self.cases.items():
            with self.subTest(case=case_name):
                self.assertEqual(set(sample), {"sql", "ast", "bound", "plan"})
                self.assertEqual(sample["sql"], contracts.CASE_SQL[case_name])
                for stage in ("ast", "bound", "plan"):
                    self.assertEqual(sample[stage]["span"]["start"]["offset"], 0)
                    self.assertEqual(sample[stage]["span"]["end"]["offset"], len(sample["sql"]))

    def test_json_positions_match_each_sql_without_source_standins(self):
        """直接核对 JSON 数字位置与原 SQL 的切片，不需要伪造 SourceSpan。"""
        for case_name, sample in self.cases.items():
            sql = sample["sql"]
            for stage in ("ast", "bound", "plan"):
                for node in json_nodes(sample[stage]):
                    kind = node["node_type"]
                    with self.subTest(case=case_name, stage=stage, node=kind):
                        if kind == "SourceSpan":
                            start, end = node["start"], node["end"]
                            self.assertEqual(node["source_name"], "<test>")
                            self.assertTrue(0 <= start["offset"] <= end["offset"] <= len(sql))
                            for position in (start, end):
                                self.assertEqual(position["node_type"], "SourcePos")
                                self.assertEqual(position["line"], 1)
                                self.assertEqual(position["column"], position["offset"] + 1)
                        elif "span" in node:
                            location = node["span"]
                            fragment = sql[location["start"]["offset"]:location["end"]["offset"]]
                            if kind == "NameRef":
                                self.assertEqual(fragment, node["text"])
                            elif kind == "IdentifierExpr":
                                self.assertEqual(fragment, node["name"])
                            elif kind == "ColumnDecl":
                                self.assertEqual(fragment, f"{node['name']['text']} {node['data_type']}")
                                type_span = node["type_span"]
                                self.assertEqual(sql[type_span["start"]["offset"]:type_span["end"]["offset"]],
                                                 node["data_type"])
                            elif kind == "BoundColumn":
                                self.assertEqual(fragment, contracts.STUDENT_SCHEMA.columns[node["index"]].name)
                            elif kind in ("LiteralExpr", "BoundLiteral"):
                                text = f"'{node['value']}'" if node["data_type"] == "VARCHAR" else str(node["value"])
                                self.assertEqual(fragment, text)
                            elif kind in ("BinaryExpr", "BoundBinary"):
                                op_span = node["op_span"]
                                self.assertEqual(sql[op_span["start"]["offset"]:op_span["end"]["offset"]],
                                                 {"EQ": "=", "GE": ">="}[node["op"]])

    def test_insert_filter_delete_and_duplicate_projection_expectations(self):
        """单独列出交接时最容易混淆的值顺序、条件列序号和计划结构。"""
        self.assertEqual(self.cases["insert"]["bound"]["row"], [1, "Alice", 20])
        selected = self.cases["select"]["plan"]
        self.assertEqual(selected["column_indexes"], [1])
        self.assertEqual(selected["child"]["node_type"], "FilterPlan")
        self.assertEqual(selected["child"]["child"]["node_type"], "SeqScanPlan")
        self.assertEqual(selected["child"]["predicate"]["left"]["index"], 2)
        deleted = self.cases["delete"]["plan"]
        self.assertEqual(deleted["child"]["child"]["node_type"], "SeqScanPlan")
        self.assertEqual(deleted["child"]["predicate"]["left"]["index"], 0)
        duplicated = self.cases["select_duplicate"]
        self.assertEqual(duplicated["plan"]["column_indexes"], [0, 0])
        self.assertEqual([column["name"] for column in duplicated["plan"]["output_columns"]], ["id", "id"])
        self.assertEqual([column["span"]["start"]["offset"] for column in duplicated["ast"]["columns"]], [7, 10])

    def test_expected_bound_and_plan_match_fixed_json(self):
        """正式样例对象逐字段匹配独立预期，同时通过真实的 Bound/Plan 校验。"""
        for name, sample in self.cases.items():
            with self.subTest(case=name):
                bound = contracts.expected_bound(name)
                plan = contracts.expected_plan(name)
                validate_bound(bound, plan=True)
                validate_plan(plan)
                self.assert_snapshot_matches(bound, sample["bound"])
                self.assert_snapshot_matches(plan, sample["plan"])

    def test_planner_result_matches_independently_written_plan(self):
        """Planner 的输出必须等于固定预期，答案不是由同一 Planner 生成的。"""
        for name in self.cases:
            with self.subTest(case=name):
                actual = Planner().build(contracts.expected_bound(name))
                self.assertEqual(actual, contracts.expected_plan(name))

    def test_real_ast_objects_match_fixed_json(self):
        """检查手工 AST 的每个字段和每处真实源码位置。"""
        for name, build in AST_CASES.items():
            with self.subTest(case=name):
                self.assert_snapshot_matches(build(), self.cases[name]["ast"])

    def test_semantic_and_planner_match_shared_contracts(self):
        """真实 AST 经 Semantic 和 Planner 后，与五个独立预期一致且不修改目录。"""
        catalog = Catalog((contracts.STUDENT_TABLE,))
        for name, build in AST_CASES.items():
            with self.subTest(case=name):
                bound = Semantic().analyze(build(), catalog)
                self.assertEqual(bound, contracts.expected_bound(name))
                self.assertEqual(Planner().build(bound), contracts.expected_plan(name))
        self.assertEqual(catalog.list_tables(), [contracts.STUDENT_TABLE])

    def test_real_parser_output_connects_to_semantic_and_planner(self):
        """真实 SQL 经队友前端进入张振接口，仍须匹配五组独立标准答案。"""
        catalog = Catalog((contracts.STUDENT_TABLE,))
        for name, sample in self.cases.items():
            with self.subTest(case=name):
                source = SourceText("<test>", sample["sql"])
                statements = list(Parser().iter_statements(Lexer().scan(source)))
                self.assertEqual(len(statements), 1)
                self.assert_snapshot_matches(statements[0], sample["ast"])
                bound = Semantic().analyze(statements[0], catalog)
                self.assertEqual(bound, contracts.expected_bound(name))
                self.assertEqual(Planner().build(bound), contracts.expected_plan(name))
        self.assertEqual(catalog.list_tables(), [contracts.STUDENT_TABLE])


if __name__ == "__main__":
    unittest.main()
