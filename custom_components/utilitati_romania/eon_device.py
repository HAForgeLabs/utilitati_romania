from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMENIU
from .naming import build_location_short_name, build_provider_slug, slugify_text


def _raw_cont(cont) -> dict[str, Any]:
    raw = getattr(cont, "date_brute", None)
    return raw if isinstance(raw, dict) else {}


def tip_serviciu_eon(cont) -> str:
    """Returnează tipul normalizat al serviciului E.ON: curent, gaz sau serviciu."""
    raw = _raw_cont(cont)
    contract = raw.get("date_contract") if isinstance(raw.get("date_contract"), dict) else {}

    valori = (
        getattr(cont, "tip_serviciu", None),
        getattr(cont, "tip_utilitate", None),
        raw.get("tip_serviciu"),
        raw.get("tip_utilitate_cod"),
        raw.get("utilityType"),
        contract.get("utilityType"),
        contract.get("portfolio"),
        contract.get("productName"),
    )

    for valoare in valori:
        text = str(valoare or "").strip().lower()
        if not text:
            continue
        if text in {"02", "gn"} or "gaz" in text or "gas" in text:
            return "gaz"
        if text in {"01", "ee", "en"} or "electric" in text or "curent" in text or "energie" in text:
            return "curent"

    meter_index = raw.get("meter_index") if isinstance(raw.get("meter_index"), dict) else {}
    unitate = str(meter_index.get("um") or meter_index.get("unit") or raw.get("unitate_index") or "").strip().lower()
    if unitate.startswith("m"):
        return "gaz"
    if "kwh" in unitate or "kw" in unitate:
        return "curent"

    return "serviciu"


def cheie_serviciu_eon(cont) -> str:
    """Cheie scurtă, stabilă, folosită în unique_id/entity_id pentru serviciile E.ON."""
    tip = tip_serviciu_eon(cont)
    if tip == "gaz":
        return "gaz"
    if tip == "curent":
        return "energie_electrica"
    raw = _raw_cont(cont)
    contract = raw.get("date_contract") if isinstance(raw.get("date_contract"), dict) else {}
    fallback = (
        raw.get("tip_utilitate_cod")
        or raw.get("utilityType")
        or contract.get("utilityType")
        or getattr(cont, "tip_serviciu", None)
        or getattr(cont, "tip_utilitate", None)
        or "serviciu"
    )
    return slugify_text(str(fallback))


def id_unic_eon(cont) -> str:
    """Identificator intern pentru un serviciu E.ON.

    La contractele DUO, curentul și gazul pot avea aceeași adresă/alias.
    De aceea nu folosim doar locația în unique_id, ci combinăm codul de contract cu tipul serviciului.
    """
    raw = _raw_cont(cont)
    contract = raw.get("date_contract") if isinstance(raw.get("date_contract"), dict) else {}
    parti = [
        getattr(cont, "id_cont", None),
        getattr(cont, "id_contract", None),
        raw.get("cod_contract"),
        raw.get("accountContract"),
        contract.get("accountContract"),
        contract.get("pod"),
        raw.get("id_intern_contor"),
    ]
    baza = next((str(p).strip() for p in parti if str(p or "").strip()), "cont")
    return f"{slugify_text(baza)}_{cheie_serviciu_eon(cont)}"


def alias_loc_eon(nume: str | None, adresa: str | None, id_cont: str | None) -> str:
    return build_location_short_name(adresa or nume, nume or id_cont or "Cont")


def slug_loc_eon(id_cont: str | None, alias: str | None, adresa: str | None = None) -> str:
    return build_provider_slug("eon", adresa or alias, id_cont or alias or "cont")


def slug_serviciu_loc_eon(cont) -> str:
    alias = alias_loc_eon(getattr(cont, "nume", None), getattr(cont, "adresa", None), getattr(cont, "id_cont", None))
    baza = slug_loc_eon(getattr(cont, "id_cont", None), alias, getattr(cont, "adresa", None))
    return f"{baza}_{cheie_serviciu_eon(cont)}"


def info_device_eon(entry_id: str, cont) -> DeviceInfo:
    alias = alias_loc_eon(getattr(cont, "nume", None), getattr(cont, "adresa", None), getattr(cont, "id_cont", None))
    cheie_serviciu = cheie_serviciu_eon(cont)
    eticheta = "gaz" if cheie_serviciu == "gaz" else "energie electrică" if cheie_serviciu == "energie_electrica" else "serviciu"
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_eon_{id_unic_eon(cont)}")},
        name=f"E.ON – {alias} – {eticheta}",
        manufacturer="onitium",
        model="E.ON România",
    )
