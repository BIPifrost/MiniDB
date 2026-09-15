"""CREATE INDEX integration across Executor, IndexManager and real Session."""

import tempfile
import unittest
from pathlib import Path

from minidb.cli.session import Session
from minidb.core import errors


class CreateIndexExecutionTests(unittest.TestCase):
    def test_builds_existing_rows_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "create-index.db"
            session = Session.open(str(path), optimize=True)
            try:
                results = session.execute_text(
                    "CREATE TABLE item(id INT, label VARCHAR(32));"
                    "INSERT INTO item(id,label) VALUES (2,'b');"
                    "INSERT INTO item(id,label) VALUES (1,'a');"
                    "INSERT INTO item(id,label) VALUES (2,'c');"
                    "CREATE INDEX ix_item_id ON item(id);"
                )
                self.assertEqual(results[-1].message, "CREATE INDEX OK")
                index = session.catalog.find_index("ix_item_id")
                self.assertIsNotNone(index)
                self.assertEqual(session.index_manager.validate(index).entry_count, 3)
                selected = session.execute_text(
                    "SELECT label FROM item WHERE id = 2;", materialize=True
                )[0]
                self.assertEqual(selected.rows, [("b",), ("c",)])
            finally:
                session.close()

            reopened = Session.open(str(path), optimize=True)
            try:
                index = reopened.catalog.find_index("ix_item_id")
                self.assertIsNotNone(index)
                self.assertEqual(reopened.index_manager.validate(index).entry_count, 3)
                selected = reopened.execute_text(
                    "SELECT label FROM item WHERE id = 1;", materialize=True
                )[0]
                self.assertEqual(selected.rows, [("a",)])
            finally:
                reopened.close()

    def test_unique_build_failure_rolls_back_without_catalog_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.open(str(Path(directory) / "unique-index.db"))
            try:
                session.execute_text(
                    "CREATE TABLE item(id INT);"
                    "INSERT INTO item(id) VALUES (1);"
                    "INSERT INTO item(id) VALUES (1);"
                )
                with self.assertRaises(errors.DbError) as caught:
                    session.execute_text("CREATE UNIQUE INDEX ux_item_id ON item(id);")
                self.assertEqual(caught.exception.code, errors.UNIQUE_VIOLATION)
                self.assertIsNone(session.catalog.find_index("ux_item_id"))
                self.assertEqual(
                    session.execute_text(
                        "SELECT id FROM item;", materialize=True
                    )[0].rows,
                    [(1,), (1,)],
                )
            finally:
                session.close()

    def test_update_and_delete_keep_created_index_in_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.open(
                str(Path(directory) / "index-movements.db"), optimize=True
            )
            try:
                session.execute_text(
                    "CREATE TABLE item(id INT, label VARCHAR(32));"
                    "INSERT INTO item(id,label) VALUES (1,'a');"
                    "INSERT INTO item(id,label) VALUES (2,'b');"
                    "CREATE INDEX ix_item_id ON item(id);"
                    "UPDATE item SET id=3 WHERE id=1;"
                    "DELETE FROM item WHERE id=2;"
                )
                table = session.catalog.find_table("item")
                reports = session.index_manager.check_indexes(table)
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0].entry_count, 1)
                self.assertEqual(
                    session.execute_text(
                        "SELECT label FROM item WHERE id=3;", materialize=True
                    )[0].rows,
                    [("a",)],
                )
                self.assertEqual(
                    session.execute_text(
                        "SELECT label FROM item WHERE id=2;", materialize=True
                    )[0].rows,
                    [],
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
