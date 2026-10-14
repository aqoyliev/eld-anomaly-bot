"""Unit-number normalization, shared by every provider.

A movement provider labels a truck with whatever the fleet typed into it
("0942  O/O", "unit 775263 CARLOS PEREZ"), while the ELD side stores the bare
number. Both sides are matched through :func:`unit_key`, so the same rules also
decide what an ignore-list entry matches (see store.ignore_key).
"""

import re


def unit_key(unit_number: str) -> str:
    """Derive the bare unit number the ELD stores from a provider's label.

    "0942  O/O" -> "0942", "2512 O/O" -> "2512", "002  ZM" -> "002",
    "1277  CROSS USA" -> "1277", "1137" -> "1137",
    "unit 775263 CARLOS PEREZ" -> "775263",
    "unit 228005 IVAN AGUILAR/JORGE ESPEJO" -> "228005",
    "147109." -> "147109",
    "UTNI: 2631 GUELSO DORCANT / ALCE GARRY" -> "2631".
    A wide (2+ space) gap usually separates the number from a tag; otherwise, a
    leading numeric token followed by a tag/driver name is reduced to just the
    number. Some providers also prefix the literal word "unit", which is dropped.
    A trailing dot (e.g. "147109.") is stray punctuation and is stripped too.
    """
    u = unit_number.strip()
    # Drop a literal "unit" prefix some providers prepend (e.g. "unit 1013 ...").
    u = re.sub(r"^unit\s+", "", u, flags=re.IGNORECASE)
    head = re.split(r"\s{2,}", u)[0].strip()  # drop tag after a wide gap
    tokens = head.split()
    # Strip a trailing dot so "147109." matches the ELD's bare "147109".
    if len(tokens) > 1 and tokens[0].rstrip(".").isdigit():
        return tokens[0].rstrip(".")  # e.g. "2512 O/O" -> "2512", "228005 IVAN/JORGE" -> "228005"
    head = head.rstrip(".")
    if head.isdigit():
        return head
    # The number is not the leading token: the "unit" prefix is misspelled or
    # punctuated ("UTNI: 2631 ...", "TRUCK #418"), so the rules above leave the
    # whole driver-name label in place and it would be looked up as the unit
    # number. Fall back to the first run of digits anywhere in the label.
    match = re.search(r"\d+", u)
    return match.group(0) if match else head
