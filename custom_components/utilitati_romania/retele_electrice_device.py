from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMENIU
from .naming import build_location_alias, build_provider_slug


def alias_loc_retele_electrice(nume: str | None, adresa: str | None, pod: str | None) -> str:
    return build_location_alias(adresa or nume, pod)


def slug_loc_retele_electrice(pod: str | None, alias: str | None, adresa: str | None = None) -> str:
    return build_provider_slug("retele_electrice", alias or adresa, pod)


def info_device_retele_electrice(entry_id: str, cont) -> DeviceInfo:
    alias = alias_loc_retele_electrice(
        getattr(cont, "nume", None),
        getattr(cont, "adresa", None),
        getattr(cont, "id_cont", None),
    )
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_retele_electrice_{cont.id_cont}")},
        name=f"Retele Electrice - {alias}",
        manufacturer="Retele Electrice Romania",
        model="Contul meu",
    )
