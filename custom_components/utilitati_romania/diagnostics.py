from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHEIE_LICENTA,
    CONF_DIGI_COOKIES,
    CONF_PAROLA,
    DATE_VERIFICARE_LICENTA,
    DOMENIU,
)
from .licentiere import mascheaza_cheia_licenta


def _mascheaza_cookies(cookies: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rezultat: list[dict[str, Any]] = []
    for item in cookies or []:
        rezultat.append(
            {
                "key": item.get("key"),
                "value": "***",
                "domain": item.get("domain"),
                "path": item.get("path"),
                "secure": item.get("secure"),
                "expires": item.get("expires"),
            }
        )
    return rezultat


def _valoare_serializabila(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _rezuma_instantaneu(instantaneu: Any | None) -> dict[str, Any] | None:
    if instantaneu is None:
        return None

    conturi = list(getattr(instantaneu, "conturi", []) or [])
    facturi = list(getattr(instantaneu, "facturi", []) or [])
    consumuri = list(getattr(instantaneu, "consumuri", []) or [])

    return {
        "furnizor": getattr(instantaneu, "furnizor", None),
        "conturi": len(conturi),
        "facturi": len(facturi),
        "consumuri": len(consumuri),
        "extra": getattr(instantaneu, "extra", None),
        "conturi_preview": [
            {
                "id_cont": getattr(cont, "id_cont", None),
                "nume": getattr(cont, "nume", None),
                "adresa": getattr(cont, "adresa", None),
                "id_contract": getattr(cont, "id_contract", None),
                "tip_serviciu": getattr(cont, "tip_serviciu", None),
            }
            for cont in conturi[:10]
        ],
        "facturi_preview": [
            {
                "id_factura": getattr(factura, "id_factura", None),
                "id_cont": getattr(factura, "id_cont", None),
                "id_contract": getattr(factura, "id_contract", None),
                "valoare": getattr(factura, "valoare", None),
                "data_emitere": _valoare_serializabila(getattr(factura, "data_emitere", None)),
                "data_scadenta": _valoare_serializabila(getattr(factura, "data_scadenta", None)),
                "stare": getattr(factura, "stare", None),
                "rest_plata": (getattr(factura, "date_brute", {}) or {}).get("rest_plata"),
            }
            for factura in facturi[:20]
        ],
        "consumuri_preview": [
            {
                "id_cont": getattr(consum, "id_cont", None),
                "cheie": getattr(consum, "cheie", None),
                "valoare": getattr(consum, "valoare", None),
                "unitate": getattr(consum, "unitate", None),
            }
            for consum in consumuri[:20]
        ],
    }


def _coordonator_din_runtime(runtime: Any) -> Any | None:
    if runtime is None:
        return None
    if hasattr(runtime, "data"):
        return runtime
    if isinstance(runtime, dict):
        posibil = runtime.get("coordinator") or runtime.get("coordonator")
        if hasattr(posibil, "data"):
            return posibil
    return None


def _rezuma_intrari_runtime(hass: HomeAssistant) -> list[dict[str, Any]]:
    rezultat: list[dict[str, Any]] = []
    for entry_id, runtime in (hass.data.get(DOMENIU, {}) or {}).items():
        if str(entry_id).startswith("_"):
            continue
        coordonator = _coordonator_din_runtime(runtime)
        if coordonator is None:
            if isinstance(runtime, dict):
                rezultat.append({"entry_id": entry_id, "runtime": {k: v for k, v in runtime.items() if k != "coordinator"}})
            continue
        instantaneu = getattr(coordonator, "data", None)
        rezultat.append(
            {
                "entry_id": entry_id,
                "furnizor": getattr(coordonator, "cheie_furnizor", None),
                "titlu": getattr(getattr(coordonator, "intrare", None), "title", None),
                "instantaneu": _rezuma_instantaneu(instantaneu),
            }
        )
    return rezultat


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    runtime = hass.data.get(DOMENIU, {}).get(entry.entry_id)
    coordonator = _coordonator_din_runtime(runtime)
    data = dict(entry.data)
    optiuni = dict(entry.options)
    for container in (data, optiuni):
        if CONF_PAROLA in container:
            container[CONF_PAROLA] = "***"
        if CONF_CHEIE_LICENTA in container:
            container[CONF_CHEIE_LICENTA] = mascheaza_cheia_licenta(container[CONF_CHEIE_LICENTA])
        if CONF_DIGI_COOKIES in container:
            container[CONF_DIGI_COOKIES] = _mascheaza_cookies(container[CONF_DIGI_COOKIES])
        if DATE_VERIFICARE_LICENTA in container and isinstance(container[DATE_VERIFICARE_LICENTA], dict):
            container[DATE_VERIFICARE_LICENTA] = {
                k: v
                for k, v in container[DATE_VERIFICARE_LICENTA].items()
                if k in {"valid", "status", "plan", "expires_at", "checked_at", "message", "connection_error"}
            }

    rezultat: dict[str, Any] = {
        "intrare": data,
        "optiuni": optiuni,
        "runtime": runtime if isinstance(runtime, dict) and coordonator is None else None,
        "instantaneu": None if coordonator is None else _rezuma_instantaneu(getattr(coordonator, "data", None)),
    }

    if isinstance(runtime, dict) and runtime.get("admin"):
        rezultat["intrari_runtime"] = _rezuma_intrari_runtime(hass)

    return rezultat
