from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
import re

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.const import UnitOfVolume

from .coordonator import CoordonatorUtilitatiRomania
from .entitate import EntitateUtilitatiRomania
from .const import DOMENIU, CONF_FURNIZOR, FURNIZOR_ADMIN_GLOBAL
from .modele import FacturaUtilitate, InstantaneuFurnizor
from .hidro_device import alias_loc_consum, info_device_hidro, slug_loc_consum
from .eon_device import (
    alias_loc_eon,
    cheie_serviciu_eon,
    id_unic_eon,
    info_device_eon,
    slug_serviciu_loc_eon,
    tip_serviciu_eon,
)
from .myelectrica_device import alias_loc_myelectrica, info_device_myelectrica, slug_loc_myelectrica
from .deer_device import alias_loc_deer, info_device_deer, slug_loc_deer
from .ebloc_device import alias_loc_ebloc, info_device_ebloc, slug_loc_ebloc
from .orange_device import alias_loc_orange, info_device_orange, slug_loc_orange
from .naming import build_provider_slug, extract_street_slug
from .licentiere import async_obtine_licenta_globala, mascheaza_cheia_licenta
from .facturi_agregate import colecteaza_facturi_agregate, sumar_facturi
from .storage_citiri import async_incarca_cache_citiri, obtine_citire_cache

def _cont_curent_dupa_id(coordonator: CoordonatorUtilitatiRomania, id_cont: str | None):
    data = getattr(coordonator, "data", None)
    conturi = getattr(data, "conturi", None) or []
    for cont in conturi:
        if getattr(cont, "id_cont", None) == id_cont:
            return cont
    return None


class SenzorAdminBaza(SensorEntity):
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str, name: str) -> None:
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_admin_{key}"

        object_map = {
            "status": "status_licenta",
            "plan": "plan_licenta",
            "expires_at": "valabila_pana_la",
            "checked_at": "ultima_verificare_licenta",
            "utilizator": "cont_licenta",
            "masked_key": "cod_licenta_mascat",
            "message": "mesaj_licenta",
            "contact": "contact_dezvoltator",
            "support": "suport",
            "facturi_agregate": "facturi_utilitati",
        }
        object_id = object_map.get(key, key)

        self._attr_name = name
        self._attr_suggested_object_id = f"{DOMENIU}_{object_id}"

        if key == "facturi_agregate":
            self.entity_id = "sensor.administrare_integrare_facturi_utilitati"
        else:
            self.entity_id = f"sensor.{DOMENIU}_{object_id}"

        self._attr_device_info = _admin_device_info(entry)
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:shield-key-outline"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await self._async_refresh_value()

    async def async_update(self) -> None:
        await self._async_refresh_value()

    async def _async_refresh_value(self) -> None:
        raise NotImplementedError


class SenzorAdminLicenta(SenzorAdminBaza):
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_interval = async_track_time_interval(self.hass, self._async_handle_interval, timedelta(minutes=1))

    async def async_will_remove_from_hass(self) -> None:
        if getattr(self, "_unsub_interval", None) is not None:
            self._unsub_interval()
            self._unsub_interval = None

    async def _async_handle_interval(self, _now) -> None:
        await self._async_refresh_value()
        self.async_write_ha_state()

    async def _async_refresh_value(self) -> None:
        storage = await async_obtine_licenta_globala(self.hass)
        info = storage.get("date_verificare_licenta") if isinstance(storage, dict) else {}
        info = info if isinstance(info, dict) else {}

        if self._key == "utilizator":
            self._attr_native_value = str(storage.get("utilizator", "")).strip() or "-"
            return

        if self._key == "masked_key":
            self._attr_native_value = mascheaza_cheia_licenta(str(storage.get("cheie_licenta", "")).strip()) or "-"
            return

        if self._key == "message":
            value = info.get("message")
            self._attr_native_value = str(value).strip() if value not in (None, "") else "-"
            return

        value = info.get(self._key)
        self._attr_native_value = str(value).strip() if value not in (None, "") else "-"


class SenzorAdminStatic(SenzorAdminBaza):
    def __init__(self, entry: ConfigEntry, key: str, name: str, value: str) -> None:
        super().__init__(entry, key, name)
        self._value = value
        self._attr_icon = "mdi:information-outline"

    async def _async_refresh_value(self) -> None:
        self._attr_native_value = self._value


class SenzorAdminFacturiAgregate(SenzorAdminBaza):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(entry, "facturi_agregate", "Facturi utilități")
        self.hass = hass
        self._attr_icon = "mdi:file-document-multiple-outline"
        self._attr_entity_category = None
        self._sumar: dict[str, Any] = {}
        self._unsub_interval = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_interval = async_track_time_interval(self.hass, self._async_handle_interval, timedelta(minutes=1))

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    async def _async_handle_interval(self, _now) -> None:
        await self._async_refresh_value()
        self.async_write_ha_state()

    async def _async_refresh_value(self) -> None:
        try:
            facturi = colecteaza_facturi_agregate(self.hass)
            self._sumar = sumar_facturi(facturi)
            self._attr_native_value = self._sumar.get("numar_facturi", 0)
            self._ultima_eroare = None
            self._attr_available = True
        except Exception as err:
            if not hasattr(self, "_sumar") or not isinstance(self._sumar, dict):
                self._sumar = {}
            self._attr_native_value = self._sumar.get("numar_facturi", 0)
            self._ultima_eroare = str(err)
            self._attr_available = True

    @property
    def available(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "numar_facturi": self._sumar.get("numar_facturi", 0),
            "numar_platite": self._sumar.get("numar_platite", 0),
            "numar_neplatite": self._sumar.get("numar_neplatite", 0),
            "numar_necunoscute": self._sumar.get("numar_necunoscute", 0),
            "numar_status_necunoscut": self._sumar.get("numar_status_necunoscut", 0),
            "total_neplatit": self._sumar.get("total_neplatit", 0),
            "total_neplatit_formatat": self._sumar.get("total_neplatit_formatat", "0.00 RON"),
            "moneda": self._sumar.get("moneda", "RON"),
            "locatii": self._sumar.get("locatii", []),
            "ultima_eroare": getattr(self, "_ultima_eroare", None),
        }



@dataclass(frozen=True, kw_only=True)
class DescriereSenzorRezumat(SensorEntityDescription):
    functie_valoare: Any


@dataclass(frozen=True, kw_only=True)
class DescriereSenzorCont(SensorEntityDescription):
    functie_valoare: Any


@dataclass(frozen=True, kw_only=True)
class DescriereSenzorContEonExtins(SensorEntityDescription):
    functie_valoare: Any


def _valori_consum(instantaneu: InstantaneuFurnizor, cheie: str, id_cont: str | None = None):
    valori = []
    for c in instantaneu.consumuri:
        if c.cheie != cheie:
            continue
        if id_cont is not None and c.id_cont != id_cont:
            continue
        valori.append(c.valoare)
    return valori


def _consum_dupa_cheie(instantaneu: InstantaneuFurnizor, cheie: str, id_cont: str | None = None):
    for consum in instantaneu.consumuri:
        if consum.cheie != cheie:
            continue
        if id_cont is not None and getattr(consum, "id_cont", None) != id_cont:
            continue
        return consum
    return None


def _valoare_consum(instantaneu: InstantaneuFurnizor, cheie: str, id_cont: str | None = None):
    valori = _valori_consum(instantaneu, cheie, id_cont)
    if not valori:
        return None
    if id_cont is not None:
        return valori[0]
    valori_num = []
    for v in valori:
        try:
            valori_num.append(float(v))
        except (TypeError, ValueError):
            pass
    if len(valori) > 1 and len(valori_num) == len(valori):
        return round(sum(valori_num), 2)
    return valori[0]


def _valoare_consum_global(instantaneu: InstantaneuFurnizor, cheie: str):
    for c in instantaneu.consumuri:
        if c.cheie != cheie:
            continue
        if getattr(c, "id_cont", None) not in (None, ""):
            continue
        return c.valoare
    return None


def _suma_consumuri_pe_cont(instantaneu: InstantaneuFurnizor, cheie: str) -> float | None:
    total = 0.0
    gasit = False
    for consum in instantaneu.consumuri or []:
        if getattr(consum, "cheie", None) != cheie:
            continue
        if getattr(consum, "id_cont", None) in (None, ""):
            continue
        try:
            total += float(getattr(consum, "valoare", 0) or 0)
            gasit = True
        except (TypeError, ValueError):
            continue
    return round(total, 2) if gasit else None


def _valoare_rezumat_financiar(instantaneu: InstantaneuFurnizor, cheie: str):
    valoare_globala = _valoare_consum_global(instantaneu, cheie)
    if valoare_globala is not None:
        return valoare_globala
    return _suma_consumuri_pe_cont(instantaneu, cheie)




def _cheie_serviciu_eon_din_valori(tip_serviciu: Any = None, tip_utilitate: Any = None) -> str:
    text = f"{tip_serviciu or ''} {tip_utilitate or ''}".strip().lower()
    if "gaz" in text or "gas" in text or "02" in text:
        return "gaz"
    if "curent" in text or "electric" in text or "energie" in text or "01" in text:
        return "energie_electrica"
    return "serviciu"


def _valoare_consum_eon(instantaneu: InstantaneuFurnizor, cheie: str, cont):
    id_cont = getattr(cont, "id_cont", None)
    serviciu_cont = cheie_serviciu_eon(cont)
    valori = []
    for consum in instantaneu.consumuri or []:
        if getattr(consum, "cheie", None) != cheie:
            continue
        if getattr(consum, "id_cont", None) != id_cont:
            continue
        serviciu_consum = _cheie_serviciu_eon_din_valori(
            getattr(consum, "tip_serviciu", None),
            getattr(consum, "tip_utilitate", None),
        )
        if serviciu_consum == serviciu_cont:
            valori.append(getattr(consum, "valoare", None))

    if valori:
        return valori[0]
    return _valoare_consum(instantaneu, cheie, id_cont)

def _id_ultima_factura_rezumat(instantaneu: InstantaneuFurnizor):
    if instantaneu.furnizor == "digi":
        return _valoare_consum_global(instantaneu, "id_ultima_factura")
    return _id_ultima_factura(instantaneu)


def _valoare_ultima_factura_rezumat(instantaneu: InstantaneuFurnizor):
    if instantaneu.furnizor == "digi":
        return _valoare_consum_global(instantaneu, "valoare_ultima_factura")
    return _valoare_ultima_factura(instantaneu)


def _ultima_factura(
    instantaneu: InstantaneuFurnizor,
    categorie: str | None = None,
    id_cont: str | None = None,
) -> FacturaUtilitate | None:
    facturi = instantaneu.facturi
    if categorie is not None:
        facturi = [f for f in facturi if f.categorie == categorie]
    if id_cont is not None:
        facturi = [f for f in facturi if f.id_cont == id_cont]
    if not facturi:
        return None
    return sorted(facturi, key=lambda f: f.data_emitere or date.min, reverse=True)[0]


def _valoare_ultima_factura(
    instantaneu: InstantaneuFurnizor,
    id_cont: str | None = None,
    categorie: str | None = None,
):
    if id_cont is not None:
        v = _valoare_consum(instantaneu, "valoare_ultima_factura", id_cont)
        if v not in (None, ""):
            return v
    factura = _ultima_factura(instantaneu, categorie, id_cont)
    return factura.valoare if factura else None


def _id_ultima_factura(
    instantaneu: InstantaneuFurnizor,
    id_cont: str | None = None,
    categorie: str | None = None,
):
    if id_cont is not None:
        v = _valoare_consum(instantaneu, "id_ultima_factura", id_cont)
        if v not in (None, "", "unknown", "Unknown"):
            return v
    factura = _ultima_factura(instantaneu, categorie, id_cont)
    return factura.id_factura if factura else None


def _valoare_adevarata(valoare: Any) -> bool:
    if isinstance(valoare, str):
        return valoare.strip().lower() in {"da", "true", "1", "yes", "on"}
    return bool(valoare)


def _cont_este_prosumator(cont) -> bool:
    if _valoare_adevarata(getattr(cont, "este_prosumator", False)):
        return True
    date_brute = getattr(cont, "date_brute", None) or {}
    if isinstance(date_brute, dict):
        return _valoare_adevarata(date_brute.get("este_prosumator"))
    return False


def _este_prosumator(instantaneu: InstantaneuFurnizor) -> bool:
    if any(_cont_este_prosumator(cont) for cont in (instantaneu.conturi or [])):
        return True

    for consum in instantaneu.consumuri or []:
        if getattr(consum, "cheie", None) == "este_prosumator" and _valoare_adevarata(getattr(consum, "valoare", None)):
            return True

    extra = getattr(instantaneu, "extra", None) or {}
    if isinstance(extra, dict) and _valoare_adevarata(extra.get("este_prosumator")):
        return True

    return False


def _calculeaza_total_neachitat(instantaneu: InstantaneuFurnizor):
    if instantaneu.furnizor == "digi":
        val = _valoare_consum_global(instantaneu, "total_neachitat")
        if val is None:
            val = _valoare_consum_global(instantaneu, "sold_curent")
        try:
            return round(max(float(val or 0), 0.0), 2)
        except Exception:
            return 0.0

    sold_curent = _valoare_rezumat_financiar(instantaneu, "sold_curent")
    if sold_curent is not None:
        try:
            return round(max(float(sold_curent), 0.0), 2)
        except Exception:
            return None
    return None


def _calculeaza_de_plata(instantaneu: InstantaneuFurnizor):
    if instantaneu.furnizor == "digi":
        val = _valoare_consum_global(instantaneu, "de_plata")
        try:
            return round(max(float(val or 0), 0.0), 2)
        except Exception:
            return 0.0
    return _calculeaza_total_neachitat(instantaneu)


def _scadenta_urmatoare(instantaneu: InstantaneuFurnizor):
    if instantaneu.furnizor == "digi":
        return _valoare_consum_global(instantaneu, "urmatoarea_scadenta")

    dates = []
    for f in instantaneu.facturi:
        if f.data_scadenta:
            dates.append(f.data_scadenta)
    return min(dates).isoformat() if dates else None


def _date_brute_cont(cont) -> dict[str, Any]:
    raw = getattr(cont, "date_brute", None)
    return raw if isinstance(raw, dict) else {}


def _slug_strada_digi(cont) -> str:
    return extract_street_slug(getattr(cont, "adresa", None), getattr(cont, "id_cont", None))

    replacements = {
        "ă": "a",
        "â": "a",
        "î": "i",
        "ș": "s",
        "ş": "s",
        "ț": "t",
        "ţ": "t",
    }
    for src, dst in replacements.items():
        adresa = adresa.replace(src, dst)

    # cautăm explicit tipul de stradă + numele
    match = re.search(
        r"(?:strada|str\.?|aleea|alee\.?|bd\.?|bulevardul|bulevard|calea)\s+([a-z0-9\-]+)",
        adresa,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # fallback sigur: prima valoare "curată"
    tokenuri = re.split(r"[^a-z0-9]+", adresa)
    blacklist = {
        "strada", "str", "aleea", "alee", "bd", "bulevard", "bulevardul",
        "calea", "nr", "bl", "sc", "et", "ap", "judetul", "sibiu", "selimbar"
    }

    for t in tokenuri:
        if t and t not in blacklist and not t.isdigit():
            return t

    return "cont"


def _tip_eon(cont) -> str:
    tip = tip_serviciu_eon(cont)
    return "gaz" if tip == "gaz" else "curent"


def _an_curent_loc_eon(cont) -> int:
    raw = _date_brute_cont(cont)
    ani = []
    for item in raw.get("istoric_index", []) or []:
        try:
            ani.append(int(item.get("an")))
        except Exception:
            pass
    for item in raw.get("istoric_plati", []) or []:
        data = item.get("data")
        if isinstance(data, str) and len(data) >= 4 and data[:4].isdigit():
            ani.append(int(data[:4]))
    return max(ani) if ani else datetime.now().year


def _eon_arhiva_index_count(cont):
    raw = _date_brute_cont(cont)
    an = _an_curent_loc_eon(cont)
    total = 0
    for item in raw.get("istoric_index", []) or []:
        try:
            if int(item.get("an")) == an:
                total += 1
        except Exception:
            continue
    return total


def _eon_arhiva_plati_count(cont):
    raw = _date_brute_cont(cont)
    an = _an_curent_loc_eon(cont)
    total = 0
    for item in raw.get("istoric_plati", []) or []:
        data = item.get("data")
        if isinstance(data, str) and data[:4].isdigit() and int(data[:4]) == an:
            total += 1
    return total


def _eon_arhiva_consum_total(cont):
    raw = _date_brute_cont(cont)
    val = raw.get("consum_total")
    try:
        return round(float(val), 2)
    except Exception:
        return 0.0


def _eon_conventie_consum(cont):
    raw = _date_brute_cont(cont)
    conventie = raw.get("conventie_consum") or {}
    if not isinstance(conventie, dict):
        return "nu"
    for val in conventie.values():
        try:
            if float(val) > 0:
                return "da"
        except Exception:
            continue
    return "nu"


def _eon_date_contract(cont):
    raw = _date_brute_cont(cont)
    contract = raw.get("date_contract") or {}
    if isinstance(contract, dict):
        return contract.get("accountContract") or getattr(cont, "id_contract", None) or getattr(cont, "id_cont", None)
    return getattr(cont, "id_contract", None) or getattr(cont, "id_cont", None)

from datetime import datetime

def _eon_id_ultima_factura(cont):
    raw = _date_brute_cont(cont)

    val = raw.get("id_ultima_factura")
    if val not in (None, "", "unknown", "Unknown"):
        return val

    plati = raw.get("istoric_plati") or []
    if plati:
        ultima = sorted(plati, key=lambda x: x.get("data", ""), reverse=True)[0]
        data = ultima.get("data")
        if data:
            try:
                dt = datetime.fromisoformat(data)
                luni = [
                    "ianuarie", "februarie", "martie", "aprilie", "mai", "iunie",
                    "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie"
                ]
                return f"Plată {luni[dt.month - 1]} {dt.year}"
            except Exception:
                return f"Plată {data[:7]}"

    return None


from datetime import datetime

def _eon_urmatoarea_scadenta(cont):
    raw = _date_brute_cont(cont)

    def _format(val):
        try:
            return datetime.fromisoformat(val).strftime("%d.%m.%Y")
        except Exception:
            return val

    val = raw.get("urmatoarea_scadenta")
    if val not in (None, "", "unknown", "Unknown"):
        return _format(val)

    plati = raw.get("istoric_plati") or []
    if plati:
        ultima = sorted(plati, key=lambda x: x.get("data", ""), reverse=True)[0]
        data = ultima.get("data")
        if data:
            return _format(data)

    return None


def _eon_valoare_ultima_factura(cont):
    raw = _date_brute_cont(cont)

    try:
        sold = float(raw.get("sold_factura") or 0)
    except Exception:
        sold = 0.0

    if sold > 0:
        return round(sold, 2)

    try:
        ultima_plata = float(raw.get("ultima_plata_valoare") or 0)
    except Exception:
        ultima_plata = 0.0

    if ultima_plata > 0:
        return round(ultima_plata, 2)

    try:
        valoare = float(raw.get("valoare_ultima_factura") or 0)
    except Exception:
        valoare = 0.0

    return round(valoare, 2)

SENZORI_REZUMAT: tuple[DescriereSenzorRezumat, ...] = (
    DescriereSenzorRezumat(key="numar_conturi", name="Număr conturi", icon="mdi:folder-account", functie_valoare=lambda i: len(i.conturi)),
    DescriereSenzorRezumat(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple", functie_valoare=lambda i: len(i.facturi)),
    DescriereSenzorRezumat(key="tipuri_servicii", name="Tipuri servicii", icon="mdi:shape-outline", functie_valoare=lambda i: ", ".join(sorted({str(c.tip_serviciu) for c in i.conturi if c.tip_serviciu})) or None),
    DescriereSenzorRezumat(key="numar_conturi_curent", name="Număr conturi curent", icon="mdi:lightning-bolt", functie_valoare=lambda i: sum(1 for c in i.conturi if c.tip_serviciu == "curent")),
    DescriereSenzorRezumat(key="numar_conturi_gaz", name="Număr conturi gaz", icon="mdi:fire-circle", functie_valoare=lambda i: sum(1 for c in i.conturi if c.tip_serviciu == "gaz")),
    DescriereSenzorRezumat(key="este_prosumator", name="Este prosumator", icon="mdi:solar-power-variant", functie_valoare=lambda i: "da" if _este_prosumator(i) else "nu"),
)

SENZORI_REZUMAT_FINANCIAR: tuple[DescriereSenzorRezumat, ...] = (
    DescriereSenzorRezumat(
        key="de_plata",
        name="De plată",
        icon="mdi:cash-clock",
        native_unit_of_measurement="RON",
        functie_valoare=_calculeaza_de_plata,
    ),
    DescriereSenzorRezumat(
        key="total_neachitat",
        name="Total neachitat",
        icon="mdi:cash-remove",
        native_unit_of_measurement="RON",
        functie_valoare=_calculeaza_total_neachitat,
    ),
    DescriereSenzorRezumat(
        key="sold_curent",
        name="Sold curent",
        icon="mdi:cash",
        native_unit_of_measurement="RON",
        functie_valoare=lambda i: _valoare_rezumat_financiar(i, "sold_curent"),
    ),
    DescriereSenzorRezumat(
        key="urmatoarea_scadenta",
        name="Următoarea scadență",
        icon="mdi:calendar-clock",
        functie_valoare=_scadenta_urmatoare,
    ),
    DescriereSenzorRezumat(
        key="sold_prosumator",
        name="Sold prosumator",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement="RON",
        functie_valoare=lambda i: _valoare_consum(i, "sold_prosumator") if _este_prosumator(i) else None,
    ),
    DescriereSenzorRezumat(
        key="valoare_ultima_factura",
        name="Valoare ultima factură",
        icon="mdi:cash",
        native_unit_of_measurement="RON",
        functie_valoare=_valoare_ultima_factura_rezumat,
    ),
    DescriereSenzorRezumat(
        key="id_ultima_factura",
        name="ID ultima factură",
        icon="mdi:receipt-text",
        functie_valoare=_id_ultima_factura_rezumat,
    ),
)

SENZORI_CONT_HIDRO: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="consum_lunar_curent", name="Consum lunar curent", icon="mdi:lightning-bolt", native_unit_of_measurement="kWh", functie_valoare=lambda i, c: _valoare_consum(i, "consum_lunar_curent", c.id_cont)),
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(max(float(_valoare_consum(i, "sold_curent", c.id_cont) or 0), 0.0), 2)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_curent", c.id_cont)),
    DescriereSenzorCont(key="id_ultima_factura", name="ID ultima factură", icon="mdi:receipt-text", functie_valoare=lambda i, c: _id_ultima_factura(i, c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_ultima_factura(i, c.id_cont)),
    DescriereSenzorCont(key="citire_permisa", name="Citire permisă", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum(i, "citire_permisa", c.id_cont)),
    DescriereSenzorCont(key="index_energie_electrica", name="Index energie electrică", icon="mdi:meter-electric", native_unit_of_measurement="kWh", functie_valoare=lambda i, c: _valoare_consum(i, "index_energie_electrica", c.id_cont)),
    DescriereSenzorCont(key="index_energie_produsa", name="Index energie produsă", icon="mdi:transmission-tower-export", native_unit_of_measurement="kWh", functie_valoare=lambda i, c: _valoare_consum(i, "index_energie_produsa", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="sold_factura", name="Sold factură", icon="mdi:cash-refund", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_factura", c.id_cont)),
)

SENZORI_CONT_EON: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="citire_permisa", name="Citire permisă", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum_eon(i, "citire_permisa", c)),
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum_eon(i, "de_plata", c)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum_eon(i, "factura_restanta", c)),
    DescriereSenzorCont(key="id_ultima_factura", name="ID ultima factură", icon="mdi:receipt-text", functie_valoare=lambda i, c: _eon_id_ultima_factura(c)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum_eon(i, "sold_curent", c)),
    DescriereSenzorCont(key="sold_factura", name="Sold factură", icon="mdi:cash-refund", functie_valoare=lambda i, c: "da" if float(_valoare_consum_eon(i, "sold_factura", c) or 0) > 0 else "nu"),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _eon_valoare_ultima_factura(c)),
    DescriereSenzorCont(key="index_contor", name="Index contor", icon="mdi:meter-gas", functie_valoare=lambda i, c: _valoare_consum_eon(i, "index_gaz", c) if _tip_eon(c) == "gaz" else _valoare_consum_eon(i, "index_energie_electrica", c)),
)

SENZORI_CONT_EON_EXTINS: tuple[DescriereSenzorContEonExtins, ...] = (
    DescriereSenzorContEonExtins(key="date_contract", name="Date contract", icon="mdi:file-document-edit-outline", functie_valoare=_eon_date_contract),
    DescriereSenzorContEonExtins(key="conventie_consum", name="Convenție consum", icon="mdi:chart-bar", functie_valoare=_eon_conventie_consum),
    DescriereSenzorContEonExtins(key="arhiva_consum", name="Arhivă consum", icon="mdi:clipboard-text-clock", functie_valoare=_eon_arhiva_consum_total),
    DescriereSenzorContEonExtins(key="arhiva_index", name="Arhivă index", icon="mdi:clipboard-text-clock", functie_valoare=_eon_arhiva_index_count),
    DescriereSenzorContEonExtins(key="arhiva_plati", name="Arhivă plăți", icon="mdi:cash-multiple", functie_valoare=_eon_arhiva_plati_count),
)


@dataclass(frozen=True, kw_only=True)
class DescriereSenzorApaCanal(SensorEntityDescription):
    key_path: tuple[str, ...]


SENZORI_APA_CANAL: tuple[DescriereSenzorApaCanal, ...] = (
    DescriereSenzorApaCanal(
        key="citire_index_permisa",
        name="Citire index permisă",
        native_unit_of_measurement=None,
        icon="mdi:counter",
        key_path=("meter_reading_window", "is_open"),
    ),
    DescriereSenzorApaCanal(
        key="perioada_citire",
        name="Perioadă citire index",
        native_unit_of_measurement=None,
        icon="mdi:calendar-clock",
        key_path=("meter_reading_window", "period"),
    ),
    DescriereSenzorApaCanal(
        key="index_de_transmis",
        name="Index de transmis",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        icon="mdi:counter",
        key_path=("meter_reading_window", "registers", "0", "previous_reading"),
    ),
    DescriereSenzorApaCanal(
        key="last_consumption",
        name="Ultimul consum",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        icon="mdi:water",
        key_path=("last_consumption", "value"),
    ),
    DescriereSenzorApaCanal(
        key="last_meter_reading",
        name="Ultimul index",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        icon="mdi:gauge",
        key_path=("last_meter_reading", "value"),
    ),
    DescriereSenzorApaCanal(
        key="current_balance",
        name="Sold curent",
        native_unit_of_measurement="RON",
        icon="mdi:cash-multiple",
        key_path=("current_balance", "value"),
    ),
    DescriereSenzorApaCanal(
        key="last_invoice",
        name="Ultima factură",
        native_unit_of_measurement="RON",
        icon="mdi:file-document-outline",
        key_path=("last_invoice", "amount"),
    ),
    DescriereSenzorApaCanal(
        key="last_payment",
        name="Ultima plată",
        native_unit_of_measurement="RON",
        icon="mdi:credit-card-check-outline",
        key_path=("last_payment", "amount"),
    ),
)


@dataclass(frozen=True, kw_only=True)
class DescriereSenzorContDigi(SensorEntityDescription):
    functie_valoare: Any
    functie_atribute: Any | None = None




APA_CANAL_OBJECT_KEY_MAP = {
    "citire_index_permisa": "citire_index_permisa",
    "perioada_citire": "perioada_citire",
    "index_de_transmis": "index_de_transmis",
    "last_consumption": "ultimul_consum",
    "last_meter_reading": "ultimul_index",
    "current_balance": "sold_curent",
    "last_invoice": "ultima_factura",
    "last_payment": "ultima_plata",
}


def _object_key_apa_canal(key: str) -> str:
    return APA_CANAL_OBJECT_KEY_MAP.get(key, key)

SENZORI_CONT_DIGI: tuple[DescriereSenzorContDigi, ...] = (
    DescriereSenzorContDigi(
        key="de_plata",
        name="De plată",
        icon="mdi:cash-clock",
        native_unit_of_measurement="RON",
        functie_valoare=lambda i, c: round(float(_valoare_consum(i, "de_plata", c.id_cont) or 0.0), 2),
    ),
    DescriereSenzorContDigi(
        key="sold_curent",
        name="Sold curent",
        icon="mdi:cash",
        native_unit_of_measurement="RON",
        functie_valoare=lambda i, c: round(float(_valoare_consum(i, "sold_curent", c.id_cont) or 0.0), 2),
    ),
    DescriereSenzorContDigi(
        key="valoare_ultima_factura",
        name="Valoare ultima factură",
        icon="mdi:receipt-text-check",
        native_unit_of_measurement="RON",
        functie_valoare=lambda i, c: round(float(_valoare_ultima_factura(i, c.id_cont) or 0.0), 2),
    ),
    DescriereSenzorContDigi(
        key="id_ultima_factura",
        name="ID ultima factură",
        icon="mdi:file-document-outline",
        functie_valoare=lambda i, c: _id_ultima_factura(i, c.id_cont),
    ),
    DescriereSenzorContDigi(
        key="urmatoarea_scadenta",
        name="Următoarea scadență",
        icon="mdi:calendar-clock",
        functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont),
    ),
    DescriereSenzorContDigi(
        key="factura_restanta",
        name="Factură restantă",
        icon="mdi:alert-circle",
        functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont),
    ),
    DescriereSenzorContDigi(
        key="sold_factura",
        name="Sold factură",
        icon="mdi:cash-refund",
        native_unit_of_measurement="RON",
        functie_valoare=lambda i, c: round(float(_valoare_consum(i, "sold_factura", c.id_cont) or 0.0), 2),
    ),
    DescriereSenzorContDigi(
        key="numar_servicii",
        name="Număr servicii",
        icon="mdi:counter",
        functie_valoare=lambda i, c: _valoare_consum(i, "numar_servicii", c.id_cont),
    ),
)


SENZORI_CONT_ORANGE: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "de_plata", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "sold_curent", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:receipt-text-check", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_ultima_factura(i, c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="id_ultima_factura", name="ID ultima factură", icon="mdi:file-document-outline", functie_valoare=lambda i, c: _id_ultima_factura(i, c.id_cont)),
    DescriereSenzorCont(key="urmatoarea_scadenta", name="Următoarea scadență", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont)),
    DescriereSenzorCont(key="data_ultima_factura", name="Data ultimei facturi", icon="mdi:calendar-text", functie_valoare=lambda i, c: _valoare_consum(i, "data_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="cod_client", name="Cod client", icon="mdi:badge-account", functie_valoare=lambda i, c: _valoare_consum(i, "cod_client", c.id_cont)),
    DescriereSenzorCont(key="nume_abonament", name="Abonament", icon="mdi:cellphone-text", functie_valoare=lambda i, c: _valoare_consum(i, "nume_abonament", c.id_cont)),
    DescriereSenzorCont(key="data_urmatoarei_facturi", name="Data următoarei facturi", icon="mdi:calendar-refresh", functie_valoare=lambda i, c: _valoare_consum(i, "data_urmatoarei_facturi", c.id_cont)),
)


SENZORI_CONT_MYELECTRICA: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="date_client", name="Date client", icon="mdi:account-circle", functie_valoare=lambda i, c: c.nume),
    DescriereSenzorCont(key="date_contract", name="Date contract", icon="mdi:file-document-outline", functie_valoare=lambda i, c: c.stare),
    DescriereSenzorCont(key="index_contor", name="Index contor", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum(i, "index_contor", c.id_cont)),
    DescriereSenzorCont(key="istoric_citiri", name="Istoric citiri", icon="mdi:history", functie_valoare=lambda i, c: _valoare_consum(i, "istoric_citiri", c.id_cont)),
    DescriereSenzorCont(key="citire_permisa", name="Citire permisă", icon="mdi:clock-check-outline", functie_valoare=lambda i, c: _valoare_consum(i, "citire_permisa", c.id_cont)),
    DescriereSenzorCont(key="conventie_consum", name="Convenție consum", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "conventie_consum", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="arhiva_facturi", name="Arhivă facturi", icon="mdi:archive-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_curent", c.id_cont)),
    DescriereSenzorCont(key="numar_plati", name="Număr plăți", icon="mdi:cash-check", functie_valoare=lambda i, c: _valoare_consum(i, "numar_plati", c.id_cont)),
    DescriereSenzorCont(key="arhiva_plati", name="Arhivă plăți", icon="mdi:credit-card-clock-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_plati", c.id_cont)),
    DescriereSenzorCont(key="data_ultima_plata", name="Data ultimei plăți", icon="mdi:calendar-check", functie_valoare=lambda i, c: _valoare_consum(i, "data_ultima_plata", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_plata", name="Valoare ultima plată", icon="mdi:cash-fast", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_plata", c.id_cont)),
)


SENZORI_REZUMAT_DEER: tuple[DescriereSenzorRezumat, ...] = (
    DescriereSenzorRezumat(key="numar_conturi", name="Număr locuri de consum", icon="mdi:transmission-tower", functie_valoare=lambda i: len(i.conturi)),
    DescriereSenzorRezumat(key="cod_client", name="Cod client", icon="mdi:badge-account", functie_valoare=lambda i: _valoare_consum(i, "cod_client")),
    DescriereSenzorRezumat(key="nume_client", name="Client", icon="mdi:account", functie_valoare=lambda i: _valoare_consum(i, "nume_client")),
    DescriereSenzorRezumat(key="este_prosumator", name="Este prosumator", icon="mdi:solar-power-variant", functie_valoare=lambda i: "da" if _este_prosumator(i) else "nu"),
)

SENZORI_CONT_DEER: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="client", name="Client", icon="mdi:account", functie_valoare=lambda i, c: _valoare_consum(i, "client", c.id_cont) or c.nume),
    DescriereSenzorCont(key="cod_client", name="Cod client", icon="mdi:badge-account", functie_valoare=lambda i, c: _valoare_consum(i, "cod_client", c.id_cont)),
    DescriereSenzorCont(key="adresa_loc_consum", name="Adresă loc consum", icon="mdi:map-marker", functie_valoare=lambda i, c: _valoare_consum(i, "adresa_loc_consum", c.id_cont) or c.adresa),
    DescriereSenzorCont(key="loc_consum", name="Loc de consum", icon="mdi:transmission-tower", functie_valoare=lambda i, c: _valoare_consum(i, "loc_consum", c.id_cont) or c.id_cont),
    DescriereSenzorCont(key="profil", name="Profil", icon="mdi:card-account-details-outline", functie_valoare=lambda i, c: _valoare_consum(i, "profil", c.id_cont)),
    DescriereSenzorCont(key="validitate_contract", name="Valabilitate contract", icon="mdi:calendar-range", functie_valoare=lambda i, c: _valoare_consum(i, "validitate_contract", c.id_cont)),
    DescriereSenzorCont(key="denumire_furnizor", name="Denumire furnizor", icon="mdi:store", functie_valoare=lambda i, c: _valoare_consum(i, "denumire_furnizor", c.id_cont)),
    DescriereSenzorCont(key="putere_aprobata_consum", name="Putere aprobată consum", icon="mdi:lightning-bolt", native_unit_of_measurement="kW", functie_valoare=lambda i, c: _valoare_consum(i, "putere_aprobata_consum", c.id_cont)),
    DescriereSenzorCont(key="putere_aprobata_producere", name="Putere aprobată producere", icon="mdi:solar-power", native_unit_of_measurement="kW", functie_valoare=lambda i, c: _valoare_consum(i, "putere_aprobata_producere", c.id_cont)),
    DescriereSenzorCont(key="numar_atr", name="Număr ATR", icon="mdi:identifier", functie_valoare=lambda i, c: _valoare_consum(i, "numar_atr", c.id_cont)),
    DescriereSenzorCont(key="data_inregistrare_atr", name="Data înregistrare ATR", icon="mdi:calendar-edit", functie_valoare=lambda i, c: _valoare_consum(i, "data_inregistrare_atr", c.id_cont)),
    DescriereSenzorCont(key="cod_punct_masurare", name="Cod punct de măsurare", icon="mdi:barcode", functie_valoare=lambda i, c: _valoare_consum(i, "cod_punct_masurare", c.id_cont)),
    DescriereSenzorCont(key="punct_racordare", name="Punct de racordare", icon="mdi:power-plug", functie_valoare=lambda i, c: _valoare_consum(i, "punct_racordare", c.id_cont)),
    DescriereSenzorCont(key="tensiune_delimitare", name="Tensiunea în punctul de delimitare", icon="mdi:sine-wave", functie_valoare=lambda i, c: _valoare_consum(i, "tensiune_delimitare", c.id_cont)),
    DescriereSenzorCont(key="stare_instalatiei", name="Starea instalației", icon="mdi:state-machine", functie_valoare=lambda i, c: _valoare_consum(i, "stare_instalatiei", c.id_cont)),
    DescriereSenzorCont(key="serie_contor", name="Serie contor", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum(i, "serie_contor", c.id_cont)),
    DescriereSenzorCont(key="tip_contor", name="Tip contor", icon="mdi:meter-electric", functie_valoare=lambda i, c: _valoare_consum(i, "tip_contor", c.id_cont)),
    DescriereSenzorCont(key="masurare_orara", name="Măsurare orară", icon="mdi:clock-time-four", functie_valoare=lambda i, c: _valoare_consum(i, "masurare_orara", c.id_cont)),
    DescriereSenzorCont(key="masurare_zone_orare", name="Măsurare zone orare", icon="mdi:clock-time-eight", functie_valoare=lambda i, c: _valoare_consum(i, "masurare_zone_orare", c.id_cont)),
    DescriereSenzorCont(key="clasa_precizie", name="Clasa de precizie", icon="mdi:target", functie_valoare=lambda i, c: _valoare_consum(i, "clasa_precizie", c.id_cont)),
    DescriereSenzorCont(key="index_registru_001", name="Index registru 001", icon="mdi:numeric-1-box-outline", native_unit_of_measurement="kWh", functie_valoare=lambda i, c: _valoare_consum(i, "index_registru_001", c.id_cont)),
    DescriereSenzorCont(key="index_registru_002", name="Index registru 002", icon="mdi:numeric-2-box-outline", native_unit_of_measurement="kWh", functie_valoare=lambda i, c: _valoare_consum(i, "index_registru_002", c.id_cont)),
    DescriereSenzorCont(key="istoric_registru_001", name="Ultimii 10 indici registru 001", icon="mdi:history", functie_valoare=lambda i, c: _valoare_consum(i, "index_registru_001", c.id_cont)),
    DescriereSenzorCont(key="istoric_registru_002", name="Ultimii 10 indici registru 002", icon="mdi:history", functie_valoare=lambda i, c: _valoare_consum(i, "index_registru_002", c.id_cont)),
)


SENZORI_CONT_EBLOC: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="citire_index_permisa", name="Citire index permisă", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum(i, "citire_index_permisa", c.id_cont)),
    DescriereSenzorCont(key="perioada_citire", name="Perioadă citire index", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "perioada_citire", c.id_cont)),
    DescriereSenzorCont(key="zile_pana_citire_index", name="Zile până la citire index", icon="mdi:calendar-arrow-right", functie_valoare=lambda i, c: _valoare_consum(i, "zile_pana_citire_index", c.id_cont)),
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "de_plata", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="valoare_lista_plata", name="Valoare întreținere", icon="mdi:receipt-text-check", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_lista_plata", c.id_cont)),
    DescriereSenzorCont(key="luna_lista_plata", name="Luna întreținerii", icon="mdi:calendar-month", functie_valoare=lambda i, c: _valoare_consum(i, "luna_lista_plata", c.id_cont)),
    DescriereSenzorCont(key="istoric_plati", name="Istoric plăți", icon="mdi:history", functie_valoare=lambda i, c: (f"{_valoare_consum(i, 'numar_plati', c.id_cont)} plăți" if _valoare_consum(i, 'numar_plati', c.id_cont) is not None else None)),
    DescriereSenzorCont(key="numar_persoane", name="Număr persoane", icon="mdi:account-group", functie_valoare=lambda i, c: _valoare_consum(i, "numar_persoane", c.id_cont)),
    DescriereSenzorCont(key="editare_persoane_permisa", name="Modificare persoane permisă", icon="mdi:account-edit", functie_valoare=lambda i, c: _valoare_consum(i, "editare_persoane_permisa", c.id_cont)),
    DescriereSenzorCont(key="luna_setare_persoane", name="Lună setare persoane", icon="mdi:calendar-account", functie_valoare=lambda i, c: _valoare_consum(i, "luna_setare_persoane", c.id_cont)),
    DescriereSenzorCont(key="numar_contoare", name="Număr contoare", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum(i, "numar_contoare", c.id_cont)),
    DescriereSenzorCont(key="urmatoarea_scadenta", name="Următoarea scadență", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont)),
    DescriereSenzorCont(key="data_ultima_plata", name="Data ultimei plăți", icon="mdi:calendar-check", functie_valoare=lambda i, c: _valoare_consum(i, "data_ultima_plata", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_plata", name="Valoare ultima plată", icon="mdi:cash-fast", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_plata", c.id_cont)),
)

def _admin_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMENIU, entry.entry_id)},
        name="Administrare integrare",
        manufacturer="onitium",
        model="Utilitati Romania",
        entry_type=None,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await async_incarca_cache_citiri(hass)
    if entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
        async_add_entities([
            SenzorAdminLicenta(entry, "status", "Status licență"),
            SenzorAdminLicenta(entry, "plan", "Plan licență"),
            SenzorAdminLicenta(entry, "expires_at", "Valabilă până la"),
            SenzorAdminLicenta(entry, "checked_at", "Ultima verificare licență"),
            SenzorAdminLicenta(entry, "utilizator", "Cont licență"),
            SenzorAdminLicenta(entry, "masked_key", "Cod licență mascat"),
            SenzorAdminLicenta(entry, "message", "Mesaj licență"),
            SenzorAdminStatic(entry, "contact", "Contact dezvoltator", "GitHub: @HAForgeLabs"),
            SenzorAdminStatic(entry, "support", "Suport", "github.com/HAForgeLabs/utilitati_romania/issues"),
            SenzorAdminFacturiAgregate(hass, entry),
        ])
        return

    coordonator: CoordonatorUtilitatiRomania = hass.data[DOMENIU][entry.entry_id]
    instantaneu = coordonator.data
    entitati: list[SensorEntity] = []

    if instantaneu and instantaneu.furnizor == "hidroelectrica":
        entitati.extend(SenzorRezumat(coordonator, d) for d in SENZORI_REZUMAT)
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_HIDRO:
                if descriere.key == "index_energie_produsa" and not _cont_este_prosumator(cont):
                    continue
                entitati.append(SenzorContHidroelectrica(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "eon":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_EON:
                entitati.append(SenzorContEon(coordonator, cont, descriere))
            for descriere in SENZORI_CONT_EON_EXTINS:
                entitati.append(SenzorContEonExtins(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "digi":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_DIGI:
                entitati.append(SenzorContDigi(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "orange":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_ORANGE:
                entitati.append(SenzorContOrange(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "myelectrica":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_MYELECTRICA:
                entitati.append(SenzorContMyElectrica(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "deer":
        entitati.extend(SenzorRezumat(coordonator, d) for d in SENZORI_REZUMAT_DEER)
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_DEER:
                entitati.append(SenzorContDeer(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "ebloc":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_EBLOC:
                entitati.append(SenzorContEbloc(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "apa_canal":
        for descriere in SENZORI_APA_CANAL:
            entitati.append(SenzorApaCanal(coordonator, entry, descriere))

    elif instantaneu:
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))

    async_add_entities(entitati)


class SenzorRezumat(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorRezumat

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, descriere: DescriereSenzorRezumat) -> None:
        super().__init__(coordonator)
        self.entity_description = descriere
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_{descriere.key}"
        instantaneu = coordonator.data
        if instantaneu and instantaneu.furnizor == "nova":
            conturi = instantaneu.conturi or []
            if len(conturi) == 1:
                slug = build_provider_slug("nova", getattr(conturi[0], "adresa", None), getattr(conturi[0], "id_cont", None))
                self._attr_suggested_object_id = f"{slug}_{descriere.key}"
                self.entity_id = f"sensor.{slug}_{descriere.key}"
            elif len(conturi) > 1:
                self._attr_suggested_object_id = f"nova_multi_{descriere.key}"
                self.entity_id = f"sensor.nova_multi_{descriere.key}"

    @property
    def native_value(self):
        return None if self.coordinator.data is None else self.entity_description.functie_valoare(self.coordinator.data)


class SenzorContHidroelectrica(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        alias = alias_loc_consum(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_consum(cont.id_cont, alias, cont.adresa)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_hidro_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"hidro_{cont.id_cont}_{slug}_{descriere.key}"
        self.entity_id = f"sensor.hidro_{cont.id_cont}_{slug}_{descriere.key}"
        self._attr_device_info = info_device_hidro(coordonator.intrare.entry_id, cont)

    @property
    def _cont_actual(self):
        return _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) or self.cont

    @property
    def available(self):
        return _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) is not None

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.functie_valoare(self.coordinator.data, self._cont_actual)

    @property
    def extra_state_attributes(self):
        cont = self._cont_actual

        attrs = {
            "id_cont": cont.id_cont,
            "nume_cont": cont.nume,
            "tip_serviciu": cont.tip_serviciu,
            "tip_utilitate": cont.tip_utilitate,
            "adresa": cont.adresa,
        }

        citire = obtine_citire_cache(self.hass, "hidroelectrica", cont.id_cont)
        if citire:
            attrs["ultima_citire_transmisa"] = citire.get("valoare")
            attrs["ultima_citire_transmisa_la"] = citire.get("timestamp")

        return attrs


class SenzorContEon(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        alias = alias_loc_eon(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_serviciu_loc_eon(cont)
        identificator = id_unic_eon(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_eon_{identificator}_{descriere.key}"
        if descriere.key == "index_contor":
            self._attr_name = 'Index gaz' if _tip_eon(cont) == 'gaz' else 'Index energie electrică'
            self._attr_native_unit_of_measurement = 'm³' if _tip_eon(cont) == 'gaz' else 'kWh'
        else:
            self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_eon(coordonator.intrare.entry_id, cont)

    @property
    def available(self):
        if self.coordinator.data is None:
            return False
        return any(getattr(cont, "id_cont", None) == getattr(self.cont, "id_cont", None) for cont in self.coordinator.data.conturi)

    @property
    def native_value(self):
        return None if self.coordinator.data is None else self.entity_description.functie_valoare(self.coordinator.data, self.cont)

    @property
    def extra_state_attributes(self):
        attrs = {
            "id_cont": self.cont.id_cont,
            "nume_cont": self.cont.nume,
            "tip_serviciu": self.cont.tip_serviciu,
            "tip_utilitate": self.cont.tip_utilitate,
            "serviciu_eon": cheie_serviciu_eon(self.cont),
            "identificator_eon": id_unic_eon(self.cont),
            "adresa": self.cont.adresa,
        }
        raw = _date_brute_cont(self.cont)
        if self.entity_description.key == "urmatoarea_scadenta":
            attrs["cod_contract"] = raw.get("cod_contract")
        return attrs


class SenzorContEonExtins(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorContEonExtins

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorContEonExtins) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        alias = alias_loc_eon(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_serviciu_loc_eon(cont)
        identificator = id_unic_eon(cont)
        tip = _tip_eon(cont)
        an = _an_curent_loc_eon(cont)

        if descriere.key == "arhiva_consum":
            self._attr_name = f"{an} → Arhivă consum {'gaz' if tip == 'gaz' else 'energie electrică'}"
            self._attr_suggested_object_id = f"{slug}_arhiva_consum_{'gaz' if tip == 'gaz' else 'energie_electrica'}_{an}"
            self._attr_native_unit_of_measurement = "m³" if tip == "gaz" else "kWh"
        elif descriere.key == "arhiva_index":
            self._attr_name = f"{an} → Arhivă index {'gaz' if tip == 'gaz' else 'energie electrică'}"
            self._attr_suggested_object_id = f"{slug}_arhiva_index_{'gaz' if tip == 'gaz' else 'energie_electrica'}_{an}"
        elif descriere.key == "arhiva_plati":
            self._attr_name = f"{an} → Arhivă plăți"
            self._attr_suggested_object_id = f"{slug}_arhiva_plati_{an}"
        else:
            self._attr_name = descriere.name
            self._attr_suggested_object_id = f"{slug}_{descriere.key}"

        self._attr_unique_id = f"{coordonator.intrare.entry_id}_eon_{identificator}_{descriere.key}_{an if descriere.key.startswith('arhiva_') else 'base'}"
        self.entity_id = f"sensor.{self._attr_suggested_object_id}"
        self._attr_icon = descriere.icon
        self._attr_device_info = info_device_eon(coordonator.intrare.entry_id, cont)

    @property
    def native_value(self):
        return self.entity_description.functie_valoare(self.cont)

    @property
    def extra_state_attributes(self):
        raw = _date_brute_cont(self.cont)
        attrs = {
            "id_cont": self.cont.id_cont,
            "nume_cont": self.cont.nume,
            "tip_serviciu": self.cont.tip_serviciu,
            "tip_utilitate": self.cont.tip_utilitate,
            "serviciu_eon": cheie_serviciu_eon(self.cont),
            "identificator_eon": id_unic_eon(self.cont),
            "adresa": self.cont.adresa,
        }

        if self.entity_description.key == "date_contract":
            contract = raw.get("date_contract") or {}
            if isinstance(contract, dict):
                for cheie in (
                    "accountContract",
                    "consumptionPointCode",
                    "pod",
                    "distributorName",
                    "productName",
                    "statusLabel",
                    "utilityType",
                ):
                    if contract.get(cheie) not in (None, ""):
                        attrs[cheie] = contract.get(cheie)

        elif self.entity_description.key == "conventie_consum":
            conventie = raw.get("conventie_consum") or {}
            if isinstance(conventie, dict):
                attrs.update({f"luna_{k}": v for k, v in conventie.items()})

        elif self.entity_description.key == "arhiva_consum":
            attrs["an"] = _an_curent_loc_eon(self.cont)
            attrs["valoare_totală"] = raw.get("consum_total")
            attrs["consum_luna_curenta"] = raw.get("consum_luna_curenta")

        elif self.entity_description.key == "arhiva_index":
            an = _an_curent_loc_eon(self.cont)
            attrs["an"] = an
            attrs["citiri"] = [x for x in (raw.get("istoric_index") or []) if str(x.get("an")) == str(an)]

        elif self.entity_description.key == "arhiva_plati":
            an = _an_curent_loc_eon(self.cont)
            attrs["an"] = an
            attrs["plati"] = [x for x in (raw.get("istoric_plati") or []) if str(x.get("data", ""))[:4] == str(an)]

        return attrs


class SenzorContMyElectrica(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        alias = alias_loc_myelectrica(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_myelectrica(cont.id_cont, alias, cont.adresa)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_hidro_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"hidro_{cont.id_cont}_{slug}_{descriere.key}"
        self.entity_id = f"sensor.hidro_{cont.id_cont}_{slug}_{descriere.key}"
        self._attr_device_info = info_device_myelectrica(coordonator.intrare.entry_id, cont)
        if descriere.key == 'index_contor':
            tip = str(cont.tip_serviciu or cont.tip_utilitate or '').lower()
            self._attr_native_unit_of_measurement = 'm³' if tip == 'gaz' else 'kWh'

    @property
    def available(self):
        if self.coordinator.data is None:
            return False
        return any(getattr(cont, "id_cont", None) == getattr(self.cont, "id_cont", None) for cont in self.coordinator.data.conturi)

    @property
    def native_value(self):
        return None if self.coordinator.data is None else self.entity_description.functie_valoare(self.coordinator.data, self.cont)

    @property
    def extra_state_attributes(self):
        raw = getattr(self.cont, 'date_brute', None) or {}
        attrs = {
            'nlc': self.cont.id_cont,
            'client_code': raw.get('client_code'),
            'contract_account': raw.get('contract_account'),
            'adresa': self.cont.adresa,
            'tip_serviciu': self.cont.tip_serviciu,
        }
        if self.entity_description.key == 'date_client':
            client = raw.get('client_data') or {}
            for cheie in ('ClientName', 'ClientType', 'Email', 'PhoneNumber', 'MobilePhoneNumber', 'TaxNumber'):
                if client.get(cheie) not in (None, ''):
                    attrs[cheie] = client.get(cheie)
        elif self.entity_description.key == 'date_contract':
            contract = raw.get('contract_details') or {}
            for cheie in ('ContractStatus', 'ContractAccount', 'NLC', 'ServiceType', 'OfferName', 'TariffType', 'InvoiceType', 'PaymentMethod'):
                if contract.get(cheie) not in (None, ''):
                    attrs[cheie] = contract.get(cheie)
        elif self.entity_description.key == 'index_contor':
            attrs.update({
                'serie_contor': raw.get('serie_contor'),
                'register_code': raw.get('register_code'),
            })
            meter = raw.get('meter_list') or {}
            if meter.get('MeterReadingEstimated') not in (None, ''):
                attrs['citire_estimata'] = meter.get('MeterReadingEstimated')
        elif self.entity_description.key == 'istoric_citiri':
            citiri = raw.get('readings') or []
            attrs['numar_citiri'] = len(citiri)
            attrs['ultima_citire'] = citiri[-1] if citiri else None
        elif self.entity_description.key == 'citire_permisa':
            meter = raw.get('meter_list') or {}
            if meter.get('StartDatePAC'):
                attrs['inceput_perioada'] = meter.get('StartDatePAC')
            if meter.get('EndDatePAC'):
                attrs['sfarsit_perioada'] = meter.get('EndDatePAC')
            if meter.get('PACIndicator') not in (None, ''):
                attrs['pac_indicator'] = meter.get('PACIndicator')
        elif self.entity_description.key == 'conventie_consum':
            conventie = raw.get('convention') or []
            attrs['numar_luni'] = len(conventie)
            attrs['total_conventie'] = round(sum(float(x.get('Quantity') or 0) for x in conventie if isinstance(x, dict)), 2) if conventie else 0
            attrs['ultima_luna'] = conventie[-1] if conventie else None
        elif self.entity_description.key in {'numar_facturi', 'arhiva_facturi', 'factura_restanta', 'sold_curent'}:
            facturi = raw.get('invoices') or []
            attrs['numar_facturi'] = len(facturi)
            attrs['ultima_factura'] = facturi[-1] if facturi else None
            if self.entity_description.key == 'arhiva_facturi':
                attrs['ultimele_10_facturi'] = list(facturi[-10:])
                attrs['ultima_factura_id'] = raw.get('ultima_factura_id')
                attrs['valoare_ultima_factura'] = raw.get('valoare_ultima_factura')
        elif self.entity_description.key in {'numar_plati', 'arhiva_plati', 'valoare_ultima_plata', 'data_ultima_plata'}:
            plati = raw.get('payments') or []
            attrs['numar_plati'] = len(plati)
            attrs['ultima_plata'] = plati[-1] if plati else None
            if self.entity_description.key == 'arhiva_plati':
                attrs['ultimele_10_plati'] = list(plati[-10:])
                attrs['data_ultima_plata'] = raw.get('data_ultima_plata')
                attrs['valoare_ultima_plata'] = raw.get('valoare_ultima_plata')
        return attrs


def info_device_digi(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "digi")
    nume = getattr(cont, "nume", "Digi")
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_digi_{ident}")},
        name=f"Digi - {nume}",
        manufacturer="Digi România",
        model="Servicii",
    )


class SenzorContDigi(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorContDigi

    def __init__(
        self,
        coordonator: CoordonatorUtilitatiRomania,
        cont,
        descriere: DescriereSenzorContDigi,
    ) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere

        slug_strada = _slug_strada_digi(cont)

        self._attr_unique_id = (
            f"{coordonator.intrare.entry_id}_digi_{slug_strada}_{descriere.key}"
        )
        self._attr_name = descriere.name.strip()
        self._attr_suggested_object_id = f"digi_{slug_strada}_{descriere.key}"
        self.entity_id = f"sensor.digi_{slug_strada}_{descriere.key}"
        self._attr_device_info = info_device_digi(coordonator.intrare.entry_id, cont)

    @property
    def has_entity_name(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return self.entity_description.name.strip()

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.functie_valoare(
            self.coordinator.data, self.cont
        )

    @property
    def extra_state_attributes(self):
        raw = getattr(self.cont, "date_brute", None) or {}
        latest = raw.get("latest") or {}

        attrs = {
            "id_cont": self.cont.id_cont,
            "tip_serviciu": self.cont.tip_serviciu,
            "tip_utilitate": self.cont.tip_utilitate,
        }

        key = self.entity_description.key

        if key in {
            "de_plata",
            "sold_curent",
            "sold_factura",
            "factura_restanta",
            "urmatoarea_scadenta",
            "valoare_ultima_factura",
            "id_ultima_factura",
        }:
            for cheie in (
                "invoice_id",
                "invoice_number",
                "issue_date",
                "due_date",
                "status",
            ):
                if latest.get(cheie) not in (None, ""):
                    attrs[cheie] = latest.get(cheie)

        if key in {"valoare_ultima_factura", "id_ultima_factura"}:
            if latest.get("pdf_url") not in (None, ""):
                attrs["pdf_url"] = latest.get("pdf_url")

        if key == "numar_servicii":
            servicii = latest.get("services")
            if servicii:
                attrs["services"] = servicii

        return attrs


class SenzorContOrange(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        alias = alias_loc_orange(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_orange(cont.id_cont, alias, cont.adresa)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_orange_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_orange(coordonator.intrare.entry_id, cont)

    @property
    def available(self):
        if self.coordinator.data is None:
            return False
        return any(getattr(cont, "id_cont", None) == getattr(self.cont, "id_cont", None) for cont in self.coordinator.data.conturi)

    @property
    def _cont_actual(self):
        return _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) or self.cont

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.functie_valoare(self.coordinator.data, self._cont_actual)

    @property
    def extra_state_attributes(self):
        cont = self._cont_actual
        raw = _date_brute_cont(cont)
        attrs = {
            "id_cont": cont.id_cont,
            "nume_cont": cont.nume,
            "tip_serviciu": cont.tip_serviciu,
            "tip_utilitate": cont.tip_utilitate,
            "status_serviciu": cont.stare,
            "subscriber_id": raw.get("subscriberId"),
            "profile_id": raw.get("profileId"),
        }

        if self.coordinator.data is None:
            return attrs

        factura = _ultima_factura(self.coordinator.data, id_cont=cont.id_cont)
        if factura is not None:
            attrs.update({
                "invoice_id": factura.id_factura,
                "issue_date": factura.data_emitere.isoformat() if factura.data_emitere else None,
                "due_date": factura.data_scadenta.isoformat() if factura.data_scadenta else None,
                "status": factura.stare,
                "amount": factura.valoare,
            })
            factura_raw = factura.date_brute if isinstance(factura.date_brute, dict) else {}
            response = factura_raw.get("invoice_response") if isinstance(factura_raw.get("invoice_response"), dict) else {}
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            if data.get("customerNumber") not in (None, ""):
                attrs["customer_number"] = data.get("customerNumber")
            if factura_raw.get("history_item"):
                attrs["history_item"] = factura_raw.get("history_item")

        return attrs


class SenzorContDeer(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        alias = alias_loc_deer(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_deer(cont.id_cont, alias, cont.adresa, cont.nume)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_hidro_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"hidro_{cont.id_cont}_{slug}_{descriere.key}"
        self.entity_id = f"sensor.hidro_{cont.id_cont}_{slug}_{descriere.key}"
        self._attr_device_info = info_device_deer(coordonator.intrare.entry_id, cont)

    @property
    def available(self):
        if self.coordinator.data is None:
            return False
        return any(getattr(cont, "id_cont", None) == getattr(self.cont, "id_cont", None) for cont in self.coordinator.data.conturi)

    @property
    def native_value(self):
        return None if self.coordinator.data is None else self.entity_description.functie_valoare(self.coordinator.data, self.cont)

    @property
    def extra_state_attributes(self):
        raw = _date_brute_cont(self.cont)
        attrs = {
            "pod": self.cont.id_cont,
            "adresa": self.cont.adresa,
            "nume_cont": self.cont.nume,
            "tip_serviciu": self.cont.tip_serviciu,
            "tip_utilitate": self.cont.tip_utilitate,
        }
        istoric_001 = raw.get("istoric_registru_001") or []
        istoric_002 = raw.get("istoric_registru_002") or []
        if self.entity_description.key == "validitate_contract":
            attrs["contract"] = raw.get("validitate_contract")
        elif self.entity_description.key in {"index_registru_001", "istoric_registru_001"}:
            attrs["ultimele_10_indici"] = istoric_001[-10:]
            attrs["numar_indici"] = len(istoric_001)
        elif self.entity_description.key in {"index_registru_002", "istoric_registru_002"}:
            attrs["ultimele_10_indici"] = istoric_002[-10:]
            attrs["numar_indici"] = len(istoric_002)
        elif self.entity_description.key in {"serie_contor", "tip_contor", "clasa_precizie"}:
            for key in ("serie_contor", "tip_contor", "clasa_precizie"):
                if raw.get(key) not in (None, ""):
                    attrs[key] = raw.get(key)
        return attrs



class SenzorContEbloc(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere

        alias = alias_loc_ebloc(cont.nume, cont.adresa, cont.id_cont, cont=cont)
        slug = slug_loc_ebloc(cont.id_cont, alias, cont.adresa, cont=cont)

        self._attr_unique_id = f"{coordonator.intrare.entry_id}_ebloc_{cont.id_cont}_{descriere.key}"
        self._attr_name = f"{descriere.name} - {alias}"
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_ebloc(coordonator.intrare.entry_id, cont)

    @property
    def available(self):
        if self.coordinator.data is None:
            return False
        return any(
            getattr(cont, "id_cont", None) == getattr(self.cont, "id_cont", None)
            for cont in self.coordinator.data.conturi
        )

    @property
    def _cont_actual(self):
        return _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) or self.cont

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.functie_valoare(self.coordinator.data, self._cont_actual)

    @property
    def extra_state_attributes(self):
        cont = self._cont_actual
        attrs = {
            "id_cont": cont.id_cont,
            "nume_cont": cont.nume,
            "adresa": cont.adresa,
        }

        if getattr(cont, "id_contract", None):
            attrs["id_contract"] = cont.id_contract

        raw = _date_brute_cont(cont)
        if raw.get("id_asociatie") not in (None, ""):
            attrs["id_asociatie"] = raw.get("id_asociatie")
        if raw.get("id_apartament") not in (None, ""):
            attrs["id_apartament"] = raw.get("id_apartament")

        if self.coordinator.data is None:
            return attrs

        if self.entity_description.key == "istoric_plati":
            consum_istoric = _consum_dupa_cheie(self.coordinator.data, "istoric_plati", cont.id_cont)
            raw_istoric = getattr(consum_istoric, "date_brute", None) if consum_istoric is not None else None
            if isinstance(raw_istoric, dict):
                attrs["ultimele_12_plati"] = raw_istoric.get("plati", [])
            return attrs

        if self.entity_description.key in {"id_ultima_factura", "valoare_ultima_factura", "numar_facturi", "valoare_lista_plata", "luna_lista_plata"}:
            facturi = [
                f
                for f in (self.coordinator.data.facturi if self.coordinator.data else [])
                if getattr(f, "id_cont", None) == cont.id_cont
            ]
            attrs["numar_liste_plata"] = len(facturi)
            if facturi:
                ultima = sorted(facturi, key=lambda f: f.data_emitere or date.min, reverse=True)[0]
                attrs["ultima_lista_plata_id"] = ultima.id_factura
                attrs["ultima_lista_plata_titlu"] = ultima.titlu
                attrs["ultima_lista_plata_stare"] = ultima.stare
                attrs["ultima_lista_plata_valoare"] = ultima.valoare
                attrs["ultima_lista_plata_data"] = ultima.data_emitere.isoformat() if ultima.data_emitere else None
            return attrs

        if self.entity_description.key == "perioada_citire":
            attrs["perioada_citire"] = _valoare_consum(self.coordinator.data, "perioada_citire", cont.id_cont)
            return attrs

        return attrs




class SenzorApaCanal(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorApaCanal

    def __init__(
        self,
        coordonator: CoordonatorUtilitatiRomania,
        entry: ConfigEntry,
        descriere: DescriereSenzorApaCanal,
    ) -> None:
        super().__init__(coordonator)
        self.entity_description = descriere
        self._entry = entry
        eticheta = str(entry.data.get("premise_label") or entry.title or "contract").strip()
        slug = build_provider_slug("apa_canal_sibiu", eticheta, eticheta)
        object_key = _object_key_apa_canal(descriere.key)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_{object_key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{object_key}"
        self.entity_id = f"sensor.{slug}_{object_key}"

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data.extra if self.coordinator.data else {}
        value: Any = data
        for key in self.entity_description.key_path:
            if isinstance(value, list):
                try:
                    value = value[int(key)]
                except (TypeError, ValueError, IndexError):
                    return None
                continue
            if not isinstance(value, dict):
                return None
            value = value.get(key)

        if self.entity_description.key == "citire_index_permisa":
            return "da" if value else "nu"
        if self.entity_description.key == "perioada_citire":
            return value or "Închisă"
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data.extra if self.coordinator.data else {}

        if self.entity_description.key in {"citire_index_permisa", "perioada_citire", "index_de_transmis"}:
            item = data.get("meter_reading_window") or {}
            registre = item.get("registers") or []
            primul = registre[0] if registre else {}
            cont = (self.coordinator.data.conturi or [None])[0] if self.coordinator.data else None
            id_cont = getattr(cont, "id_cont", None)
            id_contract = getattr(cont, "id_contract", None)
            number_unique_id = f"{self._entry.entry_id}_apa_canal_{id_cont}_index_de_transmis" if id_cont else None
            button_unique_id = f"{self._entry.entry_id}_apa_canal_{id_cont}_trimite_index" if id_cont else None
            number_entity_id = None
            button_entity_id = None
            if self.hass and number_unique_id and button_unique_id:
                registru_entitati = er.async_get(self.hass)
                number_entity_id = registru_entitati.async_get_entity_id("number", DOMENIU, number_unique_id)
                button_entity_id = registru_entitati.async_get_entity_id("button", DOMENIU, button_unique_id)
            return {
                "id_cont": id_cont,
                "id_contract": id_contract,
                "citire_permisa": "da" if item.get("is_open") else "nu",
                "inceput_perioada": item.get("start_date"),
                "sfarsit_perioada": item.get("end_date"),
                "perioada_citire": item.get("period"),
                "device_id": primul.get("device_id"),
                "register_id": primul.get("register_id"),
                "serial_number": primul.get("serial_number"),
                "previous_reading": primul.get("previous_reading"),
                "previous_reading_date": primul.get("previous_reading_date"),
                "unit": primul.get("unit"),
                "number_entity_id": number_entity_id,
                "button_entity_id": button_entity_id,
                "number_unique_id": number_unique_id,
                "button_unique_id": button_unique_id,
            }

        if self.entity_description.key == "last_consumption":
            item = data.get("last_consumption") or {}
            return {
                "unit": item.get("unit"),
                "start_date": item.get("start_date"),
                "end_date": item.get("end_date"),
                "billing_period_year": item.get("billing_period_year"),
                "billing_period_month": item.get("billing_period_month"),
                "reading_category": item.get("reading_category"),
                "billed_amount": item.get("billed_amount"),
                "currency": item.get("currency"),
            }

        if self.entity_description.key == "last_meter_reading":
            item = data.get("last_meter_reading") or {}
            return {
                "date": item.get("date"),
                "unit": item.get("unit"),
                "consumption": item.get("consumption"),
                "reason": item.get("reason"),
                "category": item.get("category"),
                "status": item.get("status"),
                "invoice_status": item.get("invoice_status"),
                "serial_number": item.get("serial_number"),
            }

        if self.entity_description.key == "current_balance":
            item = data.get("current_balance") or {}
            return {
                "currency": item.get("currency"),
                "open_debits": item.get("open_debits"),
                "open_credits": item.get("open_credits"),
                "total_pending": item.get("total_pending"),
            }

        if self.entity_description.key == "last_invoice":
            item = data.get("last_invoice") or {}
            return {
                "number": item.get("number"),
                "issue_date": item.get("issue_date"),
                "due_date": item.get("due_date"),
                "amount_paid": item.get("amount_paid"),
                "amount_remaining": item.get("amount_remaining"),
                "description": item.get("description"),
                "currency": item.get("currency"),
            }

        if self.entity_description.key == "last_payment":
            item = data.get("last_payment") or {}
            return {
                "document_id": item.get("document_id"),
                "date": item.get("date"),
                "method": item.get("method"),
                "payment_type": item.get("payment_type"),
                "currency": item.get("currency"),
            }

        return None

