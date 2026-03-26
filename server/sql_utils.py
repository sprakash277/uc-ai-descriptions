"""SQL safety utilities — escaping and identifier validation."""

import re


# Pattern for valid UC identifier parts: alphanumeric, underscore, hyphen
_VALID_IDENT_PART = re.compile(r"^[\w\-]+$")

# Dangerous patterns that should never appear in identifiers
_DANGEROUS_PATTERNS = re.compile(r"(--|;|/\*|\*/)")


def validate_identifier(name: str) -> bool:
    """Validate a dotted identifier (e.g., catalog.schema.table).

    Raises ValueError if the identifier contains dangerous patterns.
    Returns True if valid.
    """
    if _DANGEROUS_PATTERNS.search(name):
        raise ValueError(f"Invalid identifier — contains dangerous pattern: {name}")

    parts = name.split(".")
    for part in parts:
        # Strip backticks if already quoted
        clean = part.strip("`")
        if not clean:
            raise ValueError(f"Invalid identifier — empty part in: {name}")
        if not _VALID_IDENT_PART.match(clean):
            raise ValueError(
                f"Invalid identifier part '{clean}' in: {name}. "
                "Only alphanumeric, underscore, and hyphen are allowed."
            )
    return True


def quote_identifier(name: str) -> str:
    """Quote a dotted identifier with backticks (e.g., cat.sch.tbl -> `cat`.`sch`.`tbl`).

    Escapes any backticks within individual parts by doubling them.
    """
    parts = name.split(".")
    quoted = []
    for part in parts:
        clean = part.strip("`")
        escaped = clean.replace("`", "``")
        quoted.append(f"`{escaped}`")
    return ".".join(quoted)


def escape_comment(text: str) -> str:
    """Escape a string for use as a SQL string literal value.

    Handles single quotes, backslashes, and newlines.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    return text
