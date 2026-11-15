"""Telefonun bildirdigi son konum (kayitlara 'nerede' bilgisi eklemek icin).

Telefon baglaninca ve her ~2 dakikada bir {"type":"loc","name":..,"lat":..,"lon":..} yollar.
Konum 15 dakikadan eskiyse kayitlara yazilmaz (yanlis yer gostermesin).
"""

from __future__ import annotations

import time

_loc: dict = {}


def set_loc(name: str | None, lat: float | None, lon: float | None) -> None:
    _loc.update(name=name, lat=lat, lon=lon, at=time.time())


def fields() -> dict:
    """Kayda eklenecek alanlar: {"place": "Nilufer, Bursa", "lat": .., "lon": ..} ya da {}."""
    if not _loc or time.time() - _loc.get("at", 0) > 900:
        return {}
    return {"place": _loc.get("name"), "lat": _loc.get("lat"), "lon": _loc.get("lon")}
