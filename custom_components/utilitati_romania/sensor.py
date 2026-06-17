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
from .engie_device import alias_loc_engie, info_device_engie, slug_loc_engie
from .naming import build_provider_slug, extract_street_slug, normalize_text
from .furnizori.apa_brasov import nume_scurt_locatie_apa_brasov
from .licentiere import async_obtine_licenta_globala, mascheaza_cheia_licenta
from .facturi_agregate import colecteaza_facturi_agregate, colecteaza_locuri_consum, sumar_facturi
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
            self._sumar["locuri_consum"] = colecteaza_locuri_consum(self.hass)
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
            "locuri_consum": self._sumar.get("locuri_consum", []),
            "locuri_consum_ignorate": [item for item in self._sumar.get("locuri_consum", []) if item.get("ignored")],
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


def _este_id_factura_tehnic(valoare: Any) -> bool:
    """Detectează tokenurile tehnice care nu trebuie afișate ca număr de factură."""
    if valoare in (None, ""):
        return False
    text = str(valoare).strip()
    if not text:
        return False
    if text.endswith("==") or (len(text) >= 20 and any(ch in text for ch in "+/=")):
        return True
    if len(text) >= 32 and all(ch.isalnum() for ch in text):
        return any(ch.islower() for ch in text) and any(ch.isupper() for ch in text) and any(ch.isdigit() for ch in text)
    return False


def _id_factura_afisabil(valoare: Any) -> str | None:
    """Returnează ID-ul facturii doar dacă pare lizibil pentru utilizator."""
    if valoare in (None, "", "unknown", "Unknown", "Necunoscut"):
        return None
    text = str(valoare).strip()
    if not text or _este_id_factura_tehnic(text):
        return None
    return text

def _id_ultima_factura_rezumat(instantaneu: InstantaneuFurnizor):
    if instantaneu.furnizor in {"digi", "comprest"}:
        valoare = _id_factura_afisabil(_valoare_consum_global(instantaneu, "id_ultima_factura"))
        if valoare is not None:
            return valoare
    return _id_ultima_factura(instantaneu)


def _valoare_ultima_factura_rezumat(instantaneu: InstantaneuFurnizor):
    if instantaneu.furnizor == "digi":
        return _valoare_consum_global(instantaneu, "valoare_ultima_factura")
    return _valoare_ultima_factura(instantaneu)





def _to_float_safe(valoare: Any) -> float | None:
    """Convertește o valoare numerică primită de la furnizori în float."""
    if valoare in (None, "", "unknown", "Unknown", "Necunoscut"):
        return None
    if isinstance(valoare, (int, float)):
        return float(valoare)
    text = str(valoare).strip()
    if not text:
        return None
    text = text.replace("RON", "").replace("lei", "").replace("Lei", "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return float(text.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _factura_ultima_consum_model(
    instantaneu: InstantaneuFurnizor,
    id_cont: str | None = None,
) -> FacturaUtilitate | None:
    """Alege ultima factură reală de consum pentru un loc de consum.

    Pentru costuri nu folosim soldul total sau restul cumulat, ci factura
    individuală din istoricul furnizorului. Acest lucru este important mai ales
    la furnizori precum Hidroelectrica, unde soldul de plată poate include mai
    multe facturi neachitate.
    """
    facturi = list(instantaneu.facturi or [])
    if id_cont is not None:
        facturi = [f for f in facturi if getattr(f, "id_cont", None) == id_cont]

    candidate: list[FacturaUtilitate] = []
    for factura in facturi:
        categorie = str(getattr(factura, "categorie", None) or "").strip().lower()
        if categorie and categorie not in {"consum", "energie", "gaz", "curent", "apa_canal", "telecomunicatii"}:
            continue
        valoare = _to_float_safe(getattr(factura, "valoare", None))
        if valoare is None or valoare <= 0:
            continue
        candidate.append(factura)

    if not candidate:
        return None

    return sorted(
        candidate,
        key=lambda f: (getattr(f, "data_emitere", None) or date.min, getattr(f, "data_scadenta", None) or date.min),
        reverse=True,
    )[0]


def _unitate_cost_pentru_cont(cont=None) -> str | None:
    """Determină unitatea de consum pentru calculul costului mediu facturat."""
    if cont is None:
        return None
    text = " ".join(
        str(valoare or "").lower()
        for valoare in (
            getattr(cont, "tip_serviciu", None),
            getattr(cont, "tip_utilitate", None),
            getattr(cont, "tip_cont", None),
            getattr(cont, "nume", None),
        )
    )
    if any(marker in text for marker in ("curent", "electric", "energie")):
        return "kWh"
    if "gaz" in text:
        return "kWh"
    if any(marker in text for marker in ("apa", "apă", "canal")):
        return "m³"
    return None


def _unitate_cost_din_date_brute(node: Any) -> str | None:
    """Caută o unitate explicită în structurile brute ale furnizorului."""
    if isinstance(node, dict):
        for cheie in ("unit", "Unit", "unitOfMeasure", "measureUnit", "uom", "UOM", "unitate"):
            unitate = str(node.get(cheie) or "").strip()
            if not unitate:
                continue
            unitate_norm = unitate.lower().replace("mc", "m³").replace("m3", "m³")
            if "kwh" in unitate_norm:
                return "kWh"
            if "m³" in unitate_norm or "metru" in unitate_norm:
                return "m³"
        for valoare in node.values():
            gasit = _unitate_cost_din_date_brute(valoare)
            if gasit:
                return gasit
    elif isinstance(node, list):
        for item in node:
            gasit = _unitate_cost_din_date_brute(item)
            if gasit:
                return gasit
    return None


def _unitate_cost_ultima_factura(instantaneu: InstantaneuFurnizor, cont=None) -> str | None:
    id_cont = getattr(cont, "id_cont", None) if cont is not None else None
    factura = _factura_ultima_consum_model(instantaneu, id_cont)
    unitate = _unitate_cost_din_date_brute(getattr(factura, "date_brute", None) if factura is not None else None)
    return unitate or _unitate_cost_pentru_cont(cont)


def _aplica_unitate_cost_mediu(entity: SensorEntity, cont=None) -> None:
    """Setează unitatea nativă pentru senzorul de cost mediu pe unitate."""
    descriere = getattr(entity, "entity_description", None)
    if getattr(descriere, "key", None) != "cost_mediu_unitate_ultima_factura":
        return
    unitate = _unitate_cost_pentru_cont(cont) or "unitate"
    entity._attr_native_unit_of_measurement = f"RON/{unitate}"


def _extrage_consum_unitate_din_date_brute(node: Any, unitate_asteptata: str | None = None) -> float | None:
    """Extrage prudent consumul facturat, fără să fie limitat doar la kWh.

    Acceptăm câmpuri explicite de consum și câmpuri generice doar când obiectul
    conține o unitate clară. Nu folosim valori financiare sau solduri.
    """
    if node in (None, ""):
        return None

    unitate_norm = (unitate_asteptata or "").lower().replace("mc", "m³").replace("m3", "m³")

    if isinstance(node, dict):
        rezultate: list[float] = []
        unitate_obiect = _unitate_cost_din_date_brute(node)
        unitate_obiect_norm = (unitate_obiect or "").lower()
        unitate_potrivita = not unitate_norm or not unitate_obiect_norm or unitate_obiect_norm == unitate_norm

        chei_consum = {
            "consum", "consumption", "consumfacturat", "billedconsumption",
            "consumptionbilled", "invoiceconsumption", "energyconsumption",
            "activeenergy", "quantity", "qty", "billedquantity", "volum",
            "volume", "waterconsumption", "gasconsumption", "usage", "usagevalue",
        }
        chei_interzise = (
            "amount", "valueamount", "invoiceamount", "billamount", "balance",
            "sold", "rest", "remaining", "payment", "price", "tariff", "tva",
            "vat", "total", "valoare", "suma",
        )

        for cheie, valoare in node.items():
            cheie_norm = str(cheie).lower().replace("_", "").replace("-", "")
            if any(blocat in cheie_norm for blocat in chei_interzise):
                continue
            if (
                cheie_norm in chei_consum
                or "kwh" in cheie_norm
                or "consum" in cheie_norm
                or "consumption" in cheie_norm
            ) and unitate_potrivita:
                numeric = _to_float_safe(valoare)
                if numeric is not None and numeric > 0:
                    rezultate.append(numeric)

        for valoare in node.values():
            nested = _extrage_consum_unitate_din_date_brute(valoare, unitate_asteptata)
            if nested is not None and nested > 0:
                rezultate.append(nested)

        if not rezultate:
            return None

        unice: list[float] = []
        for valoare in rezultate:
            if not any(abs(valoare - existenta) < 0.001 for existenta in unice):
                unice.append(valoare)
        return round(sum(unice), 3)

    if isinstance(node, list):
        total = 0.0
        gasit = False
        for item in node:
            valoare = _extrage_consum_unitate_din_date_brute(item, unitate_asteptata)
            if valoare is not None and valoare > 0:
                total += valoare
                gasit = True
        return round(total, 3) if gasit else None

    return None


def _parseaza_data_generica(valoare: Any) -> date | None:
    if valoare in (None, ""):
        return None
    if isinstance(valoare, date):
        return valoare
    text = str(valoare).strip()
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:len(fmt.replace('%f','000000'))] if '%f' not in fmt else text[:26], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _consum_din_diferenta_indici(cont=None, unitate: str | None = None) -> float | None:
    """Fallback pentru furnizori care expun istoricul de index, nu consumul facturat."""
    raw = getattr(cont, "date_brute", None) or {}
    payloaduri = [
        raw.get("meter_read_history"),
        raw.get("history_payload"),
        raw.get("istoric_index"),
        raw.get("readings"),
        raw.get("citiri"),
    ]

    index_keys = (
        "MRResult", "mrResult", "prevMRResult", "Index", "index", "meterRead",
        "meterread", "readValue", "ReadValue", "newmeterread", "NewMeterRead",
        "CurrentRead", "currentRead", "readingValue", "ReadingValue", "index_nou",
    )
    date_keys = (
        "MRDate", "mrDate", "Date", "date", "readDate", "ReadDate",
        "meterReadDate", "MeterReadDate", "prevMRDate", "createdOn", "CreatedOn", "data",
    )
    register_keys = ("register", "Register", "registerCode", "RegisterCode", "obis", "OBIS", "Tip registru", "tip_registru")

    randuri: list[tuple[date, float, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            valoare_index = None
            for cheie in index_keys:
                if cheie in node:
                    valoare_index = _to_float_safe(node.get(cheie))
                    if valoare_index is not None:
                        break
            data_index = None
            for cheie in date_keys:
                if cheie in node:
                    data_index = _parseaza_data_generica(node.get(cheie))
                    if data_index is not None:
                        break
            registru = ""
            for cheie in register_keys:
                if cheie in node and node.get(cheie) not in (None, ""):
                    registru = str(node.get(cheie)).strip()
                    break
            if valoare_index is not None and data_index is not None:
                randuri.append((data_index, valoare_index, registru))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for payload in payloaduri:
        walk(payload)

    if not randuri:
        return None

    if unitate == "kWh":
        filtrate = [r for r in randuri if not r[2] or str(r[2]).strip() in {"1.8.0", "1.8.0_P"} or "1.8.0" in str(r[2])]
        if filtrate:
            randuri = filtrate

    randuri.sort(key=lambda item: (item[0], item[1]))
    unice: list[tuple[date, float, str]] = []
    for rand in randuri:
        if not any(rand[0] == exist[0] and abs(rand[1] - exist[1]) < 0.001 for exist in unice):
            unice.append(rand)

    if len(unice) < 2:
        return None

    consum = unice[-1][1] - unice[-2][1]
    return round(consum, 3) if consum > 0 else None


def _consum_unitate_ultima_factura(
    instantaneu: InstantaneuFurnizor,
    cont=None,
) -> float | None:
    """Returnează consumul ultimei facturi, în unitatea contului: kWh sau m³."""
    id_cont = getattr(cont, "id_cont", None) if cont is not None else None
    unitate = _unitate_cost_ultima_factura(instantaneu, cont)

    for cheie in (
        "consum_unitate_ultima_factura",
        "consum_ultima_factura",
        "consum_kwh_ultima_factura",
        "ultim_consum",
    ):
        val_explicit = _to_float_safe(_valoare_consum(instantaneu, cheie, id_cont))
        if val_explicit is not None and val_explicit > 0:
            return round(val_explicit, 3)

    factura = _factura_ultima_consum_model(instantaneu, id_cont)
    if factura is not None:
        consum = _extrage_consum_unitate_din_date_brute(getattr(factura, "date_brute", None), unitate)
        if consum is not None and consum > 0:
            return round(consum, 3)

    consum_din_index = _consum_din_diferenta_indici(cont, unitate)
    if consum_din_index is not None and consum_din_index > 0:
        return round(consum_din_index, 3)

    # Fallback pentru furnizorii care expun deja consumul lunar curent ca valoare
    # de consum în aceeași unitate a serviciului. Nu folosim indexul curent.
    for cheie in ("consum_lunar_curent", "consum_lunar"):
        consum_lunar = _to_float_safe(_valoare_consum(instantaneu, cheie, id_cont))
        if consum_lunar is not None and consum_lunar > 0:
            return round(consum_lunar, 3)

    return None


def _cost_mediu_unitate_ultima_factura(
    instantaneu: InstantaneuFurnizor,
    cont=None,
) -> float | None:
    """Calculează costul mediu facturat per unitate consumată.

    Pentru energie electrică rezultatul este RON/kWh, iar pentru gaz/apă este
    RON/m³. Nu folosim sold total, restanțe cumulate sau total neachitat.
    """
    id_cont = getattr(cont, "id_cont", None) if cont is not None else None
    unitate = _unitate_cost_ultima_factura(instantaneu, cont)
    if not unitate:
        return None

    explicit = _valoare_consum(instantaneu, "cost_mediu_unitate_ultima_factura", id_cont)
    val_explicit = _to_float_safe(explicit)
    if val_explicit is not None and val_explicit > 0:
        return round(val_explicit, 4)

    factura = _factura_ultima_consum_model(instantaneu, id_cont)
    if factura is None:
        return None

    valoare = _to_float_safe(getattr(factura, "valoare", None))
    consum_unitate = _consum_unitate_ultima_factura(instantaneu, cont)

    if valoare is None or valoare <= 0 or consum_unitate is None or consum_unitate <= 0:
        return None

    return round(valoare / consum_unitate, 4)


def _rest_factura_model(factura: FacturaUtilitate | None) -> float | None:
    if factura is None:
        return None
    raw = getattr(factura, "date_brute", None)
    if isinstance(raw, dict):
        for cheie in ("rest_plata", "amountToPay", "amountRemaining", "remaining", "remainingValue", "restToPay", "rest"):
            try:
                valoare = raw.get(cheie)
                if valoare not in (None, ""):
                    return float(valoare)
            except (TypeError, ValueError):
                continue
    try:
        return float(getattr(factura, "valoare", None))
    except (TypeError, ValueError):
        return None


def _este_factura_activa_model(factura: FacturaUtilitate | None) -> bool:
    if factura is None:
        return False
    rest = _rest_factura_model(factura)
    if rest is not None:
        return rest > 0
    stare = str(getattr(factura, "stare", None) or "").strip().lower()
    if any(text in stare for text in ("platita", "paid", "reversed", "storno", "cancel")):
        return False
    try:
        return float(getattr(factura, "valoare", None) or 0) > 0
    except (TypeError, ValueError):
        return False


def _este_factura_pozitiva_model(factura: FacturaUtilitate | None) -> bool:
    if factura is None:
        return False
    stare = str(getattr(factura, "stare", None) or "").strip().lower()
    if any(text in stare for text in ("reversed", "storno", "cancel")):
        return False
    try:
        if float(getattr(factura, "valoare", None) or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    rest = _rest_factura_model(factura)
    return rest is not None and rest > 0


def _factura_reprezentativa_model(facturi: list[FacturaUtilitate]) -> FacturaUtilitate | None:
    if not facturi:
        return None
    active = [f for f in facturi if _este_factura_activa_model(f)]
    if active:
        return sorted(active, key=lambda f: (f.data_scadenta or date.max, f.data_emitere or date.min))[0]
    pozitive = [f for f in facturi if _este_factura_pozitiva_model(f)]
    if pozitive:
        return sorted(pozitive, key=lambda f: f.data_emitere or date.min, reverse=True)[0]
    return sorted(facturi, key=lambda f: f.data_emitere or date.min, reverse=True)[0]


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
    if instantaneu.furnizor == "nova":
        return _factura_reprezentativa_model(facturi)
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
        v = _id_factura_afisabil(_valoare_consum(instantaneu, "id_ultima_factura", id_cont))
        if v is not None:
            return v
    factura = _ultima_factura(instantaneu, categorie, id_cont)
    return _id_factura_afisabil(factura.id_factura) if factura else None


def _tipuri_active_cont(cont) -> list[str]:
    raw = _date_brute_cont(cont)
    tipuri = raw.get("tipuri_servicii_active")
    if isinstance(tipuri, list):
        return sorted({str(tip).strip() for tip in tipuri if str(tip).strip()})
    tip = str(getattr(cont, "tip_serviciu", None) or getattr(cont, "tip_utilitate", None) or "").strip()
    return [tip] if tip else []




def _tip_serviciu_punct_nova(punct: dict[str, Any]) -> str | None:
    """Normalizează tipul utilității pentru un punct de consum Nova."""
    text = normalize_text(
        punct.get("utilityType")
        or punct.get("utility")
        or punct.get("serviceType")
        or punct.get("commodity")
        or punct.get("type")
        or ""
    ).lower()
    if text in {"gas", "gaz", "natural gas", "gaze"}:
        return "gaz"
    if text in {"electricity", "electric", "energie", "energie electrica", "curent", "power"}:
        return "curent"
    return text or None


def _puncte_consum_nova(instantaneu: InstantaneuFurnizor) -> list[dict[str, Any]]:
    """Returnează punctele de consum Nova, deduplicate pe identificator stabil."""
    puncte: list[dict[str, Any]] = []
    vazute: set[str] = set()
    for cont in instantaneu.conturi or []:
        raw = _date_brute_cont(cont)
        for punct in raw.get("metering_points", []) or []:
            if not isinstance(punct, dict):
                continue
            ident = str(
                punct.get("meteringPointId")
                or punct.get("number")
                or punct.get("specificIdForUtilityType")
                or punct.get("contractId")
                or ""
            ).strip()
            if not ident:
                ident = str(punct)
            if ident in vazute:
                continue
            vazute.add(ident)
            puncte.append(punct)
    return puncte

def _tipuri_servicii_rezumat(instantaneu: InstantaneuFurnizor) -> list[str]:
    if instantaneu.furnizor == "nova":
        tipuri_puncte = {
            tip
            for punct in _puncte_consum_nova(instantaneu)
            if (tip := _tip_serviciu_punct_nova(punct))
        }
        if tipuri_puncte:
            return sorted(tipuri_puncte)

    tipuri: set[str] = set()
    for cont in instantaneu.conturi or []:
        tipuri.update(_tipuri_active_cont(cont))
    return sorted(tipuri)


def _numar_conturi_rezumat(instantaneu: InstantaneuFurnizor) -> int:
    if instantaneu.furnizor == "digi":
        extra = getattr(instantaneu, "extra", None) or {}
        try:
            return int(extra.get("addresses_count") or 0)
        except Exception:
            pass

    if instantaneu.furnizor == "nova":
        puncte = _puncte_consum_nova(instantaneu)
        if puncte:
            return len(puncte)

    return len(instantaneu.conturi or [])


def _numara_conturi_cu_serviciu(instantaneu: InstantaneuFurnizor, tip: str) -> int:
    if instantaneu.furnizor == "nova":
        puncte = _puncte_consum_nova(instantaneu)
        if puncte:
            return sum(1 for punct in puncte if _tip_serviciu_punct_nova(punct) == tip)

    return sum(1 for cont in instantaneu.conturi or [] if tip in _tipuri_active_cont(cont))


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

    facturi_active = [
        f
        for f in list(instantaneu.facturi or [])
        if _este_factura_activa_model(f) and getattr(f, "data_scadenta", None)
    ]
    if not facturi_active:
        return None

    azi = date.today()
    scadente_viitoare = [f.data_scadenta for f in facturi_active if f.data_scadenta >= azi]
    if scadente_viitoare:
        return min(scadente_viitoare).isoformat()

    scadente_depasite = [f.data_scadenta for f in facturi_active if f.data_scadenta < azi]
    return max(scadente_depasite).isoformat() if scadente_depasite else None


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
    DescriereSenzorRezumat(key="numar_conturi", name="Număr conturi", icon="mdi:folder-account", functie_valoare=lambda i: _numar_conturi_rezumat(i)),
    DescriereSenzorRezumat(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple", functie_valoare=lambda i: len(i.facturi)),
    DescriereSenzorRezumat(key="tipuri_servicii", name="Tipuri servicii", icon="mdi:shape-outline", functie_valoare=lambda i: ", ".join(_tipuri_servicii_rezumat(i)) or None),
    DescriereSenzorRezumat(key="numar_conturi_curent", name="Număr conturi curent", icon="mdi:lightning-bolt", functie_valoare=lambda i: _numara_conturi_cu_serviciu(i, "curent")),
    DescriereSenzorRezumat(key="numar_conturi_gaz", name="Număr conturi gaz", icon="mdi:fire-circle", functie_valoare=lambda i: _numara_conturi_cu_serviciu(i, "gaz")),
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
    DescriereSenzorCont(key="cost_mediu_unitate_ultima_factura", name="Cost mediu unitate ultima factură", icon="mdi:cash-check", native_unit_of_measurement="RON/unitate", functie_valoare=lambda i, c: _cost_mediu_unitate_ultima_factura(i, c)),
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
    DescriereSenzorCont(key="cost_mediu_unitate_ultima_factura", name="Cost mediu unitate ultima factură", icon="mdi:cash-check", native_unit_of_measurement="RON/unitate", functie_valoare=lambda i, c: _cost_mediu_unitate_ultima_factura(i, c)),
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


SENZORI_CONT_NOVA: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "de_plata", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_curent", c.id_cont)),
    DescriereSenzorCont(key="sold_prosumator", name="Sold prosumator", icon="mdi:solar-power-variant", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_prosumator", c.id_cont)),
    DescriereSenzorCont(key="este_prosumator", name="Este prosumator", icon="mdi:transmission-tower-export", functie_valoare=lambda i, c: _valoare_consum(i, "este_prosumator", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:receipt-text-check", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_ultima_factura(i, c.id_cont)),
    DescriereSenzorCont(key="cost_mediu_unitate_ultima_factura", name="Cost mediu unitate ultima factură", icon="mdi:cash-check", native_unit_of_measurement="RON/unitate", functie_valoare=lambda i, c: _cost_mediu_unitate_ultima_factura(i, c)),
    DescriereSenzorCont(key="pret_mediu_energie_prosumator_ultima_factura", name="Preț mediu energie prosumator ultima factură", icon="mdi:transmission-tower-export", native_unit_of_measurement="RON/kWh", functie_valoare=lambda i, c: _valoare_consum(i, "pret_mediu_energie_prosumator_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="id_ultima_factura", name="ID ultima factură", icon="mdi:file-document-outline", functie_valoare=lambda i, c: _id_ultima_factura(i, c.id_cont)),
    DescriereSenzorCont(key="urmatoarea_scadenta", name="Următoarea scadență", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="numar_plati", name="Număr plăți", icon="mdi:cash-check", functie_valoare=lambda i, c: _valoare_consum(i, "numar_plati", c.id_cont)),
)



SENZORI_CONT_APA_BRASOV: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "de_plata", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_curent", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:receipt-text-check", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="id_ultima_factura", name="ID ultima factură", icon="mdi:file-document-outline", functie_valoare=lambda i, c: _valoare_consum(i, "id_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="urmatoarea_scadenta", name="Următoarea scadență", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont)),
    DescriereSenzorCont(key="index_contor", name="Index contor", icon="mdi:counter", native_unit_of_measurement="m³", functie_valoare=lambda i, c: _valoare_consum(i, "index_contor", c.id_cont)),
    DescriereSenzorCont(key="ultim_consum", name="Ultimul consum", icon="mdi:water", native_unit_of_measurement="m³", functie_valoare=lambda i, c: _valoare_consum(i, "ultim_consum", c.id_cont)),
    DescriereSenzorCont(key="citire_index_permisa", name="Citire index permisă", icon="mdi:clock-check-outline", functie_valoare=lambda i, c: _valoare_consum(i, "citire_index_permisa", c.id_cont)),
    DescriereSenzorCont(key="perioada_citire", name="Perioadă citire index", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "perioada_citire", c.id_cont)),
    DescriereSenzorCont(key="zile_pana_citire_index", name="Zile până la citire index", icon="mdi:calendar-arrow-right", native_unit_of_measurement="zile", functie_valoare=lambda i, c: _valoare_consum(i, "zile_pana_citire_index", c.id_cont)),
    DescriereSenzorCont(key="numar_plati", name="Număr plăți", icon="mdi:cash-check", functie_valoare=lambda i, c: _valoare_consum(i, "numar_plati", c.id_cont)),
    DescriereSenzorCont(key="ultima_plata", name="Ultima plată", icon="mdi:cash-fast", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "ultima_plata", c.id_cont)),
)

SENZORI_CONT_APA_ORADEA: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "de_plata", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_curent", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:receipt-text-check", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="id_ultima_factura", name="ID ultima factură", icon="mdi:file-document-outline", functie_valoare=lambda i, c: _valoare_consum(i, "id_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="data_ultima_factura", name="Data ultimei facturi", icon="mdi:calendar-text", functie_valoare=lambda i, c: _valoare_consum(i, "data_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="urmatoarea_scadenta", name="Următoarea scadență", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi_neachitate", name="Număr facturi neachitate", icon="mdi:file-alert-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi_neachitate", c.id_cont)),
    DescriereSenzorCont(key="numar_plati", name="Număr plăți", icon="mdi:cash-check", functie_valoare=lambda i, c: _valoare_consum(i, "numar_plati", c.id_cont)),
    DescriereSenzorCont(key="data_ultima_plata", name="Data ultimei plăți", icon="mdi:calendar-check", functie_valoare=lambda i, c: _valoare_consum(i, "data_ultima_plata", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_plata", name="Valoare ultima plată", icon="mdi:cash-fast", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_plata", c.id_cont)),
    DescriereSenzorCont(key="numar_contoare", name="Număr contoare", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum(i, "numar_contoare", c.id_cont)),
    DescriereSenzorCont(key="index_contor", name="Index contor", icon="mdi:counter", native_unit_of_measurement="m³", functie_valoare=lambda i, c: _valoare_consum(i, "index_contor", c.id_cont)),
)


SENZORI_CONT_APA_GALATI: tuple[DescriereSenzorCont, ...] = SENZORI_CONT_APA_ORADEA
SENZORI_CONT_HIDRO_PRAHOVA: tuple[DescriereSenzorCont, ...] = SENZORI_CONT_APA_ORADEA + (
    DescriereSenzorCont(key="citire_index_permisa", name="Citire index permisă", icon="mdi:clock-check-outline", functie_valoare=lambda i, c: _valoare_consum(i, "citire_index_permisa", c.id_cont)),
    DescriereSenzorCont(key="perioada_citire", name="Perioadă citire index", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "perioada_citire", c.id_cont)),
    DescriereSenzorCont(key="zile_pana_citire_index", name="Zile până la citire index", icon="mdi:calendar-arrow-right", native_unit_of_measurement="zile", functie_valoare=lambda i, c: _valoare_consum(i, "zile_pana_citire_index", c.id_cont)),
    DescriereSenzorCont(key="data_urmatoare_citire_index", name="Data următoarei citiri index", icon="mdi:calendar-arrow-right", functie_valoare=lambda i, c: _valoare_consum(i, "data_urmatoare_citire_index", c.id_cont)),
)


SENZORI_CONT_COMPREST: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "de_plata", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: round(float(_valoare_consum(i, "sold_curent", c.id_cont) or 0.0), 2)),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:receipt-text-check", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="id_ultima_factura", name="ID ultima factură", icon="mdi:file-document-outline", functie_valoare=lambda i, c: _valoare_consum(i, "id_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="urmatoarea_scadenta", name="Următoarea scadență", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont)),
    DescriereSenzorCont(key="data_ultima_factura", name="Data ultimei facturi", icon="mdi:calendar-text", functie_valoare=lambda i, c: _valoare_consum(i, "data_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:alert-circle", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi_neachitate", name="Număr facturi neachitate", icon="mdi:file-alert-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi_neachitate", c.id_cont)),
    DescriereSenzorCont(key="numar_plati", name="Număr plăți", icon="mdi:cash-check", functie_valoare=lambda i, c: _valoare_consum(i, "numar_plati", c.id_cont)),
    DescriereSenzorCont(key="data_ultima_plata", name="Data ultimei plăți", icon="mdi:calendar-check", functie_valoare=lambda i, c: _valoare_consum(i, "data_ultima_plata", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_plata", name="Valoare ultima plată", icon="mdi:cash-fast", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_plata", c.id_cont)),
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


SENZORI_CONT_ENGIE: tuple[DescriereSenzorCont, ...] = (
    DescriereSenzorCont(key="date_client", name="Date client", icon="mdi:account-circle", functie_valoare=lambda i, c: c.nume),
    DescriereSenzorCont(key="date_contract", name="Date contract", icon="mdi:file-document-outline", functie_valoare=lambda i, c: c.stare),
    DescriereSenzorCont(key="sold_curent", name="Sold curent", icon="mdi:cash", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "sold_curent", c.id_cont)),
    DescriereSenzorCont(key="de_plata", name="De plată", icon="mdi:cash-clock", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "de_plata", c.id_cont)),
    DescriereSenzorCont(key="factura_restanta", name="Factură restantă", icon="mdi:file-document-alert", functie_valoare=lambda i, c: _valoare_consum(i, "factura_restanta", c.id_cont)),
    DescriereSenzorCont(key="numar_facturi", name="Număr facturi", icon="mdi:file-document-multiple-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="arhiva_facturi", name="Arhivă facturi", icon="mdi:archive-outline", functie_valoare=lambda i, c: _valoare_consum(i, "numar_facturi", c.id_cont)),
    DescriereSenzorCont(key="valoare_ultima_factura", name="Valoare ultima factură", icon="mdi:receipt-text", native_unit_of_measurement="RON", functie_valoare=lambda i, c: _valoare_consum(i, "valoare_ultima_factura", c.id_cont)),
    DescriereSenzorCont(key="urmatoarea_scadenta", name="Următoarea scadență", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "urmatoarea_scadenta", c.id_cont)),
    DescriereSenzorCont(key="index_contor", name="Index contor", icon="mdi:counter", functie_valoare=lambda i, c: _valoare_consum(i, "index_contor", c.id_cont)),
    DescriereSenzorCont(key="citire_permisa", name="Citire permisă", icon="mdi:clock-check-outline", functie_valoare=lambda i, c: _valoare_consum(i, "citire_permisa", c.id_cont)),
    DescriereSenzorCont(key="perioada_citire", name="Perioadă citire index", icon="mdi:calendar-clock", functie_valoare=lambda i, c: _valoare_consum(i, "perioada_citire", c.id_cont)),
    DescriereSenzorCont(key="zile_pana_citire_index", name="Zile până la citire index", icon="mdi:calendar-arrow-right", functie_valoare=lambda i, c: _valoare_consum(i, "zile_pana_citire_index", c.id_cont)),
    DescriereSenzorCont(key="consum_lunar", name="Consum lunar", icon="mdi:chart-line", functie_valoare=lambda i, c: _valoare_consum(i, "consum_lunar", c.id_cont)),
    DescriereSenzorCont(key="revizie_tehnica", name="Revizie tehnică", icon="mdi:wrench-clock", functie_valoare=lambda i, c: _valoare_consum(i, "revizie_tehnica", c.id_cont)),
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


    elif instantaneu and instantaneu.furnizor == "engie":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_ENGIE:
                tip_serviciu = str(getattr(cont, "tip_serviciu", "") or "").lower()
                tip_utilitate = str(getattr(cont, "tip_utilitate", "") or "").lower()
                if descriere.key == "revizie_tehnica" and tip_serviciu != "gaz":
                    continue
                entitati.append(SenzorContEngie(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "nova":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_NOVA:
                if descriere.key in {"sold_prosumator", "pret_mediu_energie_prosumator_ultima_factura"} and not _cont_este_prosumator(cont):
                    continue
                entitati.append(SenzorContNova(coordonator, cont, descriere))

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

    elif instantaneu and instantaneu.furnizor == "apa_brasov":
        # Apă Brașov poate fi configurată cu toate locațiile din cont. Păstrăm
        # un dispozitiv principal pentru furnizor, cu senzori de rezumat, la fel
        # ca la ceilalți furnizori multi-cont, iar fiecare loc de consum rămâne
        # pe propriul dispozitiv.
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_APA_BRASOV:
                entitati.append(SenzorContApaBrasov(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "apa_oradea":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_APA_ORADEA:
                entitati.append(SenzorContApaOradea(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "apa_galati":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_APA_GALATI:
                entitati.append(SenzorContApaGalati(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "hidro_prahova":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_HIDRO_PRAHOVA:
                entitati.append(SenzorContHidroPrahova(coordonator, cont, descriere))

    elif instantaneu and instantaneu.furnizor == "comprest":
        entitati.extend(SenzorRezumat(coordonator, d) for d in (list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR)))
        for cont in instantaneu.conturi:
            for descriere in SENZORI_CONT_COMPREST:
                entitati.append(SenzorContComprest(coordonator, cont, descriere))

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
        _aplica_unitate_cost_mediu(self, cont)

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
        _aplica_unitate_cost_mediu(self, cont)

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
        _aplica_unitate_cost_mediu(self, cont)

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
        _aplica_unitate_cost_mediu(self, cont)
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
    raw = getattr(cont, "date_brute", None) or {}
    service_label = str(raw.get("service_label") or getattr(cont, "tip_serviciu", None) or "").strip()
    if service_label and service_label.lower() not in {"servicii digi", "servicii", "telecom"}:
        nume_device = f"Digi - {nume} - {service_label}"
    else:
        nume_device = f"Digi - {nume}"
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_digi_{ident}")},
        name=nume_device,
        manufacturer="Digi România",
        model="Servicii",
    )


class SenzorContEngie(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        alias = alias_loc_engie(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_engie(cont.id_cont, alias, cont.adresa)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_engie_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"engie_{cont.id_cont}_{slug}_{descriere.key}"
        self.entity_id = f"sensor.engie_{cont.id_cont}_{slug}_{descriere.key}"
        self._attr_device_info = info_device_engie(coordonator.intrare.entry_id, cont)
        _aplica_unitate_cost_mediu(self, cont)
        if descriere.key == "index_contor":
            tip = str(cont.tip_serviciu or cont.tip_utilitate or "").lower()
            self._attr_native_unit_of_measurement = "m³" if tip == "gaz" else "kWh"

    @property
    def _cont_actual(self):
        return _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) or self.cont

    @property
    def available(self):
        return self.coordinator.data is not None and _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) is not None

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.functie_valoare(self.coordinator.data, self._cont_actual)

    @property
    def extra_state_attributes(self):
        cont = self._cont_actual
        raw = getattr(cont, "date_brute", None) or {}
        attrs = {
            "poc": raw.get("poc"),
            "pa": raw.get("pa"),
            "pod": raw.get("pod"),
            "contract_account": raw.get("contract_account"),
            "installation_number": raw.get("installation_number"),
            "division": raw.get("division"),
            "adresa": cont.adresa,
            "tip_serviciu": cont.tip_serviciu,
        }
        key = self.entity_description.key
        if key == "date_client":
            profil = raw.get("profil") or {}
            for cheie in ("name", "firstName", "lastName", "email", "phone", "mobile", "clientId"):
                if isinstance(profil, dict) and profil.get(cheie) not in (None, ""):
                    attrs[cheie] = profil.get(cheie)
        elif key == "date_contract":
            divizie = raw.get("divizie") or {}
            loc = raw.get("loc") or {}
            for sursa in (loc, divizie):
                if isinstance(sursa, dict):
                    for cheie in ("contract_account", "contractAccount", "contract_number", "status", "account_class"):
                        if sursa.get(cheie) not in (None, ""):
                            attrs[cheie] = sursa.get(cheie)
        elif key in {"numar_facturi", "arhiva_facturi", "factura_restanta", "valoare_ultima_factura", "urmatoarea_scadenta"}:
            facturi = raw.get("facturi") or []
            attrs["numar_facturi"] = len(facturi)
            attrs["ultima_factura"] = facturi[0] if facturi else None
            if key == "arhiva_facturi":
                attrs["ultimele_10_facturi"] = list(facturi[:10])
        elif key in {"index_contor", "citire_permisa", "perioada_citire", "zile_pana_citire_index"}:
            attrs["serie_contor"] = raw.get("serie_contor")
            attrs["date_index"] = raw.get("index")
            attrs["perioada_citire"] = _valoare_consum(self.coordinator.data, "perioada_citire", cont.id_cont)
            attrs["zile_pana_citire_index"] = _valoare_consum(self.coordinator.data, "zile_pana_citire_index", cont.id_cont)
        elif key == "consum_lunar":
            consum = raw.get("consum_lunar") or []
            attrs["numar_inregistrari"] = len(consum)
            attrs["ultimele_12_luni"] = list(consum[:12])
        elif key == "revizie_tehnica":
            revizie = raw.get("revizie") or {}
            if isinstance(revizie, dict):
                for cheie in (
                    "last_icu_revision_date",
                    "last_icu_verify_date",
                    "last_revision_date",
                    "last_verify_date",
                    "next_icu_inspection_date",
                    "next_inspection_date",
                    "next_inspection_is_overdue",
                    "next_icu_inspection_is_overdue",
                    "next_inspection_type",
                    "next_icu_inspection_type",
                ):
                    if revizie.get(cheie) not in (None, ""):
                        attrs[cheie] = revizie.get(cheie)
        return attrs


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

        id_cont = str(getattr(cont, "id_cont", None) or slug_strada or "cont").strip()
        id_cont_slug = build_provider_slug("digi", id_cont, id_cont)
        object_slug = f"{slug_strada}_{id_cont_slug}" if slug_strada not in id_cont_slug else id_cont_slug

        self._attr_unique_id = (
            f"{coordonator.intrare.entry_id}_digi_{id_cont}_{descriere.key}"
        )
        self._attr_name = descriere.name.strip()
        self._attr_suggested_object_id = f"digi_{object_slug}_{descriere.key}"
        self.entity_id = f"sensor.digi_{object_slug}_{descriere.key}"
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


def _tip_nova(cont) -> str:
    tipuri = _tipuri_active_cont(cont)
    if "curent" in tipuri and "gaz" in tipuri:
        return "mixt"
    if "gaz" in tipuri:
        return "gaz"
    if "curent" in tipuri:
        return "curent"
    tip = str(getattr(cont, "tip_serviciu", None) or getattr(cont, "tip_utilitate", None) or "").lower()
    return tip or "cont"


def _slug_loc_nova(cont) -> str:
    tip = _tip_nova(cont).replace("mixt", "energie_electrica_gaz")
    return f"{build_provider_slug('nova', getattr(cont, 'adresa', None), getattr(cont, 'id_cont', None))}_{tip}"


def info_device_nova(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "nova")
    tip = _tip_nova(cont)
    if tip == "mixt":
        tip_afisat = "Energie electrică și gaz"
    elif tip == "gaz":
        tip_afisat = "Gaz"
    elif tip == "curent":
        tip_afisat = "Energie electrică"
    else:
        tip_afisat = "Cont"
    adresa = getattr(cont, "adresa", None)
    nume = adresa or getattr(cont, "nume", None) or ident
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_nova_{ident}")},
        name=f"Nova - {tip_afisat} - {nume}",
        manufacturer="Nova Power & Gas",
        model=tip_afisat,
    )


class SenzorContNova(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        slug = _slug_loc_nova(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_nova_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_nova(coordonator.intrare.entry_id, cont)
        _aplica_unitate_cost_mediu(self, cont)

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
            "id_contract": cont.id_contract,
            "nume_cont": cont.nume,
            "tip_serviciu": cont.tip_serviciu,
            "tip_utilitate": cont.tip_utilitate,
            "tipuri_servicii_active": _tipuri_active_cont(cont),
            "adresa": cont.adresa,
            "este_prosumator": cont.este_prosumator,
            "nova_account_id": raw.get("nova_account_id"),
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
                "remaining": factura.date_brute.get("rest_plata") if isinstance(factura.date_brute, dict) else None,
            })

        if self.entity_description.key == "pret_mediu_energie_prosumator_ultima_factura" and self.coordinator.data is not None:
            consum = _consum_dupa_cheie(
                self.coordinator.data,
                "pret_mediu_energie_prosumator_ultima_factura",
                cont.id_cont,
            )
            detalii = getattr(consum, "date_brute", None) if consum is not None else None
            if isinstance(detalii, dict):
                for cheie in (
                    "energie_livrata_prosumator_kwh",
                    "energie_consumata_retea_kwh",
                    "energie_compensata_prosumator_kwh",
                    "energie_reportata_prosumator_kwh",
                    "energie_returnata_prosumator_kwh",
                    "valoare_energie_prosumator_ultima_factura",
                    "cantitate_energie_prosumator_kwh",
                    "sursa_prosumator",
                ):
                    if detalii.get(cheie) not in (None, ""):
                        attrs[cheie] = detalii.get(cheie)
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




def _nume_loc_apa_brasov(cont) -> str:
    return nume_scurt_locatie_apa_brasov(
        getattr(cont, "adresa", None) or getattr(cont, "nume", None),
        getattr(cont, "id_cont", None),
    )


def _slug_loc_apa_brasov(cont) -> str:
    return build_provider_slug("apa_brasov", _nume_loc_apa_brasov(cont), getattr(cont, "id_cont", None))


def info_device_apa_brasov(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "apa_brasov")
    nume = _nume_loc_apa_brasov(cont)
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_apa_brasov_{ident}")},
        name=f"Apă Brașov - {nume}",
        manufacturer="Compania Apa Brașov",
        model="Apă și canal",
    )


class SenzorContApaBrasov(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        slug = _slug_loc_apa_brasov(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_apa_brasov_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_apa_brasov(coordonator.intrare.entry_id, cont)
        _aplica_unitate_cost_mediu(self, cont)

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
        raw = _date_brute_cont(cont)
        attrs = {
            "id_cont": cont.id_cont,
            "id_contract": cont.id_contract,
            "nume_cont": cont.nume,
            "adresa": cont.adresa,
            "tip_serviciu": cont.tip_serviciu,
            "tip_utilitate": cont.tip_utilitate,
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

        if self.entity_description.key in {"numar_facturi", "valoare_ultima_factura", "id_ultima_factura"}:
            facturi = raw.get("invoices") or []
            attrs["facturi"] = facturi[-12:] if isinstance(facturi, list) else []
        elif self.entity_description.key in {"numar_plati", "ultima_plata"}:
            plati = raw.get("payments") or []
            attrs["plati"] = plati[-12:] if isinstance(plati, list) else []
        elif self.entity_description.key in {"index_contor", "citire_index_permisa", "perioada_citire", "zile_pana_citire_index"}:
            attrs["last_meter_reading"] = raw.get("last_meter_reading")
        elif self.entity_description.key == "ultim_consum":
            attrs["last_consumption"] = raw.get("last_consumption")

        return attrs


def _raw_get_suffix(raw: dict[str, Any], *keys: str):
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    wanted = {re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") for key in keys}
    for raw_key, value in raw.items():
        if value in (None, ""):
            continue
        raw_norm = re.sub(r"[^a-z0-9]+", "_", str(raw_key).lower()).strip("_")
        if raw_norm in wanted or any(raw_norm.endswith(f"_{key}") for key in wanted):
            return value
    return None


def _nume_loc_apa_oradea(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    nr_contract = _raw_get_suffix(raw, "numar_contract", "nr_contract", "contract")
    adresa = getattr(cont, "adresa", None) or _raw_get_suffix(raw, "adresa", "address", "loc_consum", "punct_consum")
    baza = adresa or getattr(cont, "nume", None) or "Loc consum"
    detalii = nr_contract or getattr(cont, "id_cont", None)
    text = f"{baza} ({detalii})" if detalii and str(detalii) not in str(baza) else str(baza)
    return re.sub(r"\s+", " ", text).strip() or str(getattr(cont, "id_cont", None) or "loc_consum")


def _slug_loc_apa_oradea(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    nr_contract = _raw_get_suffix(raw, "numar_contract", "nr_contract", "contract")
    baza = getattr(cont, "adresa", None) or _raw_get_suffix(raw, "adresa", "address", "loc_consum", "punct_consum") or getattr(cont, "nume", None) or nr_contract
    return build_provider_slug("apa_oradea", baza, getattr(cont, "id_cont", None))


def info_device_apa_oradea(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "apa_oradea")
    nume = _nume_loc_apa_oradea(cont)
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_apa_oradea_{ident}")},
        name=f"Apă Oradea - {nume}",
        manufacturer="Compania de Apă Oradea",
        model="Apă / canal",
    )


class SenzorContApaOradea(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        slug = _slug_loc_apa_oradea(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_apa_oradea_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_apa_oradea(coordonator.intrare.entry_id, cont)
        _aplica_unitate_cost_mediu(self, cont)

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
            "id_contract": cont.id_contract,
            "nume_cont": cont.nume,
            "adresa": cont.adresa,
            "tip_serviciu": cont.tip_serviciu,
            "tip_utilitate": cont.tip_utilitate,
        }
        raw = _date_brute_cont(cont)
        for cheie in ("id_client", "cod_beneficiar", "referinta_interna", "numar_contract", "numar_contoare"):
            valoare = raw.get(cheie)
            if valoare not in (None, ""):
                attrs[cheie] = valoare
        if self.coordinator.data is None:
            return attrs
        facturi = [f for f in (self.coordinator.data.facturi or []) if f.id_cont == cont.id_cont]
        if facturi:
            ultima = facturi[0]
            attrs.update({
                "ultima_factura_id": ultima.id_factura,
                "ultima_factura_titlu": ultima.titlu,
                "ultima_factura_emitere": ultima.data_emitere.isoformat() if ultima.data_emitere else None,
                "ultima_factura_scadenta": ultima.data_scadenta.isoformat() if ultima.data_scadenta else None,
                "ultima_factura_status": ultima.stare,
                "ultima_factura_valoare": ultima.valoare,
                "numar_facturi": len(facturi),
            })
        if self.entity_description.key in {"numar_facturi", "numar_facturi_neachitate", "valoare_ultima_factura", "id_ultima_factura"}:
            attrs["facturi"] = [
                {
                    "id": f.id_factura,
                    "titlu": f.titlu,
                    "valoare": f.valoare,
                    "emitere": f.data_emitere.isoformat() if f.data_emitere else None,
                    "scadenta": f.data_scadenta.isoformat() if f.data_scadenta else None,
                    "stare": f.stare,
                }
                for f in facturi[:12]
            ]
        return attrs


def _nume_loc_apa_galati(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    nr_contract = _raw_get_suffix(raw, "nr_contract", "numar_contract", "contract")
    cod_locatie = getattr(cont, "id_cont", None)
    adresa = getattr(cont, "adresa", None) or _raw_get_suffix(raw, "adresa_factura", "adresa", "loc_consum", "punct_consum")
    baza = adresa or getattr(cont, "nume", None) or "Loc consum"
    detalii = nr_contract or cod_locatie
    text = f"{baza} ({detalii})" if detalii and str(detalii) not in str(baza) else str(baza)
    return re.sub(r"\s+", " ", text).strip() or str(cod_locatie or "loc_consum")


def _slug_loc_apa_galati(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    baza = getattr(cont, "adresa", None) or _raw_get_suffix(raw, "adresa_factura", "adresa", "den_locatie") or getattr(cont, "nume", None)
    return build_provider_slug("apa_galati", baza, getattr(cont, "id_cont", None))


def info_device_apa_galati(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "apa_galati")
    nume = _nume_loc_apa_galati(cont)
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_apa_galati_{ident}")},
        name=f"Apă Canal Galați - {nume}",
        manufacturer="Apă Canal S.A. Galați",
        model="Apă / canal",
    )


class SenzorContApaGalati(SenzorContApaOradea):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        EntitateUtilitatiRomania.__init__(self, coordonator)
        self.cont = cont
        self.entity_description = descriere
        slug = _slug_loc_apa_galati(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_apa_galati_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_apa_galati(coordonator.intrare.entry_id, cont)
        _aplica_unitate_cost_mediu(self, cont)


def _nume_loc_hidro_prahova(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    cod_client = _raw_get_suffix(raw, "id_client", "cod_client", "client")
    baza = getattr(cont, "adresa", None) or getattr(cont, "nume", None) or "Loc consum"
    detalii = cod_client or getattr(cont, "id_cont", None)
    text = f"{baza} ({detalii})" if detalii and str(detalii) not in str(baza) else str(baza)
    return re.sub(r"\s+", " ", text).strip() or str(detalii or "loc_consum")


def _slug_loc_hidro_prahova(cont) -> str:
    baza = getattr(cont, "adresa", None) or getattr(cont, "nume", None)
    return build_provider_slug("hidro_prahova", baza, getattr(cont, "id_cont", None))


def info_device_hidro_prahova(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "hidro_prahova")
    nume = _nume_loc_hidro_prahova(cont)
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_hidro_prahova_{ident}")},
        name=f"Hidro Prahova - {nume}",
        manufacturer="Hidro Prahova S.A.",
        model="Apă / canal",
    )


class SenzorContHidroPrahova(SenzorContApaOradea):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        EntitateUtilitatiRomania.__init__(self, coordonator)
        self.cont = cont
        self.entity_description = descriere
        slug = _slug_loc_hidro_prahova(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_hidro_prahova_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_hidro_prahova(coordonator.intrare.entry_id, cont)
        _aplica_unitate_cost_mediu(self, cont)


def _nume_loc_comprest(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    nr_contract = _raw_get_suffix(raw, "numar_contract_afisat", "nr_ctr", "nr_ctr_data", "nr_contract", "contract")
    cod_client = _raw_get_suffix(raw, "cod_client_detectat", "cod_client", "client_code")
    adresa = getattr(cont, "adresa", None)
    if not adresa:
        adresa = _raw_get_suffix(raw, "adresa", "address", "loc_consum", "punct_consum", "locatie")
    nume = getattr(cont, "nume", None)
    baza = adresa or nume or "Loc consum"
    detalii = nr_contract or cod_client or getattr(cont, "id_cont", None)
    text = f"{baza} ({detalii})" if detalii and str(detalii) not in str(baza) else str(baza)
    text = re.sub(r"\s+", " ", text).strip()
    return text or str(getattr(cont, "id_cont", None) or "loc_consum")


def _slug_loc_comprest(cont) -> str:
    raw = getattr(cont, "date_brute", None) or {}
    nr_contract = _raw_get_suffix(raw, "numar_contract_afisat", "nr_ctr", "nr_ctr_data", "nr_contract", "contract")
    baza = getattr(cont, "adresa", None) or _raw_get_suffix(raw, "adresa", "address", "loc_consum", "punct_consum", "locatie") or getattr(cont, "nume", None) or nr_contract
    return build_provider_slug("comprest", baza, getattr(cont, "id_cont", None))


def info_device_comprest(entry_id: str, cont) -> DeviceInfo:
    ident = getattr(cont, "id_cont", "comprest")
    nume = _nume_loc_comprest(cont)
    return DeviceInfo(
        identifiers={(DOMENIU, f"{entry_id}_comprest_{ident}")},
        name=f"Comprest - {nume}",
        manufacturer="Comprest",
        model="Salubritate",
    )


class SenzorContComprest(EntitateUtilitatiRomania, SensorEntity):
    entity_description: DescriereSenzorCont

    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont, descriere: DescriereSenzorCont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        self.entity_description = descriere
        slug = _slug_loc_comprest(cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_comprest_{cont.id_cont}_{descriere.key}"
        self._attr_name = descriere.name
        self._attr_suggested_object_id = f"{slug}_{descriere.key}"
        self.entity_id = f"sensor.{slug}_{descriere.key}"
        self._attr_device_info = info_device_comprest(coordonator.intrare.entry_id, cont)

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
            "id_contract": cont.id_contract,
            "nume_cont": cont.nume,
            "adresa": cont.adresa,
            "tip_serviciu": cont.tip_serviciu,
            "tip_utilitate": cont.tip_utilitate,
        }

        raw = _date_brute_cont(cont)
        for cheie in ("cod_client", "client_code", "nr_ctr_data", "tip", "email", "telefon"):
            valoare = raw.get(cheie)
            if valoare not in (None, ""):
                attrs[cheie] = valoare

        if self.coordinator.data is None:
            return attrs

        facturi = [f for f in (self.coordinator.data.facturi or []) if f.id_cont == cont.id_cont]
        plati = [
            c.date_brute
            for c in (self.coordinator.data.consumuri or [])
            if getattr(c, "id_cont", None) == cont.id_cont
            and getattr(c, "cheie", None) in {"data_ultima_plata", "valoare_ultima_plata"}
            and isinstance(getattr(c, "date_brute", None), dict)
            and c.date_brute
        ]

        if facturi:
            ultima = facturi[0]
            attrs.update({
                "ultima_factura_id": ultima.id_factura,
                "ultima_factura_titlu": ultima.titlu,
                "ultima_factura_emitere": ultima.data_emitere.isoformat() if ultima.data_emitere else None,
                "ultima_factura_scadenta": ultima.data_scadenta.isoformat() if ultima.data_scadenta else None,
                "ultima_factura_status": ultima.stare,
                "ultima_factura_valoare": ultima.valoare,
                "numar_facturi": len(facturi),
            })

        if self.entity_description.key in {"numar_facturi", "numar_facturi_neachitate", "valoare_ultima_factura", "id_ultima_factura"}:
            attrs["facturi"] = [
                {
                    "id": f.id_factura,
                    "titlu": f.titlu,
                    "valoare": f.valoare,
                    "scadenta": f.data_scadenta.isoformat() if f.data_scadenta else None,
                    "stare": f.stare,
                }
                for f in facturi[:12]
            ]
        elif self.entity_description.key in {"numar_plati", "data_ultima_plata", "valoare_ultima_plata"}:
            attrs["plati"] = plati[:12]

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

