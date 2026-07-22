from __future__ import annotations

from homeassistant.components.number import NumberEntity, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from .coordonator import CoordonatorUtilitatiRomania
from .const import (
    CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
    DOMENIU,
    IMPLICIT_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
    MAXIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
    MINIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
)
from .entitate import EntitateUtilitatiRomania
from .hidro_device import alias_loc_consum, info_device_hidro, slug_loc_consum
from .eon_device import alias_loc_eon, cheie_serviciu_eon, id_unic_eon, info_device_eon, slug_serviciu_loc_eon, tip_serviciu_eon
from .myelectrica_device import alias_loc_myelectrica, info_device_myelectrica, slug_loc_myelectrica
from .ebloc_device import alias_loc_ebloc, info_device_ebloc, slug_loc_ebloc
from .engie_device import alias_loc_engie, info_device_engie, slug_loc_engie
from .naming import build_provider_slug



def _registre_eon(cont) -> list[dict]:
    raw = getattr(cont, "date_brute", None) or {}
    meter_index = raw.get("meter_index") or {}
    devices = ((meter_index.get("indexDetails") or {}).get("devices") or [])
    return [registru for device in devices for registru in (device.get("indexes") or []) if isinstance(registru, dict) and registru.get("ablbelnr")]


def _rol_registru_eon(registru: dict) -> str:
    return "injectie" if str(registru.get("code") or "").upper() == "P" else "consum"

def _valoare_consum_curent(coordonator: CoordonatorUtilitatiRomania, id_cont: str, cheie: str) -> float | None:
    data = getattr(coordonator, 'data', None)
    consumuri = getattr(data, 'consumuri', None) or []
    for consum in consumuri:
        if getattr(consum, 'id_cont', None) != id_cont:
            continue
        if getattr(consum, 'cheie', None) != cheie:
            continue
        valoare = getattr(consum, 'valoare', None)
        try:
            return float(valoare) if valoare is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _citire_permisa_curenta(coordonator: CoordonatorUtilitatiRomania, id_cont: str) -> bool:
    data = getattr(coordonator, 'data', None)
    consumuri = getattr(data, 'consumuri', None) or []
    for consum in consumuri:
        if getattr(consum, 'id_cont', None) != id_cont:
            continue
        if getattr(consum, 'cheie', None) not in {'citire_permisa', 'citire_index_permisa'}:
            continue
        valoare = getattr(consum, 'valoare', None)
        if isinstance(valoare, str):
            return valoare.strip().lower() in {'da', 'true', '1', 'yes', 'on'}
        return bool(valoare)
    return False


def _fereastra_apa_canal(coordonator: CoordonatorUtilitatiRomania, id_cont: str) -> dict:
    data = getattr(coordonator, 'data', None)
    conturi = getattr(data, 'conturi', None) or []
    for cont in conturi:
        if getattr(cont, 'id_cont', None) != id_cont:
            continue
        raw = getattr(cont, 'date_brute', None) or {}
        return raw.get('meter_reading_window') or {}
    return {}


def _primul_registru_apa_canal(coordonator: CoordonatorUtilitatiRomania, id_cont: str) -> dict:
    registre = (_fereastra_apa_canal(coordonator, id_cont).get('registers') or [])
    return registre[0] if registre else {}




def _engie_index_ascuns(cont) -> bool:
    raw = getattr(cont, "date_brute", None) or {}
    index_data = raw.get("index") if isinstance(raw.get("index"), dict) else {}
    installations = index_data.get("installations") if isinstance(index_data.get("installations"), list) else []
    for item in installations:
        if isinstance(item, dict) and item.get("hide_index") is True:
            return True
    return bool(index_data.get("hide_index") is True)


def _engie_cauta_in_date_index(data, chei: tuple[str, ...]) -> str:
    """Cauta recursiv o valoare tehnica ENGIE in datele brute/index."""
    if isinstance(data, dict):
        for cheie in chei:
            valoare = data.get(cheie)
            if valoare not in (None, ""):
                return str(valoare).strip()
        for valoare in data.values():
            if isinstance(valoare, (dict, list)):
                gasit = _engie_cauta_in_date_index(valoare, chei)
                if gasit:
                    return gasit
    elif isinstance(data, list):
        for item in data:
            gasit = _engie_cauta_in_date_index(item, chei)
            if gasit:
                return gasit
    return ""


def _engie_date_tehnice_index(raw_or_cont, cont=None) -> tuple[str, str, str]:
    """Extrage datele necesare pentru transmiterea indexului ENGIE.

    In raspunsurile ENGIE, installation_number poate veni in datele principale ale
    locului de consum sau in raspunsul dedicat pentru index, de obicei in
    index.installations[0].installation_number. Din acest motiv cautarea este
    intentionat mai toleranta si recursiva.
    """
    if cont is None and not isinstance(raw_or_cont, dict):
        cont = raw_or_cont
        raw = getattr(cont, "date_brute", None) or {}
    else:
        raw = raw_or_cont if isinstance(raw_or_cont, dict) else {}

    poc = (
        str(raw.get("poc") or raw.get("poc_number") or raw.get("pocNumber") or "").strip()
        or _engie_cauta_in_date_index(raw, ("poc_number", "pocNumber", "poc"))
    )
    division = (
        str(raw.get("division") or raw.get("utility") or "").strip().lower()
        or _engie_cauta_in_date_index(raw, ("division", "utility", "type")).lower()
        or str(getattr(cont, "tip_serviciu", None) or getattr(cont, "tip_utilitate", None) or "gaz").strip().lower()
    )
    installation = (
        str(raw.get("installation_number") or raw.get("installationNumber") or "").strip()
        or _engie_cauta_in_date_index(
            raw,
            (
                "installation_number",
                "installationNumber",
                "installation",
                "installation_id",
                "installationId",
                "installationNo",
                "installation_no",
            ),
        )
    )
    return poc, division or "gaz", installation


def _engie_are_date_tehnice_index(raw_or_cont, cont=None) -> bool:
    poc, division, installation = _engie_date_tehnice_index(raw_or_cont, cont)
    return bool(poc and division and installation)
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordonator: CoordonatorUtilitatiRomania = hass.data[DOMENIU][entry.entry_id]
    entitati: list[NumberEntity] = []

    if coordonator.data:
        if coordonator.data.furnizor == "hidroelectrica":
            for cont in coordonator.data.conturi:
                entitati.append(NumarIndexHidro(coordonator, cont))

        elif coordonator.data.furnizor == "eon":
            for cont in coordonator.data.conturi:
                registre = _registre_eon(cont)
                if not registre:
                    entitati.append(NumarIndexEon(coordonator, cont))
                    continue
                for registru in registre:
                    entitati.append(NumarIndexEon(coordonator, cont, registru))

        elif coordonator.data.furnizor == "myelectrica":
            for cont in coordonator.data.conturi:
                raw = getattr(cont, "date_brute", None) or {}
                meter = raw.get("meter_list") or {}
                contoare = meter.get("to_Contor", []) or []
                are_contor = bool(
                    contoare
                    and (
                        contoare[0].get("SerieContor")
                        or ((contoare[0].get("to_Cadran") or [{}])[0].get("RegisterCode"))
                    )
                )
                if are_contor:
                    entitati.append(NumarIndexMyElectrica(coordonator, cont))

        elif coordonator.data.furnizor == "apa_canal":
            for cont in coordonator.data.conturi:
                entitati.append(NumarIndexApaCanal(coordonator, cont))

        elif coordonator.data.furnizor == "engie":
            for cont in coordonator.data.conturi:
                if _engie_are_date_tehnice_index(cont):
                    entitati.append(NumarIndexEngie(coordonator, cont))

        elif coordonator.data.furnizor == "ebloc":
            for cont in coordonator.data.conturi:
                entitati.append(NumarPersoaneEbloc(coordonator, cont))

        elif coordonator.data.furnizor == "retele_electrice":
            entitati.append(NumarIntervalActualizareContorRetele(coordonator))

    async_add_entities(entitati)


class NumarIntervalActualizareContorRetele(EntitateUtilitatiRomania, NumberEntity):
    _attr_name = "Interval actualizare automata contor"
    _attr_icon = "mdi:timer-sync-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = MINIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE
    _attr_native_max_value = MAXIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "h"
    _attr_mode = "box"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania) -> None:
        super().__init__(coordonator)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_retele_electrice_interval_actualizare_automata_contor"
        self._attr_suggested_object_id = "retele_electrice_interval_actualizare_automata_contor"

    @property
    def native_value(self) -> float:
        return float(getattr(
            self.coordinator,
            "interval_date_instantanee_ore",
            IMPLICIT_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
        ))

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_seteaza_interval_date_instantanee(value)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        return {
            "furnizor": "retele_electrice",
            "config_entry_id": self.coordinator.intrare.entry_id,
            "nume_intrare": self.coordinator.intrare.title,
            "dezactivat_la_zero": True,
        }


class NumarIndexHidro(EntitateUtilitatiRomania, RestoreNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 99999999
    _attr_native_step = 1
    _attr_icon = "mdi:counter"
    _attr_mode = "box"
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont

        alias = alias_loc_consum(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_consum(cont.id_cont, alias, cont.adresa)

        self._attr_unique_id = (
            f"{coordonator.intrare.entry_id}_hidro_{cont.id_cont}_index_energie_electrica"
        )
        self._attr_name = f"Index energie electrică {alias}"
        self._attr_device_info = info_device_hidro(coordonator.intrare.entry_id, cont)
        self._attr_native_value = 0
        self._attr_suggested_object_id = (
            f"hidro_{cont.id_cont}_{slug}_index_energie_electrica"
        )
        self.entity_id = f"number.hidro_{cont.id_cont}_{slug}_index_energie_electrica"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        ultima_stare = await self.async_get_last_number_data()
        if ultima_stare and ultima_stare.native_value is not None:
            self._attr_native_value = int(float(ultima_stare.native_value))
        else:
            valoare_curenta = _valoare_consum_curent(self.coordinator, self.cont.id_cont, 'index_energie_electrica')
            if valoare_curenta is not None:
                self._attr_native_value = valoare_curenta

    @property
    def available(self) -> bool:
        return _citire_permisa_curenta(self.coordinator, self.cont.id_cont)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = int(float(value))
        self.async_write_ha_state()


class NumarIndexEon(EntitateUtilitatiRomania, RestoreNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 99999999
    _attr_native_step = 1
    _attr_icon = "mdi:counter"
    _attr_mode = "box"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, registru: dict | None = None) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.registru = registru or {}
        self.rol_registru = _rol_registru_eon(self.registru)

        alias = alias_loc_eon(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_serviciu_loc_eon(cont)
        identificator = id_unic_eon(cont)
        tip = tip_serviciu_eon(cont)
        este_injectie = self.rol_registru == "injectie"
        sufix = "_injectie" if este_injectie else ""
        eticheta = "energie livrată" if este_injectie else ("gaz" if tip == "gaz" else "energie electrică")

        self._attr_native_unit_of_measurement = "m³" if tip == "gaz" else "kWh"
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_eon_{identificator}_index{sufix}"
        self._attr_name = f"Index {eticheta} {alias}"
        self._attr_icon = "mdi:transmission-tower-export" if este_injectie else "mdi:counter"
        self._attr_device_info = info_device_eon(coordonator.intrare.entry_id, cont)
        self._attr_suggested_object_id = f"{slug}_index{sufix}"
        self.entity_id = f"number.{slug}_index{sufix}"
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        valoare_registru = None
        for cheie in ("currentValue", "oldSelfIndexValue", "oldValue"):
            if self.registru.get(cheie) is not None:
                valoare_registru = int(float(self.registru[cheie]))
                break

        ultima_stare = await self.async_get_last_number_data()
        if ultima_stare and ultima_stare.native_value is not None:
            valoare_restaurata = int(float(ultima_stare.native_value))
            self._attr_native_value = valoare_registru if valoare_restaurata <= 0 and valoare_registru is not None else valoare_restaurata
        elif valoare_registru is not None:
            self._attr_native_value = valoare_registru

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = int(float(value))
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "furnizor": "eon",
            "id_cont": self.cont.id_cont,
            "rol_registru": self.rol_registru,
            "cod_registru": str(self.registru.get("code") or ""),
            "ablbelnr": str(self.registru.get("ablbelnr") or ""),
        }


class NumarIndexMyElectrica(EntitateUtilitatiRomania, RestoreNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 99999999
    _attr_native_step = 1
    _attr_icon = "mdi:counter"
    _attr_mode = "box"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont

        alias = alias_loc_myelectrica(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_myelectrica(cont.id_cont, alias, cont.adresa)
        tip = str(cont.tip_serviciu or cont.tip_utilitate or "").lower()

        self._attr_native_unit_of_measurement = "m³" if tip == "gaz" else "kWh"
        self._attr_unique_id = (
            f"{coordonator.intrare.entry_id}_myelectrica_{cont.id_cont}_index_contor"
        )
        self._attr_name = f"Index contor {alias}"
        self._attr_device_info = info_device_myelectrica(coordonator.intrare.entry_id, cont)
        self._attr_suggested_object_id = f"utilitati_romania_myelectrica_{slug}_index_contor"
        self.entity_id = f"number.utilitati_romania_myelectrica_{slug}_index_contor"
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        ultima_stare = await self.async_get_last_number_data()
        if ultima_stare and ultima_stare.native_value is not None:
            self._attr_native_value = int(float(ultima_stare.native_value))

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = int(float(value))
        self.async_write_ha_state()


class NumarIndexApaCanal(EntitateUtilitatiRomania, RestoreNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 99999999
    _attr_native_step = 1
    _attr_icon = "mdi:counter"
    _attr_mode = "box"
    _attr_native_unit_of_measurement = "m³"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        alias = str(cont.nume or cont.adresa or cont.id_cont or "contract").strip()
        eticheta = str(
            coordonator.intrare.data.get("premise_label")
            or coordonator.intrare.title
            or alias
        ).strip()
        slug = build_provider_slug("apa_canal_sibiu", eticheta, eticheta)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_apa_canal_{cont.id_cont}_index_de_transmis"
        self._attr_name = f"Index de transmis {alias}"
        self._attr_suggested_object_id = f"{slug}_index_de_transmis"
        self.entity_id = f"number.{slug}_index_de_transmis"
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        ultima_stare = await self.async_get_last_number_data()
        if ultima_stare and ultima_stare.native_value is not None:
            self._attr_native_value = int(float(ultima_stare.native_value))
            return

        registru = _primul_registru_apa_canal(self.coordinator, self.cont.id_cont)
        valoare_anterioara = _valoare_consum_curent(self.coordinator, self.cont.id_cont, 'index_de_transmis')
        valoare = valoare_anterioara
        if valoare is None:
            try:
                valoare = float(registru.get('previous_reading'))
            except (TypeError, ValueError):
                valoare = None
        if valoare is not None:
            self._attr_native_value = int(float(valoare))


    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "furnizor": "apa_canal",
            "id_cont": getattr(self.cont, "id_cont", None),
            "id_contract": getattr(self.cont, "id_contract", None),
        }

    @property
    def available(self) -> bool:
        registru = _primul_registru_apa_canal(self.coordinator, self.cont.id_cont)
        return _citire_permisa_curenta(self.coordinator, self.cont.id_cont) and bool(registru.get('device_id') and registru.get('register_id'))

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = int(float(value))
        self.async_write_ha_state()


class NumarIndexEngie(EntitateUtilitatiRomania, RestoreNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 99999999
    _attr_native_step = 1
    _attr_icon = "mdi:counter"
    _attr_mode = "box"

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        alias = alias_loc_engie(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_engie(cont.id_cont, alias, cont.adresa)
        tip = str(cont.tip_serviciu or cont.tip_utilitate or "").lower()
        self._attr_native_unit_of_measurement = "m³" if tip == "gaz" else "kWh"
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_engie_{cont.id_cont}_index_de_transmis"
        self._attr_name = f"Index de transmis {alias}"
        self._attr_device_info = info_device_engie(coordonator.intrare.entry_id, cont)
        self._attr_suggested_object_id = f"engie_{cont.id_cont}_{slug}_index_de_transmis"
        self.entity_id = f"number.engie_{cont.id_cont}_{slug}_index_de_transmis"
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        ultima_stare = await self.async_get_last_number_data()
        if ultima_stare and ultima_stare.native_value is not None:
            self._attr_native_value = int(float(ultima_stare.native_value))
            return

        valoare_curenta = _valoare_consum_curent(self.coordinator, self.cont.id_cont, "index_contor")
        if valoare_curenta is not None:
            self._attr_native_value = int(float(valoare_curenta))

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        poc, division, installation = _engie_date_tehnice_index(self.cont)
        return {
            "furnizor": "engie",
            "id_cont": getattr(self.cont, "id_cont", None),
            "id_contract": getattr(self.cont, "id_contract", None),
            "poc": poc,
            "division": division,
            "installation_number": installation,
        }

    @property
    def available(self) -> bool:
        return (
            _citire_permisa_curenta(self.coordinator, self.cont.id_cont)
            and not _engie_index_ascuns(self.cont)
            and _engie_are_date_tehnice_index(self.cont)
        )

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = int(float(value))
        self.async_write_ha_state()


class NumarPersoaneEbloc(EntitateUtilitatiRomania, RestoreNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 1
    _attr_icon = "mdi:account-group"
    _attr_mode = "box"
    _attr_native_unit_of_measurement = "pers."

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        alias = alias_loc_ebloc(cont.nume, cont.adresa, cont.id_cont, cont=cont)
        slug = slug_loc_ebloc(cont.id_cont, alias, cont.adresa, cont=cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_ebloc_{cont.id_cont}_numar_persoane_setare"
        self._attr_name = f"Număr persoane - {alias}"
        self._attr_device_info = info_device_ebloc(coordonator.intrare.entry_id, cont)
        self._attr_suggested_object_id = f"{slug}_numar_persoane_setare"
        self.entity_id = f"number.{slug}_numar_persoane_setare"
        self._attr_native_value = 0

    @property
    def native_value(self):
        if self._attr_native_value is None:
            return None
        return int(float(self._attr_native_value))

    @property
    def available(self) -> bool:
        permis = _valoare_consum_curent_text(self.coordinator, self.cont.id_cont, "editare_persoane_permisa")
        return permis == "da"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        valoare_curenta = _valoare_consum_curent(self.coordinator, self.cont.id_cont, "numar_persoane")
        if valoare_curenta is not None:
            self._attr_native_value = valoare_curenta
            return
        ultima_stare = await self.async_get_last_number_data()
        if ultima_stare and ultima_stare.native_value is not None:
            self._attr_native_value = int(float(ultima_stare.native_value))

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = int(value)
        self.async_write_ha_state()


def _valoare_consum_curent_text(coordonator: CoordonatorUtilitatiRomania, id_cont: str, cheie: str) -> str | None:
    data = getattr(coordonator, "data", None)
    consumuri = getattr(data, "consumuri", None) or []
    for consum in consumuri:
        if getattr(consum, "id_cont", None) != id_cont:
            continue
        if getattr(consum, "cheie", None) != cheie:
            continue
        valoare = getattr(consum, "valoare", None)
        return str(valoare) if valoare is not None else None
    return None
