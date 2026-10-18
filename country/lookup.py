"""Find a Country from whatever a caller has: its code, or a name as someone wrote it."""

from .codes import code_for, normalise_code
from .models import Country


def find_country(identifier):
    """The Country meant by `identifier`, or None.

    In order: its canonical code ("CK"); a name the codes table knows, which reaches the right row
    whatever that row's own names say (RiskScape writes "Cook Islands", the live row says "Cook Island");
    then the row's own short or long name, case-insensitively, for rows with no code yet.
    """
    text = (identifier or "").strip()
    if not text:
        return None
    country = Country.objects.filter(code=normalise_code(text)).first()
    if country:
        return country
    implied = code_for(text, text)
    if implied:
        country = Country.objects.filter(code=implied).first()
        if country:
            return country
    return (
        Country.objects.filter(short_name__iexact=text).first()
        or Country.objects.filter(long_name__iexact=text).first()
    )
