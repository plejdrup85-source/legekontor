"""Delt rate-limiter for både V1 (app.py) og V2 (v2/router.py).

Ligger i egen modul for å unngå sirkulær import mellom app.py og v2.router.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address


def _client_key(request) -> str:
    """Nøkkel per klient. Foretrekker X-Forwarded-For (Render/proxy) og faller
    tilbake til direkte klient-IP."""
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    except Exception:
        pass
    return get_remote_address(request)


limiter = Limiter(key_func=_client_key)
