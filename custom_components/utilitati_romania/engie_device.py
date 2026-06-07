from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMENIU
from .naming import build_location_short_name, build_provider_slug, normalize_text


def alias_loc_engie(nume: str | None, adresa: str | None, id_cont: str) -> str:
    alias = normalize_text(nume)
    if alias:
        return alias
    return build_location_short_name(adresa, id_cont)


def slug_loc_engie(id_cont: str, alias: str, adresa: str | None = None) -> str:
    return build_provider_slug("engie", alias or adresa, id_cont)


def info_device_engie(entry_id: str, cont) -> DeviceInfo:
    alias = alias_loc_engie(getattr(cont, "nume", None), getattr(cont, "adresa", None), getattr(cont, "id_cont", "cont"))
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_engie_{getattr(cont, 'id_cont', 'cont')}" )},
        name=f"Engie - {alias}",
        manufacturer="ENGIE România",
        model="MyENGIE",
    )
