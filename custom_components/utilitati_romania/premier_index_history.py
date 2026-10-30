"""Protecție persistentă pentru indexurile Premier Energy confirmate."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMENIU

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMENIU}.premier_energy_index_confirmations"


def _registry(hass: HomeAssistant) -> dict[str, Any]:
    key = f"{DOMENIU}_premier_index_registry"
    registry = hass.data.get(key)
    if registry is None:
        registry = {"store": Store(hass, _STORAGE_VERSION, _STORAGE_KEY), "lock": asyncio.Lock(), "data": None}
        hass.data[key] = registry
    return registry


def _key(entry_id: str, location_id: str) -> str:
    return f"{entry_id}:{location_id}"


async def async_check_confirmation(
    hass: HomeAssistant, entry_id: str, location_id: str, index: int, today: date
) -> bool:
    """Returnează True dacă aceeași citire a fost confirmată deja astăzi."""
    reg = _registry(hass)
    async with reg["lock"]:
        if reg["data"] is None:
            loaded = await reg["store"].async_load()
            reg["data"] = loaded if isinstance(loaded, dict) else {}
        record = reg["data"].get(_key(entry_id, location_id))
        return (isinstance(record, dict) and record.get("date") == today.isoformat()
                and record.get("index") == index)


async def async_record_confirmation(
    hass: HomeAssistant, entry_id: str, location_id: str,
    index: int, today: date, position: str
) -> None:
    """Persistă exclusiv indexurile confirmate de furnizor."""
    reg = _registry(hass)
    async with reg["lock"]:
        if reg["data"] is None:
            loaded = await reg["store"].async_load()
            reg["data"] = loaded if isinstance(loaded, dict) else {}
        key = _key(entry_id, location_id)
        updated = dict(reg["data"])
        updated[key] = {"index": index, "date": today.isoformat(), "position": str(position)}
        await reg["store"].async_save(updated)
        reg["data"] = updated
