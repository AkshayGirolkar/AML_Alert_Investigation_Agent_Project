"""Pure parsing helpers for the IBM HI-Small files (F02).

Input files are untrusted data: everything here is string handling only.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

_HEADER_RE = re.compile(r"^(?P<typology>[A-Z][A-Z-]*)(?::(?P<rest>.*))?$")
_N_RE = re.compile(r"Max\s+(\d+)(?:-degree|\s+hops)")
_BEGIN = "BEGIN LAUNDERING ATTEMPT - "
_END = "END LAUNDERING ATTEMPT"
PATTERN_LINE_FIELDS = 11


def normalize_bank_id(raw: str) -> int:
    """Zero-padded bank code ('010', '00220') -> int."""
    value = raw.strip()
    if not value.isdigit():
        raise ValueError(f"invalid bank id: {raw!r}")
    return int(value)


def split_entity_type(entity_name: str) -> str:
    """'Corporation #33520' -> 'Corporation'."""
    if " #" not in entity_name:
        raise ValueError(f"entity name has no ' #': {entity_name!r}")
    entity_type = entity_name.split(" #", 1)[0].strip()
    if not entity_type:
        raise ValueError(f"empty entity type: {entity_name!r}")
    return entity_type


@dataclass(frozen=True)
class AttemptHeader:
    typology: str
    max_degree_or_hops: int | None
    detail_raw: str


def parse_attempt_header(text: str) -> AttemptHeader:
    """Parse the text after 'BEGIN LAUNDERING ATTEMPT - '.

    'FAN-OUT:  Max 16-degree Fan-Out' -> ('FAN-OUT', 16, full text).
    BIPARTITE / SCATTER-GATHER / STACK have no N -> None.
    """
    detail = text.strip()
    match = _HEADER_RE.match(detail)
    if not match:
        raise ValueError(f"unparseable attempt header: {text!r}")
    rest = match.group("rest")
    n: int | None = None
    if rest is not None:
        n_match = _N_RE.search(rest)
        if not n_match:
            raise ValueError(f"unparseable attempt header: {text!r}")
        n = int(n_match.group(1))
    return AttemptHeader(match.group("typology"), n, detail)


@dataclass
class Attempt:
    attempt_id: int  # 1-based order of BEGIN blocks
    header: AttemptHeader
    lines: list[list[str]] = field(default_factory=list)  # 11 raw fields each


def parse_patterns_lines(lines: Iterator[str] | list[str]) -> Iterator[Attempt]:
    """Yield attempts in file order. Raises on malformed structure."""
    current: Attempt | None = None
    count = 0
    for lineno, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith(_BEGIN):
            if current is not None:
                raise ValueError(f"line {lineno}: BEGIN inside an open attempt")
            count += 1
            current = Attempt(count, parse_attempt_header(line[len(_BEGIN) :]))
        elif line.startswith(_END):
            if current is None:
                raise ValueError(f"line {lineno}: END without BEGIN")
            yield current
            current = None
        else:
            if current is None:
                raise ValueError(f"line {lineno}: transaction outside an attempt")
            fields = line.split(",")
            if len(fields) != PATTERN_LINE_FIELDS:
                raise ValueError(
                    f"line {lineno}: expected {PATTERN_LINE_FIELDS} fields, "
                    f"got {len(fields)}"
                )
            current.lines.append(fields)
    if current is not None:
        raise ValueError("file ended inside an open attempt")


def parse_patterns_file(path: str) -> Iterator[Attempt]:
    with open(path, encoding="utf-8") as fh:
        yield from parse_patterns_lines(fh)
