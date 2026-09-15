"""Session error-code and one-shot write-token integration tests."""

import copy
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from minidb.cli.session import Session
from minidb.core import errors
from minidb.core.records import _issue_validated_write_token


class SessionWriteAuthorizationTests(unittest.TestCase):
    def test_registered_token_is_authorized_only_during_one_apply(self):
        session = Session(object(), object())
        token = _issue_validated_write_token(session.session_id, 0, uuid4())
        session.register_validated_token(token)

        self.assertFalse(session._token_is_authorized(token))
        session._consume_token(token)
        self.assertTrue(session._token_is_authorized(token))
        self.assertNotIn(token, session._validated_tokens)
        session._finish_token_apply(token)
        self.assertFalse(session._token_is_authorized(token))

        with self.assertRaises(errors.DbError) as caught:
            session._consume_token(token)
        self.assertEqual(caught.exception.code, errors.INVALID_ARGUMENT)

    def test_equal_but_distinct_token_cannot_consume_registered_identity(self):
        session = Session(object(), object())
        token = _issue_validated_write_token(session.session_id, 0, uuid4())
        duplicate = copy.deepcopy(token)
        self.assertEqual(duplicate, token)
        self.assertIsNot(duplicate, token)
        session.register_validated_token(token)

        with self.assertRaises(errors.DbError) as caught:
            session._consume_token(duplicate)
        self.assertEqual(caught.exception.code, errors.INVALID_ARGUMENT)
        self.assertFalse(session._token_is_authorized(token))

    def test_semantic_contract_errors_are_not_wrapped_as_internal(self):
        cases = (
            ("CREATE TABLE t(id INT UNIQUE UNIQUE);", errors.DUPLICATE_CONSTRAINT),
            (
                "CREATE TABLE t(id INT); UPDATE t SET id=1,id=2;",
                errors.DUPLICATE_UPDATE_COLUMN,
            ),
        )
        for sql, expected_code in cases:
            with self.subTest(code=expected_code), tempfile.TemporaryDirectory() as directory:
                session = Session.open(str(Path(directory) / "errors.db"))
                try:
                    with self.assertRaises(errors.DbError) as caught:
                        session.execute_text(sql)
                    self.assertEqual(caught.exception.code, expected_code)
                    self.assertIsNone(caught.exception.__cause__)
                finally:
                    session.abort()


if __name__ == "__main__":
    unittest.main()
