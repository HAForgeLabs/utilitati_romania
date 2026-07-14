from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

import re

from .const import DOMENIU
from .coordonator import CoordonatorUtilitatiRomania
from .entitate import EntitateUtilitatiRomania
from .retele_electrice_device import info_device_retele_electrice
from .naming import build_provider_slug


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordonator: CoordonatorUtilitatiRomania = hass.data[DOMENIU][entry.entry_id]
    entitati: list[BinarySensorEntity] = []

    if coordonator.data and coordonator.data.furnizor == "digi":
        entitati.append(DigiNecesitaReautentificareBinarySensor(coordonator))
        entitati.append(DigiAreRestanteBinarySensor(coordonator))

    if coordonator.data and coordonator.data.furnizor == "retele_electrice":
        for cont in coordonator.data.conturi:
            entitati.append(ReteleElectriceIntrerupereBinarySensor(coordonator, cont))

    if coordonator.data and coordonator.data.furnizor == "apa_nova_bucuresti":
        for cont in coordonator.data.conturi:
            entitati.append(ApaNovaFacturaRestantaBinarySensor(coordonator, cont))
            entitati.append(ApaNovaContorInteligentBinarySensor(coordonator, cont))

    async_add_entities(entitati)


class DigiNecesitaReautentificareBinarySensor(EntitateUtilitatiRomania, BinarySensorEntity):
    _attr_name = "Necesită reautentificare"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordonator: CoordonatorUtilitatiRomania) -> None:
        super().__init__(coordonator)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_digi_necesita_reautentificare"
        self._attr_suggested_object_id = "digi_necesita_reautentificare"

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data.extra or {}).get("needs_reauth")) if self.coordinator.data else False


class DigiAreRestanteBinarySensor(EntitateUtilitatiRomania, BinarySensorEntity):
    _attr_name = "Are restanțe Digi"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania) -> None:
        super().__init__(coordonator)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_digi_are_restante"
        self._attr_suggested_object_id = "digi_are_restante"

    @property
    def is_on(self) -> bool:
        if not self.coordinator.data:
            return False
        for consum in self.coordinator.data.consumuri:
            if consum.cheie == "factura_restanta" and consum.id_cont and str(consum.valoare).strip().lower() == "da":
                return True
        return False


class ReteleElectriceIntrerupereBinarySensor(EntitateUtilitatiRomania, BinarySensorEntity):
    _attr_name = "Intrerupere alimentare"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:transmission-tower-off"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_retele_electrice_{cont.id_cont}_intrerupere_alimentare"
        self._attr_suggested_object_id = f"retele_electrice_{cont.id_cont}_intrerupere_alimentare"
        self._attr_device_info = info_device_retele_electrice(coordonator.intrare.entry_id, cont)

    def _cont_curent(self):
        if self.coordinator.data is None:
            return self.cont
        return next(
            (item for item in self.coordinator.data.conturi if getattr(item, "id_cont", None) == self.cont.id_cont),
            self.cont,
        )

    @property
    def is_on(self) -> bool:
        raw = getattr(self._cont_curent(), "date_brute", None) or {}
        return raw.get("intrerupere_activa") is True

    @property
    def extra_state_attributes(self):
        cont = self._cont_curent()
        raw = getattr(cont, "date_brute", None) or {}
        return {
            "pod": getattr(cont, "id_cont", None),
            "adresa": getattr(cont, "adresa", None),
            "mesaj": raw.get("mesaj_intreruperi"),
            "status_alimentare": raw.get("status_alimentare"),
        }


def _valoare_consum_cont(coordonator: CoordonatorUtilitatiRomania, cont_id: str, cheie: str):
    if coordonator.data is None:
        return None
    for consum in coordonator.data.consumuri:
        if consum.id_cont == cont_id and consum.cheie == cheie:
            return consum.valoare
    return None


def _nume_loc_apa_nova(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    adresa = getattr(cont, "adresa", None) or getattr(cont, "nume", None) or "Loc consum"
    detaliu = raw.get("numar_instalatie") or raw.get("cod_client") or getattr(cont, "id_cont", None)
    text = f"{adresa} ({detaliu})" if detaliu and str(detaliu) not in str(adresa) else str(adresa)
    return re.sub(r"\s+", " ", text).strip()


def _slug_loc_apa_nova(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    baza = getattr(cont, "adresa", None) or getattr(cont, "nume", None) or raw.get("cod_client")
    return build_provider_slug("apa_nova_bucuresti", baza, getattr(cont, "id_cont", None))


def _info_device_apa_nova(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "apa_nova_bucuresti")
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_apa_nova_bucuresti_{ident}")},
        name=f"Apa Nova București - {_nume_loc_apa_nova(cont)}",
        manufacturer="Apa Nova București",
        model="Apă / canal",
    )


class _ApaNovaBinarySensorBase(EntitateUtilitatiRomania, BinarySensorEntity):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, key: str) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self._key = key
        slug = _slug_loc_apa_nova(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_apa_nova_bucuresti_{cont.id_cont}_{key}"
        self._attr_suggested_object_id = f"{slug}_{key}"
        self.entity_id = f"binary_sensor.{slug}_{key}"
        self._attr_device_info = _info_device_apa_nova(coordonator.intrare.entry_id, cont)

    @property
    def available(self) -> bool:
        if self.coordinator.data is None:
            return False
        return any(getattr(item, "id_cont", None) == self.cont.id_cont for item in self.coordinator.data.conturi)

    @property
    def is_on(self) -> bool:
        return _valoare_consum_cont(self.coordinator, self.cont.id_cont, self._key) is True


class ApaNovaFacturaRestantaBinarySensor(_ApaNovaBinarySensorBase):
    _attr_name = "Factură restantă"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-circle"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator, cont, "factura_restanta")


class ApaNovaContorInteligentBinarySensor(_ApaNovaBinarySensorBase):
    _attr_name = "Contor inteligent"
    _attr_icon = "mdi:water-sync"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator, cont, "contor_inteligent")
