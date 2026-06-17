from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMENIU
from .naming import normalize_text

_STORAGE_VERSION = 1
_STORAGE_KEY = "utilitati_romania_locuri_ignorate"
_DATA_KEY = "_locuri_consum_ignorate"
_PAYLOAD_KEY = "locuri"


def _domain_data(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(DOMENIU, {})


def _cache(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    data = _domain_data(hass)
    cache = data.setdefault(_DATA_KEY, {})
    return cache if isinstance(cache, dict) else {}


def _store(hass: HomeAssistant) -> Store:
    data = _domain_data(hass)
    store = data.get(f"{_DATA_KEY}_store")
    if store is None:
        store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        data[f"{_DATA_KEY}_store"] = store
    return store


def _clean(value: Any) -> str:
    return str(value or "").strip()


def construieste_cheie_loc_consum(
    entry_id: str | None,
    furnizor: str | None,
    id_cont: str | None = None,
    id_contract: str | None = None,
    locatie_cheie: str | None = None,
    eticheta: str | None = None,
) -> str | None:
    """Construiește cheia stabilă pentru un loc de consum administrabil.

    Folosim prioritar identificatorii tehnici. Cheia trebuie să fie stabilă între
    restarturi și suficient de generică pentru toți furnizorii.
    """
    entry = _clean(entry_id)
    provider = normalize_text(_clean(furnizor)).lower()
    if not entry or not provider:
        return None

    for prefix, value in (
        ("cont", id_cont),
        ("contract", id_contract),
        ("locatie", locatie_cheie),
        ("label", eticheta),
    ):
        text = normalize_text(_clean(value)).lower()
        if text:
            return f"{entry}:{provider}:{prefix}:{text}"

    return None


async def async_incarca_locuri_ignorate(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    loaded = await _store(hass).async_load()
    payload = loaded if isinstance(loaded, dict) else {}
    values = payload.get(_PAYLOAD_KEY, {}) if isinstance(payload, dict) else {}
    cache = _cache(hass)
    cache.clear()

    if isinstance(values, dict):
        for key, value in values.items():
            key_text = _clean(key)
            if not key_text:
                continue
            if isinstance(value, dict):
                cache[key_text] = dict(value)
            elif value is True:
                cache[key_text] = {"ignored": True}

    return dict(cache)


async def async_salveaza_locuri_ignorate(hass: HomeAssistant) -> None:
    await _store(hass).async_save({_PAYLOAD_KEY: dict(sorted(_cache(hass).items()))})


def obtine_locuri_ignorate(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    return dict(_cache(hass))


def este_loc_consum_ignorat(hass: HomeAssistant, cheie: str | None) -> bool:
    if not cheie:
        return False
    value = _cache(hass).get(cheie)
    return bool(isinstance(value, dict) and value.get("ignored") is True)


async def async_seteaza_loc_consum_ignorat(
    hass: HomeAssistant,
    *,
    cheie: str | None = None,
    entry_id: str | None = None,
    furnizor: str | None = None,
    id_cont: str | None = None,
    id_contract: str | None = None,
    locatie_cheie: str | None = None,
    eticheta: str | None = None,
    ignored: bool,
) -> str | None:
    key = _clean(cheie) or construieste_cheie_loc_consum(
        entry_id,
        furnizor,
        id_cont=id_cont,
        id_contract=id_contract,
        locatie_cheie=locatie_cheie,
        eticheta=eticheta,
    )
    if not key:
        return None

    cache = _cache(hass)
    if ignored:
        cache[key] = {
            "ignored": True,
            "entry_id": _clean(entry_id),
            "furnizor": _clean(furnizor).lower(),
            "id_cont": _clean(id_cont),
            "id_contract": _clean(id_contract),
            "locatie_cheie": _clean(locatie_cheie),
            "eticheta": _clean(eticheta),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        cache.pop(key, None)

    await async_salveaza_locuri_ignorate(hass)
    return key
