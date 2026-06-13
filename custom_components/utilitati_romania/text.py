from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_FURNIZOR, DOMENIU, FURNIZOR_ADMIN_GLOBAL
from .coordonator import CoordonatorUtilitatiRomania
from .grupare_facturi import async_obtine_grupare_factura, async_seteaza_grupare_factura
from .helpers_facturi_locatie import build_facturi_location_label, normalize_facturi_location_key
from .licentiere import async_obtine_licenta_globala


def _admin_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMENIU, entry.entry_id)},
        name="Administrare integrare",
        manufacturer="onitium",
        model="Utilitati Romania",
        entry_type=None,
    )


def _grupare_facturi_device_info() -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMENIU, "grupare_facturi")},
        name="Grupare facturi",
        manufacturer="onitium",
        model="Utilitati Romania",
        entry_type=None,
    )


def _colecteaza_entitati_grupare(
    hass: HomeAssistant,
) -> list["TextGrupareFacturi"]:
    entitati: list[TextGrupareFacturi] = []

    for existing_entry in hass.config_entries.async_entries(DOMENIU):
        if existing_entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
            continue

        coordonator = hass.data.get(DOMENIU, {}).get(existing_entry.entry_id)
        if not isinstance(coordonator, CoordonatorUtilitatiRomania):
            continue

        for cont in getattr(coordonator.data, "conturi", None) or []:
            if getattr(cont, "id_cont", None):
                entitati.append(TextGrupareFacturi(coordonator, cont))

    entitati.sort(
        key=lambda entitate: (
            str(entitate.provider_name).casefold(),
            str(entitate.location_alias).casefold(),
            str(entitate.id_cont).casefold(),
        )
    )

    return entitati


async def async_adauga_entitati_grupare_pentru_intrare(
    hass: HomeAssistant,
    entry_id: str,
) -> int:
    """Adaugă dinamic entitățile lipsă pentru gruparea facturilor unui furnizor.

    Entitățile de grupare sunt expuse pe intrarea globală „Administrare integrare”.
    Când un furnizor nou este adăugat după ce administrarea este deja încărcată,
    platforma text nu mai este recreată automat. De aceea păstrăm callback-ul
    `async_add_entities` al platformei globale și îl folosim pentru a adăuga doar
    entitățile lipsă, fără să dublăm entitățile existente.
    """
    data = hass.data.get(DOMENIU, {})
    async_add_entities = data.get("_grupare_facturi_async_add_entities")
    if async_add_entities is None:
        return 0

    coordonator = data.get(entry_id)
    if not isinstance(coordonator, CoordonatorUtilitatiRomania):
        return 0

    registry = er.async_get(hass)
    entitati: list[TextGrupareFacturi] = []

    for cont in getattr(coordonator.data, "conturi", None) or []:
        if not getattr(cont, "id_cont", None):
            continue

        entitate = TextGrupareFacturi(coordonator, cont)
        if registry.async_get_entity_id("text", DOMENIU, entitate.unique_id):
            continue

        entitati.append(entitate)

    if not entitati:
        return 0

    entitati.sort(
        key=lambda entitate: (
            str(entitate.provider_name).casefold(),
            str(entitate.location_alias).casefold(),
            str(entitate.id_cont).casefold(),
        )
    )
    async_add_entities(entitati)
    return len(entitati)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_FURNIZOR) != FURNIZOR_ADMIN_GLOBAL:
        return

    hass.data.setdefault(DOMENIU, {})["_grupare_facturi_async_add_entities"] = async_add_entities

    entitati: list[TextEntity] = [TextCodLicentaNoua(entry)]
    entitati.extend(_colecteaza_entitati_grupare(hass))

    async_add_entities(entitati)


class TextCodLicentaNoua(RestoreEntity, TextEntity):
    _attr_icon = "mdi:key-outline"
    _attr_native_min = 0
    _attr_native_max = 128
    _attr_mode = "text"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_admin_cod_licenta_noua"
        self._attr_name = "Cod licență nou"
        self._attr_suggested_object_id = f"{DOMENIU}_cod_licenta_noua"
        self.entity_id = f"text.{DOMENIU}_cod_licenta_noua"
        self._attr_device_info = _admin_device_info(entry)
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_native_value = ""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        storage = await async_obtine_licenta_globala(self.hass)
        storage_key = str(storage.get("cheie_licenta", "")).strip() if isinstance(storage, dict) else ""

        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._attr_native_value = last_state.state
        elif storage_key:
            self._attr_native_value = storage_key

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value[: self._attr_native_max]
        self.async_write_ha_state()


class TextGrupareFacturi(RestoreEntity, TextEntity):
    _attr_icon = "mdi:shape-outline"
    _attr_native_min = 0
    _attr_native_max = 128
    _attr_mode = "text"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        self.coordonator = coordonator
        self.cont = cont
        self._entry = coordonator.intrare
        self._furnizor = coordonator.cheie_furnizor
        self._id_cont = str(cont.id_cont)

        alias = build_facturi_location_label(cont)
        slug = normalize_facturi_location_key(cont) or "locatie"
        provider_name = str(coordonator.intrare.title or coordonator.cheie_furnizor).strip()

        self.provider_name = provider_name
        self.location_alias = alias
        self.id_cont = self._id_cont

        self._attr_unique_id = f"{self._entry.entry_id}_{self._furnizor}_{self._id_cont}_grupare_facturi"
        self._attr_name = f"{provider_name} · Grupare facturi {alias}"
        self._attr_suggested_object_id = f"{self._furnizor}_{self._id_cont}_{slug}_grupare_facturi"
        self._attr_device_info = _grupare_facturi_device_info()
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_native_value = ""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        value = await async_obtine_grupare_factura(
            self.hass,
            self._entry.entry_id,
            self._furnizor,
            self._id_cont,
        )
        if value is not None:
            self._attr_native_value = value
            return

        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._attr_native_value = last_state.state

    async def async_set_value(self, value: str) -> None:
        clean_value = value[: self._attr_native_max].strip()
        self._attr_native_value = clean_value
        await async_seteaza_grupare_factura(
            self.hass,
            self._entry.entry_id,
            self._furnizor,
            self._id_cont,
            clean_value,
        )
        self.async_write_ha_state()

        # După schimbarea unei grupări, senzorul agregat trebuie recalculat
        # imediat. Dacă apelul rămâne neblocant, dashboardul poate afișa
        # temporar gruparea veche până la următorul refresh periodic.
        await self.hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": "sensor.administrare_integrare_facturi_utilitati"},
            blocking=True,
        )
