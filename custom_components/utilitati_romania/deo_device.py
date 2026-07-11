from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMENIU
from .naming import build_location_short_name, build_provider_slug


def alias_loc_deo(nume: str | None, adresa: str | None, nlc: str) -> str:
    return build_location_short_name(adresa or nume, nume or f"NLC {nlc}")


def slug_loc_deo(nlc: str, alias: str, adresa: str | None = None) -> str:
    return build_provider_slug("deo", adresa or alias, nlc)


def info_device_deo(entry_id: str, cont) -> DeviceInfo:
    alias = alias_loc_deo(getattr(cont, "nume", None), getattr(cont, "adresa", None), getattr(cont, "id_cont", "nlc"))
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_deo_{getattr(cont, 'id_cont', 'nlc')}")},
        name=f"DEO - {alias}",
        manufacturer="Distributie Energie Oltenia",
        model="Portalul Utilizatorilor",
    )
