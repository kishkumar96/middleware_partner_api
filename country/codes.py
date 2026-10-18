"""Canonical country codes.

Countries were identified only by `short_name` and `long_name`, which differ between deployments
and are inconsistent within one: the live API has "Ton" for Tonga, "FJI" for Fiji and "Cook Island"
(singular) for the Cook Islands, and a database seeded another way numbers the same countries
differently (Cook Islands is id 3 live and 1 locally). A client matching by name or id will
eventually pick the wrong country. `Country.code` is an ISO 3166-1 alpha-2 code ("CK") that means
the same everywhere.
"""

import re

CODE_PATTERN = re.compile(r"^[A-Z]{2}$")

# What the names and short names seen so far mean. Anything else is left blank for an operator to set.
_BY_LONG_NAME = {
    "fiji": "FJ",
    "vanuatu": "VU",
    "cook islands": "CK",
    "cook island": "CK",
    "tonga": "TO",
    "samoa": "WS",
}
_BY_SHORT_NAME = {
    "fj": "FJ",
    "fji": "FJ",
    "vu": "VU",
    "vut": "VU",
    "ck": "CK",
    "cok": "CK",
    "to": "TO",
    "ton": "TO",
    "ws": "WS",
    "wsm": "WS",
}


def normalise_code(value):
    """"  ck " -> "CK"; blank -> None (so several countries without a code do not clash on ""."""
    cleaned = (value or "").strip().upper()
    return cleaned or None


def code_for(short_name, long_name):
    """The code for a country known by these names, or None when they are not recognised."""
    by_long = _BY_LONG_NAME.get((long_name or "").strip().casefold())
    by_short = _BY_SHORT_NAME.get((short_name or "").strip().casefold())
    return by_long or by_short
