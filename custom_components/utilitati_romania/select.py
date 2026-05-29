from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_FURNIZOR,
    CONF_MOBILE_NOTIFY_SERVICE,
    DOMENIU,
    FURNIZOR_ADMIN_GLOBAL,
)


_NOTIFY_OPTION_NONE = "none"


def _admin_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMENIU, entry.entry_id)},
        name="Administrare integrare",
        manufacturer="onitium",
        model="Utilitati Romania",
        entry_type=None,
    )


def _mobile_notify_service_names(hass: HomeAssistant) -> list[str]:
    services = hass.services.async_services().get("notify", {})
    options = sorted(
        service_name
        for service_name in services.keys()
        if str(service_name).startswith("mobile_app_")
    )
    return [_NOTIFY_OPTION_NONE, *options]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_FURNIZOR) != FURNIZOR_ADMIN_GLOBAL:
        return

    async_add_entities([SelectorDispozitivMobilOpenProvider(hass, entry)])


class SelectorDispozitivMobilOpenProvider(RestoreEntity, SelectEntity):
    _attr_icon = "mdi:cellphone-link"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_admin_dispozitiv_mobil_open_provider"
        self._attr_name = "Dispozitiv mobil pentru deschidere furnizori"
        self._attr_device_info = _admin_device_info(entry)
        self._attr_options = _mobile_notify_service_names(hass)
        self._attr_current_option = self._option_from_config_entry()

    def _option_from_config_entry(self) -> str:
        value = str(self._entry.options.get(CONF_MOBILE_NOTIFY_SERVICE) or "").strip()
        return value or _NOTIFY_OPTION_NONE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        options = _mobile_notify_service_names(self.hass)
        self._attr_options = options

        stored_value = self._option_from_config_entry()
        if stored_value in options:
            self._attr_current_option = stored_value
            self.async_write_ha_state()
            return

        restored_state = await self.async_get_last_state()
        restored_value = str(restored_state.state).strip() if restored_state else ""
        if restored_value in options:
            self._attr_current_option = restored_value
            await self._async_save_selected_option(restored_value)
        else:
            self._attr_current_option = _NOTIFY_OPTION_NONE

        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        options = _mobile_notify_service_names(self.hass)
        self._attr_options = options
        selected_option = option if option in options else _NOTIFY_OPTION_NONE
        self._attr_current_option = selected_option
        await self._async_save_selected_option(selected_option)
        self.async_write_ha_state()

    async def _async_save_selected_option(self, option: str) -> None:
        current_options = dict(self._entry.options)
        current_options[CONF_MOBILE_NOTIFY_SERVICE] = option
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=current_options,
        )
