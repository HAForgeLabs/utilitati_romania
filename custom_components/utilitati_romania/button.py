from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components import persistent_notification

from .coordonator import CoordonatorUtilitatiRomania
from .entitate import EntitateUtilitatiRomania
from .const import DOMENIU, CONF_FURNIZOR, FURNIZOR_ADMIN_GLOBAL, SERVICIU_RELOAD_ALL
from .licentiere import (
    async_obtine_context_licenta,
    async_salveaza_licenta_globala,
    async_valideaza_licenta,
    normalizeaza_cheia_licenta,
)
from .hidro_device import alias_loc_consum, info_device_hidro, slug_loc_consum
from .eon_device import alias_loc_eon, cheie_serviciu_eon, id_unic_eon, info_device_eon, slug_serviciu_loc_eon, tip_serviciu_eon
from .furnizori.hidroelectrica_helper import build_usage_entity, safe_get
from .myelectrica_device import alias_loc_myelectrica, info_device_myelectrica, slug_loc_myelectrica
from .ebloc_device import alias_loc_ebloc, info_device_ebloc, slug_loc_ebloc
from .naming import build_provider_slug
from .furnizori.apa_brasov import nume_scurt_locatie_apa_brasov

from .storage_citiri import async_salveaza_citire

_LOGGER = logging.getLogger(__name__)


def _mascheaza_hidro(valoare, pastrat: int = 4) -> str:
    """Maschează identificatorii Hidroelectrica înainte de scrierea în log."""
    if valoare in (None, ""):
        return ""
    text = str(valoare).strip()
    if len(text) <= pastrat * 2:
        return "*" * len(text)
    return f"{text[:pastrat]}...{text[-pastrat:]}"


def _cont_curent_dupa_id(coordonator: CoordonatorUtilitatiRomania, id_cont: str | None):
    data = getattr(coordonator, "data", None)
    conturi = getattr(data, "conturi", None) or []
    for cont in conturi:
        if getattr(cont, "id_cont", None) == id_cont:
            return cont
    return None


def _citire_permisa_curenta(coordonator: CoordonatorUtilitatiRomania, id_cont: str) -> bool:
    data = getattr(coordonator, "data", None)
    consumuri = getattr(data, "consumuri", None) or []
    for consum in consumuri:
        if getattr(consum, "id_cont", None) != id_cont:
            continue
        if getattr(consum, "cheie", None) not in {"citire_permisa", "citire_index_permisa"}:
            continue
        valoare = getattr(consum, "valoare", None)
        if isinstance(valoare, str):
            return valoare.strip().lower() in {"da", "true", "1", "yes", "on"}
        return bool(valoare)
    return False


def _fereastra_apa_canal(coordonator: CoordonatorUtilitatiRomania, id_cont: str) -> dict:
    data = getattr(coordonator, "data", None)
    conturi = getattr(data, "conturi", None) or []
    for cont in conturi:
        if getattr(cont, "id_cont", None) != id_cont:
            continue
        raw = getattr(cont, "date_brute", None) or {}
        return raw.get("meter_reading_window") or {}
    return {}


def _primul_registru_apa_canal(coordonator: CoordonatorUtilitatiRomania, id_cont: str) -> dict:
    registre = (_fereastra_apa_canal(coordonator, id_cont).get("registers") or [])
    return registre[0] if registre else {}


def _admin_license_text_entity_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_admin_cod_licenta_noua"
    return registry.async_get_entity_id("text", DOMENIU, unique_id)


def _senzori_licenta_admin() -> list[str]:
    return [
        f"sensor.{DOMENIU}_status_licenta",
        f"sensor.{DOMENIU}_plan_licenta",
        f"sensor.{DOMENIU}_valabila_pana_la",
        f"sensor.{DOMENIU}_ultima_verificare_licenta",
        f"sensor.{DOMENIU}_cont_licenta",
        f"sensor.{DOMENIU}_cod_licenta_mascat",
        f"sensor.{DOMENIU}_mesaj_licenta",
    ]


def _filtreaza_entitati_existente(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    return [entity_id for entity_id in entity_ids if hass.states.get(entity_id) is not None]


async def _async_actualizeaza_senzorii_licentei(hass: HomeAssistant) -> None:
    entity_ids = _filtreaza_entitati_existente(hass, _senzori_licenta_admin())
    if not entity_ids:
        return

    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": entity_ids},
        blocking=False,
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
    if entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
        async_add_entities([ButonReloadToateSubintegrarile(entry), ButonAplicaLicenta(entry), ButonVerificaLicenta(entry)])
        return

    coordonator: CoordonatorUtilitatiRomania = hass.data[DOMENIU][entry.entry_id]
    entitati: list[ButtonEntity] = [ButonActualizareAcum(coordonator)]
    if coordonator.data and coordonator.data.furnizor == "hidroelectrica":
        for cont in coordonator.data.conturi:
            entitati.append(ButonTrimiteIndexHidro(coordonator, cont))
    elif coordonator.data and coordonator.data.furnizor == "eon":
        for cont in coordonator.data.conturi:
            entitati.append(ButonTrimiteIndexEon(coordonator, cont))
    elif coordonator.data and coordonator.data.furnizor == "myelectrica":
        for cont in coordonator.data.conturi:
            raw = getattr(cont, "date_brute", None) or {}
            meter = raw.get("meter_list") or {}
            contoare = meter.get("to_Contor", []) or []
            are_contor = bool(contoare and (contoare[0].get("SerieContor") or ((contoare[0].get("to_Cadran") or [{}])[0].get("RegisterCode"))))
            if are_contor:
                entitati.append(ButonTrimiteIndexMyElectrica(coordonator, cont))
    elif coordonator.data and coordonator.data.furnizor == "apa_canal":
        for cont in coordonator.data.conturi:
            entitati.append(ButonTrimiteIndexApaCanal(coordonator, cont))
    elif coordonator.data and coordonator.data.furnizor == "ebloc":
        entitati.append(ButonCurataSesiuniEbloc(coordonator))
        for cont in coordonator.data.conturi:
            entitati.append(ButonTrimiteNumarPersoaneEbloc(coordonator, cont))
    async_add_entities(entitati)


class ButonReloadToateSubintegrarile(ButtonEntity):
    _attr_icon = "mdi:reload-alert"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_admin_reload_all"
        self._attr_name = "Reload all subs"
        self._attr_device_info = _admin_device_info(entry)
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.hass.services.async_call(DOMENIU, SERVICIU_RELOAD_ALL, {}, blocking=True)


class ButonVerificaLicenta(ButtonEntity):
    _attr_icon = "mdi:shield-sync"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_admin_verifica_licenta"
        self._attr_name = "Verifică licență"
        self.entity_id = f"button.{DOMENIU}_verifica_licenta"
        self._attr_device_info = _admin_device_info(entry)
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        utilizator, cheie, _storage = await async_obtine_context_licenta(self.hass, intrare=self._entry)
        if not utilizator:
            raise HomeAssistantError("Nu există încă un cont de licență asociat. Configurează mai întâi cel puțin un furnizor.")
        if not cheie:
            raise HomeAssistantError("Nu există un cod de licență salvat pentru verificare.")

        rezultat = await async_valideaza_licenta(self.hass, cheie, utilizator)

        if rezultat.eroare_conectare:
            mesaj = rezultat.mesaj or "Serverul de licență nu a putut fi contactat."
            persistent_notification.async_create(
                self.hass,
                f"Verificarea licenței nu a putut fi finalizată.\n\nMotiv: **{mesaj}**",
                title="Utilități România – Licență",
                notification_id="utilitati_romania_verifica_licenta",
            )
            raise HomeAssistantError(mesaj)

        await async_salveaza_licenta_globala(self.hass, cheie, utilizator, rezultat)
        await _async_actualizeaza_senzorii_licentei(self.hass)

        if rezultat.valida:
            mesaj = (
                "Licența a fost verificată cu succes.\n\n"
                f"- Status: **{rezultat.status}**\n"
                f"- Plan: **{rezultat.plan or '-'}**\n"
                f"- Expiră la: **{rezultat.expira_la or '-'}**"
            )
        else:
            mesaj = (
                "Licența a fost verificată, dar nu mai este validă.\n\n"
                f"- Status: **{rezultat.status}**\n"
                f"- Motiv: **{rezultat.mesaj or '-'}**\n\n"
                "Furnizorii pot rămâne indisponibili până la activarea unei licențe valide."
            )

        persistent_notification.async_create(
            self.hass,
            mesaj,
            title="Utilități România – Licență",
            notification_id="utilitati_romania_verifica_licenta",
        )


class ButonAplicaLicenta(ButtonEntity):
    _attr_icon = "mdi:key-chain-variant"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_admin_aplica_licenta"
        self._attr_name = "Aplică licență"
        self._attr_device_info = _admin_device_info(entry)
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        text_entity_id = _admin_license_text_entity_id(self.hass, self._entry)
        if not text_entity_id:
            raise HomeAssistantError("Nu am găsit câmpul text pentru introducerea licenței.")

        stare = self.hass.states.get(text_entity_id)
        cod = normalizeaza_cheia_licenta(stare.state if stare else "")
        if not cod:
            raise HomeAssistantError("Introdu mai întâi un cod de licență nou.")

        utilizator, _cheie_curenta, _storage = await async_obtine_context_licenta(self.hass, intrare=self._entry)
        if not utilizator:
            raise HomeAssistantError("Nu există încă un cont de licență asociat. Configurează mai întâi cel puțin un furnizor.")

        notif_id = "utilitati_romania_aplica_licenta"
        rezultat = await async_valideaza_licenta(self.hass, cod, utilizator)

        if not rezultat.valida:
            mesaj = rezultat.mesaj or "Codul de licență nu a putut fi validat."
            persistent_notification.async_create(
                self.hass,
                f"Aplicarea licenței a eșuat.\n\nMotiv: **{mesaj}**",
                title="Utilități România – Licență",
                notification_id=notif_id,
            )
            raise HomeAssistantError(mesaj)

        await async_salveaza_licenta_globala(self.hass, cod, utilizator, rezultat)

        await self.hass.services.async_call(
            "text",
            "set_value",
            {"entity_id": text_entity_id, "value": cod},
            blocking=True,
        )

        await _async_actualizeaza_senzorii_licentei(self.hass)

        persistent_notification.async_create(
            self.hass,
            (
                "Licența a fost actualizată cu succes.\n\n"
                f"- Utilizator: **{utilizator}**\n"
                f"- Plan: **{rezultat.plan or '-'}**\n"
                f"- Expiră la: **{rezultat.expira_la or '-'}**\n\n"
                "Senzorii de licență au fost actualizați fără reîncărcarea automată a furnizorilor. "
                "Dacă un furnizor era deja blocat de licență, folosește manual butonul „Reload all subs” sau reîncarcă integrarea din Home Assistant."
            ),
            title="Utilități România – Licență",
            notification_id=notif_id,
        )


class ButonActualizareAcum(EntitateUtilitatiRomania, ButtonEntity):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania) -> None:
        super().__init__(coordonator)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_actualizare_acum"
        self._attr_name = "Actualizează acum"
        self._attr_icon = "mdi:refresh"


    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class ButonTrimiteIndexHidro(EntitateUtilitatiRomania, ButtonEntity):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        alias = alias_loc_consum(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_consum(cont.id_cont, alias, cont.adresa)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_hidro_{cont.id_cont}_trimite_index"
        self._attr_name = f"Trimite index energie electrică {alias}"
        self._attr_icon = "mdi:send-circle"
        self._attr_device_info = info_device_hidro(coordonator.intrare.entry_id, cont)
        self._attr_suggested_object_id = f"hidro_{cont.id_cont}_{slug}_trimite_index"
        self.entity_id = f"button.hidro_{cont.id_cont}_{slug}_trimite_index"
        self._entity_numar = f"number.hidro_{cont.id_cont}_{slug}_index_energie_electrica"

    @property
    def _cont_actual(self):
        return _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) or self.cont

    @property
    def available(self) -> bool:
        cont_existent = _cont_curent_dupa_id(self.coordinator, getattr(self.cont, "id_cont", None)) is not None
        return cont_existent and _citire_permisa_curenta(self.coordinator, self.cont.id_cont)

    async def async_press(self) -> None:
        if not _citire_permisa_curenta(self.coordinator, self.cont.id_cont):
            raise HomeAssistantError("Perioada de transmitere a indexului nu este activă pentru acest loc de consum.")

        numar = self.hass.states.get(self._entity_numar)
        if not numar:
            raise ValueError(f"Nu există entitatea {self._entity_numar}")
        index_value = str(int(float(numar.state)))

        meta = self._cont_actual.date_brute or {}
        previous_payload = meta.get("previous_meter_read") or {}
        prev_data = safe_get(previous_payload, "result", "Data", default=[])
        if not prev_data or not isinstance(prev_data, list):
            raise ValueError("Nu există date anterioare pentru transmiterea indexului.")
        now_str = datetime.now().strftime("%d/%m/%Y")
        usage_entities = [
            build_usage_entity(reading, index_value, now_str)
            for reading in prev_data
            if isinstance(reading, dict)
        ]
        api = self.coordinator.client.api
        user_id = api.user_id or ""
        pod = meta.get("pod") or ""
        instalare = meta.get("instalare") or ""
        account_number = meta.get("account_number") or ""
        contract_account_id = meta.get("contract_account_id") or ""

        _LOGGER.warning(
            "[HIDRO DEBUG] Trimite index: entity_id=%s, id_cont=%s, "
            "UtilityAccountNumber=%s, AccountNumber=%s, Pod=%s, Instalare=%s, "
            "previous_meter_read=%s, usage_entities=%s, index=%s.",
            self.entity_id,
            _mascheaza_hidro(getattr(self.cont, "id_cont", None)),
            _mascheaza_hidro(contract_account_id),
            _mascheaza_hidro(account_number),
            _mascheaza_hidro(pod),
            _mascheaza_hidro(instalare),
            len(prev_data),
            len(usage_entities),
            index_value,
        )

        await api.async_get_meter_value(
            user_id=user_id,
            pod_value=pod,
            installation_number=instalare,
            account_number=account_number,
            usage_entity=usage_entities,
        )
        await api.async_submit_self_meter_read(
            user_id=user_id,
            pod_value=pod,
            installation_number=instalare,
            account_number=account_number,
            usage_entity=usage_entities,
        )

        await async_salveaza_citire(
            self.hass,
            "hidroelectrica",
            self.cont.id_cont,
            float(index_value),
        )

        await self.coordinator.async_request_refresh()


class ButonTrimiteIndexEon(EntitateUtilitatiRomania, ButtonEntity):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        alias = alias_loc_eon(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_serviciu_loc_eon(cont)
        identificator = id_unic_eon(cont)
        tip = tip_serviciu_eon(cont)

        self._alias = alias
        self._tip = tip
        self._number_unique_id = f"{coordonator.intrare.entry_id}_eon_{identificator}_index"

        self._attr_unique_id = f"{coordonator.intrare.entry_id}_eon_{identificator}_trimite_index"
        self._attr_name = f"Trimite index {'gaz' if tip == 'gaz' else 'energie electrică'} {alias}"
        self._attr_suggested_object_id = f"{slug}_trimite_index"
        self.entity_id = f"button.{slug}_trimite_index"
        self._attr_icon = "mdi:send-circle"
        self._attr_device_info = info_device_eon(coordonator.intrare.entry_id, cont)

    async def async_press(self) -> None:
        tip_label = "gaz" if self._tip == "gaz" else "energie electrică"
        notif_id = f"utilitati_romania_eon_trimite_index_{self.cont.id_cont}"

        try:
            registru_entitati = er.async_get(self.hass)
            number_entity_id = registru_entitati.async_get_entity_id("number", DOMENIU, self._number_unique_id)
            numar = self.hass.states.get(number_entity_id) if number_entity_id else None

            if not numar:
                text_cautat = "index gaz" if self._tip == "gaz" else "index energie electrică"
                numar = next(
                    (
                        state
                        for state in self.hass.states.async_all("number")
                        if text_cautat in str(state.attributes.get("friendly_name", "")).lower()
                        and self._alias.lower() in str(state.attributes.get("friendly_name", "")).lower()
                    ),
                    None,
                )

            if not numar:
                raise ValueError(
                    f"Nu am găsit entitatea number pentru indexul de {tip_label} aferentă locației „{self._alias}”."
                )

            try:
                index_value = int(float(numar.state))
            except (TypeError, ValueError):
                raise ValueError(
                    f"Valoarea indexului introdusă pentru „{self._alias}” nu este validă: {numar.state}"
                )

            meta = self.cont.date_brute or {}
            meter_index = meta.get("meter_index") or {}
            devices = ((meter_index.get("indexDetails") or {}).get("devices") or [])

            ablbelnr = None
            for dev in devices:
                for idx in (dev.get("indexes") or []):
                    ablbelnr = idx.get("ablbelnr")
                    if ablbelnr:
                        break
                if ablbelnr:
                    break

            if not ablbelnr:
                raise ValueError(
                    f"Nu s-a putut identifica ID-ul intern al contorului (ablbelnr) pentru „{self._alias}”."
                )

            indexes_payload = [
                {
                    "ablbelnr": ablbelnr,
                    "indexValue": index_value,
                }
            ]

            rezultat = await self.coordinator.client.api.async_submit_meter_index(
                self.cont.id_cont,
                indexes_payload,
            )

            if rezultat is None:
                raise ValueError(
                    f"Transmiterea indexului de {tip_label} pentru „{self._alias}” a eșuat. "
                    "API-ul E.ON nu a returnat un răspuns valid."
                )

            if isinstance(rezultat, dict) and rezultat.get("success") is False:
                raise ValueError(f"E.ON a refuzat transmiterea indexului: {rezultat}")

            persistent_notification.async_create(
                self.hass,
                (
                    f"Indexul de **{tip_label}** pentru **{self._alias}** a fost confirmat de E.ON.\n\n"
                    f"- Contract: `{self.cont.id_cont}`\n"
                    f"- Valoare transmisă: **{index_value}**\n"
                    f"- ID contor intern: `{ablbelnr}`\n"
                    f"- Răspuns E.ON: `{rezultat}`"
                ),
                title="Utilități România – E.ON",
                notification_id=notif_id,
            )

            await self.coordinator.async_request_refresh()

        except Exception as err:
            persistent_notification.async_create(
                self.hass,
                (
                    f"Transmiterea indexului de **{tip_label}** pentru **{self._alias}** a eșuat.\n\n"
                    f"Motiv: **{err}**\n\n"
                    f"- Contract: `{self.cont.id_cont}`"
                ),
                title="Utilități România – E.ON",
                notification_id=notif_id,
            )
            raise


class ButonTrimiteIndexMyElectrica(EntitateUtilitatiRomania, ButtonEntity):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        alias = alias_loc_myelectrica(cont.nume, cont.adresa, cont.id_cont)
        slug = slug_loc_myelectrica(cont.id_cont, alias, cont.adresa)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_myelectrica_{slug}_trimite_index"
        self._attr_name = f"Trimite index {alias}"
        self._attr_icon = "mdi:send-circle"
        self._attr_device_info = info_device_myelectrica(coordonator.intrare.entry_id, cont)
        self._entity_numar = f"number.utilitati_romania_myelectrica_{slug}_index_contor"

    async def async_press(self) -> None:
        numar = self.hass.states.get(self._entity_numar)
        if not numar:
            raise ValueError(f"Nu există entitatea {self._entity_numar}")
        index_value = int(float(numar.state))
        raw = getattr(self.cont, "date_brute", None) or {}
        serie_contor = raw.get("serie_contor") or raw.get("meter_list", {}).get("to_Contor", [{}])[0].get("SerieContor")
        register_code = raw.get("register_code")
        if not register_code:
            contoare = raw.get("meter_list", {}).get("to_Contor", []) or []
            if contoare:
                cadrane = contoare[0].get("to_Cadran", []) or []
                if cadrane:
                    register_code = cadrane[0].get("RegisterCode")
        if not serie_contor or not register_code:
            raise ValueError("Nu s-au putut identifica seria contorului sau codul registrului pentru myElectrica.")
        rezultat = await self.coordinator.client.api.async_set_index(self.cont.id_cont, serie_contor, register_code, index_value)
        if not isinstance(rezultat, dict):
            raise ValueError("Transmiterea indexului myElectrica a eșuat.")
        errors = rezultat.get("errors") or []
        if errors:
            mesaj = "; ".join(str(item.get("errorMessage") or item) for item in errors)
            raise ValueError(mesaj)
        self.hass.components.persistent_notification.create(
            f"Indexul a fost transmis cu succes pentru {alias_loc_myelectrica(self.cont.nume, self.cont.adresa, self.cont.id_cont)}.",
            title="myElectrica",
        )
        await self.coordinator.async_request_refresh()


class ButonTrimiteIndexApaCanal(EntitateUtilitatiRomania, ButtonEntity):
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
        self._alias = alias
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_apa_canal_{cont.id_cont}_trimite_index"
        self._attr_name = f"Trimite index {alias}"
        self._attr_icon = "mdi:send-circle"
        self._attr_suggested_object_id = f"{slug}_trimite_index"
        self.entity_id = f"button.{slug}_trimite_index"
        self._entity_numar = f"number.{slug}_index_de_transmis"


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
        return _citire_permisa_curenta(self.coordinator, self.cont.id_cont) and bool(registru.get("device_id") and registru.get("register_id"))

    async def async_press(self) -> None:
        stare_numar = self.hass.states.get(self._entity_numar)
        if not stare_numar:
            raise HomeAssistantError(f"Nu există entitatea {self._entity_numar}.")

        try:
            index_value = int(float(stare_numar.state))
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Valoarea indexului introdus nu este validă.") from err

        registru = _primul_registru_apa_canal(self.coordinator, self.cont.id_cont)
        device_id = str(registru.get("device_id") or "").strip()
        register_id = str(registru.get("register_id") or "").strip()
        contract_id = str(getattr(self.cont, "id_contract", None) or "").strip()

        if not contract_id or not device_id or not register_id:
            raise HomeAssistantError(
                "Nu am putut identifica datele tehnice necesare pentru transmiterea indexului Apă Canal Sibiu."
            )

        rezultat = await self.coordinator.client.async_transmite_index(
            contract_id,
            device_id,
            register_id,
            index_value,
        )
        if not isinstance(rezultat, dict):
            raise HomeAssistantError("Transmiterea indexului Apă Canal Sibiu nu a returnat un răspuns valid.")

        await async_salveaza_citire(
            self.hass,
            "apa_canal",
            self.cont.id_cont,
            float(index_value),
            sursa="card",
            extra={
                "device_id": device_id,
                "register_id": register_id,
                "serie_contor": registru.get("serial_number"),
                "unitate": registru.get("unit"),
            },
        )

        persistent_notification.async_create(
            self.hass,
            f"Indexul **{index_value}** a fost transmis cu succes pentru **{self._alias}**.",
            title="Utilități România – Apă Canal Sibiu",
            notification_id=f"utilitati_romania_apa_canal_trimite_index_{self.cont.id_cont}",
        )
        await self.coordinator.async_request_refresh()


class ButonCurataSesiuniEbloc(EntitateUtilitatiRomania, ButtonEntity):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania) -> None:
        super().__init__(coordonator)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_ebloc_curata_sesiuni"
        self._attr_name = "Curăță sesiuni vechi"
        self._attr_icon = "mdi:account-cancel"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        curatare = getattr(self.coordinator.client, "async_curata_sesiuni_vechi", None)
        if not callable(curatare):
            raise HomeAssistantError("Clientul E-Bloc nu permite curățarea sesiunilor.")

        if getattr(self, "_curatare_in_derulare", False):
            persistent_notification.async_create(
                self.hass,
                "Curățarea sesiunilor E-Bloc este deja în derulare. Operațiunea poate dura câteva minute dacă portalul are multe sesiuni vechi.",
                title="Utilități România – E-Bloc",
                notification_id="utilitati_romania_ebloc_curata_sesiuni",
            )
            return

        self._curatare_in_derulare = True
        persistent_notification.async_create(
            self.hass,
            "Am pornit curățarea sesiunilor vechi E-Bloc. Operațiunea rulează în fundal și poate dura câteva minute dacă există multe sesiuni vechi. Vei primi o notificare la final.",
            title="Utilități România – E-Bloc",
            notification_id="utilitati_romania_ebloc_curata_sesiuni",
        )
        self.hass.async_create_task(self._async_curata_sesiuni_fundal())

    async def _async_curata_sesiuni_fundal(self) -> None:
        try:
            curatare = getattr(self.coordinator.client, "async_curata_sesiuni_vechi", None)
            if not callable(curatare):
                raise HomeAssistantError("Clientul E-Bloc nu permite curățarea sesiunilor.")

            rezultat = await asyncio.wait_for(curatare(), timeout=240)
            sterse = int(rezultat.get("sterse") or 0)
            esuate = int(rezultat.get("esuate") or 0)
            total = int(rezultat.get("total") or 0)

            mesaj = (
                "Curățarea sesiunilor E-Bloc a fost finalizată.\n\n"
                f"- Sesiuni găsite: **{total}**\n"
                f"- Sesiuni șterse: **{sterse}**\n"
                f"- Sesiuni păstrate: **{rezultat.get('pastrate', 0)}**\n"
                f"- Sesiuni neșterse: **{esuate}**\n"
                f"- Sesiuni rămase în portal: **{rezultat.get('ramase', 0)}**"
            )
            if esuate:
                mesaj += "\n\nUnele sesiuni nu au putut fi șterse. Poți reîncerca după un refresh al integrării."

            persistent_notification.async_create(
                self.hass,
                mesaj,
                title="Utilități România – E-Bloc",
                notification_id="utilitati_romania_ebloc_curata_sesiuni",
            )

            await self.coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Curățarea sesiunilor E-Bloc a eșuat: %s", err)
            persistent_notification.async_create(
                self.hass,
                f"Curățarea sesiunilor E-Bloc nu a putut fi finalizată complet: {err}",
                title="Utilități România – E-Bloc",
                notification_id="utilitati_romania_ebloc_curata_sesiuni",
            )
        finally:
            self._curatare_in_derulare = False


class ButonTrimiteNumarPersoaneEbloc(EntitateUtilitatiRomania, ButtonEntity):
    def __init__(self, coordonator: CoordonatorUtilitatiRomania, cont) -> None:
        super().__init__(coordonator)
        self.cont = cont
        alias = alias_loc_ebloc(cont.nume, cont.adresa, cont.id_cont, cont=cont)
        slug = slug_loc_ebloc(cont.id_cont, alias, cont.adresa, cont=cont)
        self._attr_unique_id = f"{coordonator.intrare.entry_id}_ebloc_{cont.id_cont}_trimite_numar_persoane"
        self._attr_name = f"Trimite număr persoane - {alias}"
        self._attr_icon = "mdi:account-arrow-up"
        self._attr_device_info = info_device_ebloc(coordonator.intrare.entry_id, cont)
        self._attr_suggested_object_id = f"{slug}_trimite_numar_persoane"
        self.entity_id = f"button.{slug}_trimite_numar_persoane"
        self._entity_numar = f"number.{slug}_numar_persoane_setare"

    @property
    def available(self) -> bool:
        data = getattr(self.coordinator, "data", None)
        if data is None:
            return False
        for consum in data.consumuri:
            if getattr(consum, "id_cont", None) == self.cont.id_cont and getattr(consum, "cheie", None) == "editare_persoane_permisa":
                return str(getattr(consum, "valoare", "")).lower() == "da"
        return False

    async def async_press(self) -> None:
        stare = self.hass.states.get(self._entity_numar)
        if not stare:
            raise HomeAssistantError(f"Nu există entitatea {self._entity_numar}")
        try:
            numar_persoane = int(float(stare.state))
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Valoarea pentru numărul de persoane nu este validă.") from err

        luna = None
        for consum in (getattr(self.coordinator.data, "consumuri", None) or []):
            if getattr(consum, "id_cont", None) == self.cont.id_cont and getattr(consum, "cheie", None) == "luna_setare_persoane":
                luna = getattr(consum, "valoare", None)
                break
        if not luna:
            raise HomeAssistantError("Nu am găsit luna pentru setarea numărului de persoane.")

        rezultat = await self.coordinator.client.async_seteaza_numar_persoane(self.cont.id_cont, str(luna), numar_persoane)
        text = str(rezultat).lower()
        if "error" in text or "eroare" in text:
            raise HomeAssistantError(f"e-bloc.ro a refuzat actualizarea numărului de persoane: {rezultat}")

        await self.coordinator.async_request_refresh()
