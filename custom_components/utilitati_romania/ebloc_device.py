from __future__ import annotations

import re
import unicodedata
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import device_registry as dr

from .const import DOMENIU


def _normalizeaza_text(valoare: Any) -> str:
    text = str(valoare or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def _date_cont(cont) -> dict[str, Any]:
    date_brute = getattr(cont, "date_brute", None)
    return date_brute if isinstance(date_brute, dict) else {}


def _apartament(cont) -> dict[str, Any]:
    data = _date_cont(cont).get("apartament")
    return data if isinstance(data, dict) else {}


def _asociatie(cont) -> dict[str, Any]:
    data = _date_cont(cont).get("asociatie")
    return data if isinstance(data, dict) else {}


def _nr_apartament(cont, id_cont: str | None = None) -> str:
    apartament = _apartament(cont)
    valoare = (
        apartament.get("ap")
        or apartament.get("nr_ap")
        or apartament.get("apartament")
        or _date_cont(cont).get("numar_apartament")
    )

    if valoare not in (None, ""):
        return str(valoare).strip()

    # id_cont are formatul id_asociatie_id_apartament; nu e perfect, dar e stabil ca fallback.
    id_curat = str(id_cont or getattr(cont, "id_cont", "") or "").strip()
    if "_" in id_curat:
        return id_curat.rsplit("_", 1)[-1]

    return id_curat or "necunoscut"


def _strada_si_numar(cont) -> tuple[str, str]:
    asociatie = _asociatie(cont)

    strada = (
        asociatie.get("adr_strada")
        or asociatie.get("strada")
        or asociatie.get("adresa_strada")
        or ""
    )
    numar = (
        asociatie.get("adr_nr")
        or asociatie.get("numar")
        or asociatie.get("nr")
        or ""
    )

    strada = str(strada or "").strip()
    numar = str(numar or "").strip()

    if strada:
        return strada, numar

    adresa = str(getattr(cont, "adresa", None) or "").strip()
    return adresa, ""


def slug_loc_ebloc(id_cont: str | None, nume: str | None, adresa: str | None, cont=None) -> str:
    if cont is not None:
        strada, numar = _strada_si_numar(cont)
        apartament = _nr_apartament(cont, id_cont)

        parti = ["ebloc"]
        strada_slug = _normalizeaza_text(strada)
        numar_slug = _normalizeaza_text(numar)
        apartament_slug = _normalizeaza_text(apartament)

        if strada_slug:
            parti.append(strada_slug)
        if numar_slug:
            parti.append(numar_slug)
        if apartament_slug:
            parti.append(f"ap{apartament_slug}")

        rezultat = "_".join(parti)
        if rezultat != "ebloc":
            return rezultat

    baza = _normalizeaza_text(adresa) or _normalizeaza_text(nume) or _normalizeaza_text(id_cont)
    apartament = _normalizeaza_text(id_cont)
    if apartament and apartament not in baza:
        baza = f"{baza}_ap{apartament}" if baza else f"ap{apartament}"
    return f"ebloc_{baza}" if baza and not baza.startswith("ebloc_") else (baza or "ebloc_loc_consum")


def alias_loc_ebloc(nume: str | None, adresa: str | None, id_cont: str | None, cont=None) -> str:
    if cont is not None:
        strada, numar = _strada_si_numar(cont)
        apartament = _nr_apartament(cont, id_cont)

        locatie = " ".join(part for part in (strada, numar) if part).strip()
        if locatie and apartament:
            return f"{locatie} Ap. {apartament}"
        if locatie:
            return locatie
        if apartament:
            return f"Ap. {apartament}"

    adresa_curata = str(adresa or "").strip()
    id_curat = str(id_cont or "").strip()

    if adresa_curata and id_curat:
        return f"{adresa_curata} {id_curat}"

    if adresa_curata:
        return adresa_curata

    nume_curat = str(nume or "").strip()
    if nume_curat:
        return nume_curat

    return f"Loc consum {id_curat}" if id_curat else "Loc consum"


def info_device_ebloc(hass, entry_id: str, cont) -> DeviceInfo:
    ident = str(getattr(cont, "id_cont", None) or "ebloc")
    nume = alias_loc_ebloc(
        getattr(cont, "nume", None),
        getattr(cont, "adresa", None),
        ident,
        cont=cont,
    )

    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_ebloc_{ident}")},
        name=f"e-bloc.ro - {nume}",
        manufacturer="e-bloc.ro",
        model="Administrare bloc",
        via_device_id=dr.async_get_device_id_by_identifier(
            hass, (DOMENIU, entry_id), config_entry_id=entry_id
        ),
    )
