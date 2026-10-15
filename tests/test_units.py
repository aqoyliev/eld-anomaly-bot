import pytest

from utils.eld.units import unit_key


@pytest.mark.parametrize(
    "label, expected",
    [
        # Docstring examples.
        ("0942  O/O", "0942"),
        ("2512 O/O", "2512"),
        ("002  ZM", "002"),
        ("1277  CROSS USA", "1277"),
        ("1137", "1137"),
        ("unit 775263 CARLOS PEREZ", "775263"),
        ("unit 228005 IVAN AGUILAR/JORGE ESPEJO", "228005"),
        ("147109.", "147109"),
        ("UTNI: 2631 GUELSO DORCANT / ALCE GARRY", "2631"),
        # Prefix handling.
        ("UNIT 1013 SOMEONE", "1013"),
        ("Unit   1013", "1013"),
        # Whitespace and punctuation.
        ("  1137  ", "1137"),
        ("147109. JOHN DOE", "147109"),
        # Leading zeros are preserved (the ELD stores them as text).
        ("0007", "0007"),
        # Fallback: first run of digits anywhere.
        ("TRUCK #418", "418"),
        ("TRUCK 418 B", "418"),
        # No digits at all: the stripped head is returned.
        ("SPARE", "SPARE"),
        ("SPARE.", "SPARE"),
    ],
)
def test_unit_key(label, expected):
    assert unit_key(label) == expected


def test_unit_key_is_idempotent():
    for label in ("0942  O/O", "unit 775263 CARLOS PEREZ", "TRUCK #418"):
        once = unit_key(label)
        assert unit_key(once) == once
