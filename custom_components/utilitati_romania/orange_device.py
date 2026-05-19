from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMENIU
from .naming import build_provider_slug


def alias_loc_orange(nume: str | None, adresa: str | None, id_cont: str) -> str:
    return (nume or adresa or id_cont or "Orange").strip()


def slug_loc_orange(id_cont: str, alias: str, adresa: str | None = None) -> str:
    return build_provider_slug("orange", adresa or alias, id_cont)


def info_device_orange(entry_id: str, cont) -> DeviceInfo:
    alias = alias_loc_orange(getattr(cont, "nume", None), getattr(cont, "adresa", None), getattr(cont, "id_cont", "cont"))
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_orange_{getattr(cont, 'id_cont', 'cont')}")},
        name=f"Orange - {alias}",
        manufacturer="Orange România",
        model=getattr(cont, "tip_cont", None) or "Servicii telecom",
    )
