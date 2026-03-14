"""SQL safety utilities for identifier validation and comment escaping."""

import re


def validate_identifier(name: str) -> bool:
    """Validate that a string is a safe SQL identifier (catalog, schema, or table name)."""
    return bool(re.match(r'^[a-zA-Z0-9_]+$', name))


def quote_identifier(name: str) -> str:
    """Quote a single SQL identifier with backticks."""
    if not validate_identifier(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f"`{name}`"


def quote_full_name(full_name: str) -> str:
    """Validate and quote a dotted full table name (catalog.schema.table)."""
    parts = full_name.split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected catalog.schema.table, got: {full_name!r}")
    return ".".join(quote_identifier(p) for p in parts)


def escape_comment(text: str) -> str:
    """Escape a string for use in a SQL COMMENT literal (single-quoted)."""
    return text.replace("\\", "\\\\").replace("'", "\\'")
