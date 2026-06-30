from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_FURNIZOR,
    CONF_MOBILE_NOTIFICATION_SERVICE,
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


def _mobile_notify_service_names(
    hass: HomeAssistant,
    stored_value: str | None = None,
) -> list[str]:
    services = hass.services.async_services().get("notify", {})
    options = sorted(
        service_name
        for service_name in services.keys()
        if str(service_name).startswith("mobile_app_")
    )

    stored = str(stored_value or "").strip()
    if stored and stored != _NOTIFY_OPTION_NONE and stored.startswith("mobile_app_") and stored not in options:
        options.append(stored)
        options.sort()

    return [_NOTIFY_OPTION_NONE, *options]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_FURNIZOR) != FURNIZOR_ADMIN_GLOBAL:
        return

    async_add_entities(
        [
            SelectorDispozitivMobil(
                hass,
                entry,
                option_key=CONF_MOBILE_NOTIFY_SERVICE,
                unique_suffix="admin_dispozitiv_mobil_open_provider",
                name="Dispozitiv mobil pentru deschidere furnizori",
                icon="mdi:cellphone-link",
            ),
            SelectorDispozitivMobil(
                hass,
                entry,
                option_key=CONF_MOBILE_NOTIFICATION_SERVICE,
                unique_suffix="admin_dispozitiv_mobil_notificari",
                name="Dispozitiv mobil pentru notificari",
                icon="mdi:cellphone-message",
            ),
        ]
    )


class SelectorDispozitivMobil(RestoreEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        option_key: str,
        unique_suffix: str,
        name: str,
        icon: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._option_key = option_key
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = _admin_device_info(entry)
        stored_value = self._option_from_config_entry()
        self._attr_options = _mobile_notify_service_names(hass, stored_value)
        self._attr_current_option = stored_value if stored_value in self._attr_options else _NOTIFY_OPTION_NONE
        self._remove_service_listener = None

    def _option_from_config_entry(self) -> str:
        value = str(self._entry.options.get(self._option_key) or "").strip()
        return value or _NOTIFY_OPTION_NONE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self._remove_service_listener = self.hass.bus.async_listen(
            "service_registered",
            self._async_notify_service_registered,
        )

        await self._async_refresh_options(write_state=False)

        stored_value = self._option_from_config_entry()
        if stored_value in self._attr_options:
            self._attr_current_option = stored_value
            self.async_write_ha_state()
            return

        restored_state = await self.async_get_last_state()
        restored_value = str(restored_state.state).strip() if restored_state else ""
        if restored_value in self._attr_options:
            self._attr_current_option = restored_value
            await self._async_save_selected_option(restored_value)
        else:
            self._attr_current_option = _NOTIFY_OPTION_NONE

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_service_listener is not None:
            self._remove_service_listener()
            self._remove_service_listener = None

    @callback
    def _async_notify_service_registered(self, event) -> None:
        if event.data.get("domain") != "notify":
            return
        self.hass.async_create_task(self._async_refresh_options())

    async def _async_refresh_options(self, write_state: bool = True) -> None:
        stored_value = self._option_from_config_entry()
        current_value = str(self._attr_current_option or "").strip()
        preferred_value = stored_value or current_value

        options = _mobile_notify_service_names(self.hass, preferred_value)
        self._attr_options = options

        if preferred_value in options:
            self._attr_current_option = preferred_value
        elif current_value in options:
            self._attr_current_option = current_value
        else:
            self._attr_current_option = _NOTIFY_OPTION_NONE

        if write_state:
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        options = _mobile_notify_service_names(self.hass, option)
        self._attr_options = options
        selected_option = option if option in options else _NOTIFY_OPTION_NONE
        self._attr_current_option = selected_option
        await self._async_save_selected_option(selected_option)
        self.async_write_ha_state()

    async def _async_save_selected_option(self, option: str) -> None:
        current_options = dict(self._entry.options)
        current_options[self._option_key] = option
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=current_options,
        )
