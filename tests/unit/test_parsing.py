import pytest

from app.ingest.parsing import (
    normalize_bank_id,
    parse_attempt_header,
    parse_patterns_lines,
    split_entity_type,
)


@pytest.mark.parametrize(
    ("text", "typology", "n"),
    [
        ("CYCLE:  Max 4 hops", "CYCLE", 4),
        ("GATHER-SCATTER:  Max 3-degree Fan-In", "GATHER-SCATTER", 3),
        ("BIPARTITE", "BIPARTITE", None),
        ("FAN-OUT:  Max 16-degree Fan-Out", "FAN-OUT", 16),
        ("SCATTER-GATHER", "SCATTER-GATHER", None),
        ("STACK", "STACK", None),
        ("RANDOM:  Max 7 hops", "RANDOM", 7),
        ("FAN-IN:  Max 12-degree Fan-In", "FAN-IN", 12),
    ],
)
def test_parse_attempt_header(text: str, typology: str, n: int | None) -> None:
    h = parse_attempt_header(text)
    assert (h.typology, h.max_degree_or_hops, h.detail_raw) == (typology, n, text)


@pytest.mark.parametrize("bad", ["", "cycle: Max 4 hops", "CYCLE: nonsense"])
def test_parse_attempt_header_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_attempt_header(bad)


def test_normalize_bank_id() -> None:
    assert normalize_bank_id("010") == 10
    assert normalize_bank_id("00220") == 220
    assert normalize_bank_id("1") == 1
    with pytest.raises(ValueError):
        normalize_bank_id("0x1")


def test_split_entity_type() -> None:
    assert split_entity_type("Corporation #33520") == "Corporation"
    assert split_entity_type("Sole Proprietorship #50438") == "Sole Proprietorship"
    with pytest.raises(ValueError):
        split_entity_type("NoHash")


LINE = "2022/09/01 00:06,021174,800737690,012,80011F990,1.00,Euro,1.00,Euro,ACH,1"


def test_parse_patterns_lines() -> None:
    text = [
        "BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 2-degree Fan-Out",
        LINE,
        LINE,
        "END LAUNDERING ATTEMPT - FAN-OUT",
        "",
        "BEGIN LAUNDERING ATTEMPT - STACK",
        LINE,
        "END LAUNDERING ATTEMPT - STACK",
    ]
    attempts = list(parse_patterns_lines(text))
    assert [a.attempt_id for a in attempts] == [1, 2]
    assert [len(a.lines) for a in attempts] == [2, 1]
    assert attempts[1].header.max_degree_or_hops is None
    assert len(attempts[0].lines[0]) == 11


@pytest.mark.parametrize(
    "bad",
    [
        [LINE],
        ["BEGIN LAUNDERING ATTEMPT - STACK", "a,b"],
        ["BEGIN LAUNDERING ATTEMPT - STACK", LINE],
        ["END LAUNDERING ATTEMPT - STACK"],
    ],
)
def test_parse_patterns_lines_malformed(bad: list[str]) -> None:
    with pytest.raises(ValueError):
        list(parse_patterns_lines(bad))
