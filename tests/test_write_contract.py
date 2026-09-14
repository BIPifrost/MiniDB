import unittest
from dataclasses import FrozenInstanceError
from uuid import uuid4

from minidb.core.records import (
    RowId,
    RowUpdate,
    StoredRow,
    ValidatedWriteToken,
    WriteKind,
    _issue_validated_write_token,
)
from minidb.core.schema import (
    ColumnDef,
    DataType,
    IndexDef,
    IndexOrigin,
    PendingIndexDef,
    Schema,
    TableDef,
    TableRef,
    TypeSpec,
)
from minidb.core.source import SourcePos, SourceSpan
from minidb.engine.write_contract import PreparedWrite, validate_prepared
from minidb.storage.constraints import ValidatedWriteToken as ValidatorToken


STUDENT_SCHEMA = Schema((
    ColumnDef("id", TypeSpec(DataType.INT)),
    ColumnDef("name", TypeSpec(DataType.VARCHAR, length=64)),
    ColumnDef("age", TypeSpec(DataType.INT)),
))
STUDENT_TABLE = TableDef(TableRef(1, "student", 3), STUDENT_SCHEMA)
STUDENT_INDEX = IndexDef(1, "ix_student_id", 1, 0, 4, False, IndexOrigin.USER)
PENDING_STUDENT_INDEX = PendingIndexDef(
    "ix_student_id", 0, False, IndexOrigin.USER
)


class WriteContractTests(unittest.TestCase):
    def setUp(self):
        self.session_id = uuid4()
        self.prepared_id = uuid4()
        self.span = SourceSpan(
            SourcePos(1, 1, 0), SourcePos(1, 2, 1), "write.sql"
        )

    def _token(self, *, session_id=None, prepared_id=None, generation=3):
        return _issue_validated_write_token(
            self.session_id if session_id is None else session_id,
            generation,
            self.prepared_id if prepared_id is None else prepared_id,
        )

    def _prepared(self, kind, **changes):
        fields = {
            "prepared_id": self.prepared_id,
            "session_id": self.session_id,
            "catalog_generation": 3,
            "kind": kind,
            "span": self.span,
            "table": None,
            "create_name": None,
            "create_schema": None,
            "create_indexes": (),
            "create_index_entries": (),
            "insert_row": None,
            "updates": (),
            "deletes": (),
            "affected_indexes": (),
            "encoded_candidate_bytes": 0,
            "validation_token": self._token(),
        }
        fields.update(changes)
        return PreparedWrite(**fields)

    def test_token_public_construction_is_rejected(self):
        self.assertIs(ValidatorToken, ValidatedWriteToken)
        with self.assertRaises(TypeError):
            ValidatedWriteToken(self.session_id, 3, self.prepared_id)
        with self.assertRaises(TypeError):
            _issue_validated_write_token(self.session_id, True, self.prepared_id)

    def test_token_and_prepared_write_are_immutable(self):
        token = self._token()
        prepared = self._prepared(
            WriteKind.INSERT,
            table=STUDENT_TABLE,
            insert_row=(1, "Alice", 20),
            validation_token=token,
        )
        with self.assertRaises(FrozenInstanceError):
            token.catalog_generation = 4
        with self.assertRaises(FrozenInstanceError):
            prepared.insert_row = (2, "Bob", 17)
        validate_prepared(prepared)

    def test_all_write_kinds_accept_their_own_fields(self):
        row_id = RowId(STUDENT_TABLE.ref.root_page_id, 0)
        old = StoredRow(row_id, (1, "Alice", 20))
        update = RowUpdate(row_id, old.values, (1, "Alicia", 20))
        cases = (
            self._prepared(
                WriteKind.CREATE_TABLE,
                create_name="student",
                create_schema=STUDENT_SCHEMA,
            ),
            self._prepared(
                WriteKind.CREATE_INDEX,
                table=STUDENT_TABLE,
                create_indexes=(PENDING_STUDENT_INDEX,),
                create_index_entries=((1, row_id),),
            ),
            self._prepared(
                WriteKind.INSERT,
                table=STUDENT_TABLE,
                insert_row=(1, "Alice", 20),
                affected_indexes=(STUDENT_INDEX,),
            ),
            self._prepared(
                WriteKind.UPDATE,
                table=STUDENT_TABLE,
                updates=(update,),
                affected_indexes=(STUDENT_INDEX,),
            ),
            self._prepared(
                WriteKind.DELETE,
                table=STUDENT_TABLE,
                deletes=(old,),
                affected_indexes=(STUDENT_INDEX,),
            ),
        )
        self.assertEqual(tuple(item.kind for item in cases), tuple(WriteKind))

    def test_empty_update_and_delete_batches_are_valid(self):
        self._prepared(WriteKind.UPDATE, table=STUDENT_TABLE)
        self._prepared(WriteKind.DELETE, table=STUDENT_TABLE)

    def test_token_identity_fields_must_match(self):
        with self.assertRaisesRegex(ValueError, "validation_token"):
            self._prepared(
                WriteKind.INSERT,
                table=STUDENT_TABLE,
                insert_row=(1, "Alice", 20),
                validation_token=self._token(prepared_id=uuid4()),
            )

    def test_irrelevant_fields_are_rejected_for_each_kind(self):
        with self.assertRaisesRegex(ValueError, "CREATE_TABLE"):
            self._prepared(
                WriteKind.CREATE_TABLE,
                create_name="student",
                create_schema=STUDENT_SCHEMA,
                table=STUDENT_TABLE,
            )
        with self.assertRaisesRegex(ValueError, "INSERT"):
            self._prepared(
                WriteKind.INSERT,
                table=STUDENT_TABLE,
                insert_row=(1, "Alice", 20),
                updates=(
                    RowUpdate(
                        RowId(STUDENT_TABLE.ref.root_page_id, 0),
                        (1, "Alice", 20),
                        (1, "Alicia", 20),
                    ),
                ),
            )

    def test_duplicate_physical_targets_are_rejected(self):
        row_id = RowId(STUDENT_TABLE.ref.root_page_id, 0)
        old = StoredRow(row_id, (1, "Alice", 20))
        update = RowUpdate(row_id, old.values, (1, "Alicia", 20))
        with self.assertRaisesRegex(ValueError, "updates.*duplicate"):
            self._prepared(
                WriteKind.UPDATE,
                table=STUDENT_TABLE,
                updates=(update, update),
            )
        with self.assertRaisesRegex(ValueError, "deletes.*duplicate"):
            self._prepared(
                WriteKind.DELETE,
                table=STUDENT_TABLE,
                deletes=(old, old),
            )

    def test_index_fields_require_the_formal_catalog_types(self):
        with self.assertRaisesRegex(TypeError, "create_indexes"):
            self._prepared(
                WriteKind.CREATE_INDEX,
                table=STUDENT_TABLE,
                create_indexes=(object(),),
            )
        with self.assertRaisesRegex(TypeError, "affected_indexes"):
            self._prepared(
                WriteKind.INSERT,
                table=STUDENT_TABLE,
                insert_row=(1, "Alice", 20),
                affected_indexes=(object(),),
            )


if __name__ == "__main__":
    unittest.main()
