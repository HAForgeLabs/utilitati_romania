from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_STORAGE_VERSION = 1
_STORAGE_KEY = "utilitati_romania_distribution_supplier_links"
_CACHE_KEY = "_distribution_supplier_links"
_OPTIONS_KEY = "distribution_supplier_links"
_ADMIN_PROVIDER = "admin_global"


def _normalize_links(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for key, linked in value.items():
        source = str(key or "").strip()
        target = str(linked or "").strip()
        if source and target:
            normalized[source] = target
    return normalized


def _admin_entry(hass: HomeAssistant) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries("utilitati_romania"):
        if entry.data.get("furnizor") == _ADMIN_PROVIDER:
            return entry
    return None


async def async_incarca_asocieri_distributie(
    hass: HomeAssistant,
    *,
    force_reload: bool = False,
) -> dict[str, str]:
    domain_data = hass.data.setdefault("utilitati_romania", {})
    cached = domain_data.get(_CACHE_KEY)
    if not force_reload and isinstance(cached, dict):
        return dict(cached)

    admin_entry = _admin_entry(hass)
    if admin_entry is not None:
        option_links = _normalize_links(admin_entry.options.get(_OPTIONS_KEY))
        if option_links:
            domain_data[_CACHE_KEY] = option_links
            return dict(option_links)

    store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
    saved = await store.async_load()
    links = _normalize_links((saved or {}).get("links") if isinstance(saved, dict) else saved)

    if links and admin_entry is not None:
        new_options = dict(admin_entry.options)
        new_options[_OPTIONS_KEY] = links
        hass.config_entries.async_update_entry(admin_entry, options=new_options)

    domain_data[_CACHE_KEY] = links
    return dict(links)


async def async_salveaza_asocieri_distributie(
    hass: HomeAssistant,
    links: dict[str, str],
) -> dict[str, str]:
    normalized = _normalize_links(links)

    admin_entry = _admin_entry(hass)
    if admin_entry is not None:
        new_options = dict(admin_entry.options)
        new_options[_OPTIONS_KEY] = normalized
        hass.config_entries.async_update_entry(admin_entry, options=new_options)

    store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
    await store.async_save({"links": normalized})

    hass.data.setdefault("utilitati_romania", {})[_CACHE_KEY] = normalized
    return dict(normalized)
