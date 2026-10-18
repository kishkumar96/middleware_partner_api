"""A track in the form the RiskScape impact models read it.

The SPC serves times as "2026-10-17 12:00:00"; the models' time parser only
accepts "2026-10-17T12:00:00Z", and a run on an SPC track as it is drops every
point and still reports complete, with empty results. Converting here, rather
than in the model, leaves the NIWA model untouched and needs no file uploads.
"""

import copy
import re

_TIME_KEYS = ("Time", "TIME")
_SPACE_TIME = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*Z?\s*$")


def iso_utc_time(value):
    """"2026-10-17 12:00:00" -> "2026-10-17T12:00:00Z"; anything else is left alone."""
    if not isinstance(value, str):
        return value
    match = _SPACE_TIME.match(value)
    return f"{match.group(1)}T{match.group(2)}Z" if match else value


def model_ready_geometry(geometry):
    """The track's point FeatureCollection with every point's time in ISO-UTC form."""
    if not isinstance(geometry, dict):
        return geometry
    ready = copy.deepcopy(geometry)
    for feature in ready.get("features") or []:
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        for key in _TIME_KEYS:
            if key in properties:
                properties[key] = iso_utc_time(properties[key])
    return ready
