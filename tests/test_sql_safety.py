"""Tests for SQL escaping and identifier validation."""

import pytest


class TestIdentifierValidation:

    def test_valid_three_part_name(self):
        from server.sql_utils import validate_identifier
        assert validate_identifier("my_catalog.my_schema.my_table") == True

    def test_rejects_semicolon(self):
        from server.sql_utils import validate_identifier
        with pytest.raises(ValueError):
            validate_identifier("catalog; DROP TABLE--")

    def test_rejects_comment_injection(self):
        from server.sql_utils import validate_identifier
        with pytest.raises(ValueError):
            validate_identifier("catalog--comment")

    def test_allows_hyphens(self):
        from server.sql_utils import validate_identifier
        assert validate_identifier("my-catalog.my-schema.my-table") == True

    def test_allows_underscores_and_dots(self):
        from server.sql_utils import validate_identifier
        assert validate_identifier("cat_1.sch_2.tbl_3") == True


class TestCommentEscaping:

    def test_escapes_single_quotes(self):
        from server.sql_utils import escape_comment
        assert "\\'" in escape_comment("it's a test")

    def test_escapes_backslashes(self):
        from server.sql_utils import escape_comment
        assert "\\\\" in escape_comment("path\\to\\thing")

    def test_replaces_newlines(self):
        from server.sql_utils import escape_comment
        result = escape_comment("line1\nline2")
        assert "\n" not in result


class TestQuoteIdentifier:

    def test_backtick_wraps_parts(self):
        from server.sql_utils import quote_identifier
        assert quote_identifier("cat.sch.tbl") == "`cat`.`sch`.`tbl`"

    def test_escapes_backticks_in_names(self):
        from server.sql_utils import quote_identifier
        result = quote_identifier("cat.sch.my`table")
        assert "my``table" in result
