from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import logging
from typing import Any

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DIGI_COOKIES,
    CONF_FURNIZOR,
    CONF_INTERVAL_ACTUALIZARE,
    CONF_PAROLA,
    CONF_UTILIZATOR,
    DOMENIU,
)
from .exceptions import EroareAutentificare, EroareConectare, EroareLicenta
from .furnizori.registru import obtine_clasa_furnizor
from .licentiere import (
    async_salveaza_licenta_in_intrare,
    async_verifica_licenta,
    valideaza_rezultat_licenta,
)
from .modele import InstantaneuFurnizor
from .notificari import ManagerNotificari
from .storage_citiri import async_salveaza_citire, obtine_citire_cache

_LOGGER = logging.getLogger(__name__)


class CoordonatorUtilitatiRomania(DataUpdateCoordinator[InstantaneuFurnizor]):
    def __init__(self, hass: HomeAssistant, intrare: ConfigEntry) -> None:
        self.hass = hass
        self.intrare = intrare
        self.cheie_furnizor: str = intrare.data[CONF_FURNIZOR]
        self.sesiune: ClientSession = async_get_clientsession(hass)
        self._manager_notificari = ManagerNotificari(hass)
        self._notificari_incarcate = False
        self._task_refresh_initial_deer: asyncio.Task[None] | None = None

        interval_ore = intrare.options.get(
            CONF_INTERVAL_ACTUALIZARE,
            intrare.data.get(CONF_INTERVAL_ACTUALIZARE, 6),
        )

        clasa_furnizor = obtine_clasa_furnizor(self.cheie_furnizor)
        self.client = clasa_furnizor(
            sesiune=self.sesiune,
            utilizator=intrare.options.get(CONF_UTILIZATOR, intrare.data[CONF_UTILIZATOR]),
            parola=intrare.options.get(CONF_PAROLA, intrare.data[CONF_PAROLA]),
            optiuni={**intrare.data, **intrare.options},
        )

        if self.cheie_furnizor == "digi":
            cookies = intrare.options.get(
                CONF_DIGI_COOKIES,
                intrare.data.get(CONF_DIGI_COOKIES, []),
            )
            if hasattr(self.client, "importa_cookies"):
                self.client.importa_cookies(cookies)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMENIU}_{self.cheie_furnizor}",
            update_interval=timedelta(hours=interval_ore),
            config_entry=intrare,
        )

    async def async_inchide(self) -> None:
        if self._task_refresh_initial_deer is not None:
            self._task_refresh_initial_deer.cancel()
            try:
                await self._task_refresh_initial_deer
            except asyncio.CancelledError:
                pass
            finally:
                self._task_refresh_initial_deer = None

        inchidere = getattr(self.client, "async_inchide", None)
        if callable(inchidere):
            await inchidere()

    def _porneste_refresh_initial_deer_in_fundal(self) -> None:
        if self._task_refresh_initial_deer is not None and not self._task_refresh_initial_deer.done():
            return

        self._task_refresh_initial_deer = self.hass.async_create_task(
            self._async_refresh_initial_deer_in_fundal()
        )

    async def _async_refresh_initial_deer_in_fundal(self) -> None:
        try:
            instantaneu = await self.client.async_obtine_instantaneu_complet()

            try:
                snapshot = self._construieste_snapshot_notificari(instantaneu)
                await self._manager_notificari.proceseaza(snapshot)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Procesarea notificărilor a eșuat pentru %s: %s",
                    self.cheie_furnizor,
                    err,
                )

            self.async_set_updated_data(instantaneu)

        except EroareAutentificare as err:
            _LOGGER.warning(
                "Refresh-ul inițial în fundal a eșuat pentru %s din cauza autentificării: %s",
                self.cheie_furnizor,
                err,
            )
        except EroareConectare as err:
            _LOGGER.warning(
                "Refresh-ul inițial în fundal a eșuat pentru %s din cauza conexiunii: %s",
                self.cheie_furnizor,
                err,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Refresh-ul inițial în fundal a eșuat pentru %s: %s",
                self.cheie_furnizor,
                err,
            )
        finally:
            self._task_refresh_initial_deer = None

    async def _async_update_data(self) -> InstantaneuFurnizor:
        if not self._notificari_incarcate:
            try:
                await self._manager_notificari.async_incarca()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Nu s-a putut încărca storage-ul notificărilor pentru %s: %s",
                    self.cheie_furnizor,
                    err,
                )
            finally:
                self._notificari_incarcate = True

        try:
            rezultat_licenta = await async_verifica_licenta(self.hass, self.intrare)

            # Salvăm rezultatul verificării înainte de validarea strictă, astfel încât
            # senzorii globali de licență să reflecte corect și statusurile negative
            # primite de la server (revoked / expired / invalid). Nu suprascriem însă
            # cache-ul valid cu erori de conectare temporare.
            if rezultat_licenta.valida or not rezultat_licenta.eroare_conectare:
                await async_salveaza_licenta_in_intrare(self.hass, self.intrare, rezultat_licenta)
                await self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {
                        "entity_id": [
                            f"sensor.{DOMENIU}_status_licenta",
                            f"sensor.{DOMENIU}_plan_licenta",
                            f"sensor.{DOMENIU}_valabila_pana_la",
                            f"sensor.{DOMENIU}_ultima_verificare_licenta",
                            f"sensor.{DOMENIU}_cont_licenta",
                            f"sensor.{DOMENIU}_cod_licenta_mascat",
                            f"sensor.{DOMENIU}_mesaj_licenta",
                        ]
                    },
                    blocking=False,
                )

            valideaza_rezultat_licenta(rezultat_licenta)
        except EroareLicenta as err:
            raise UpdateFailed(f"Licență invalidă: {err}") from err

        try:
            if (
                self.cheie_furnizor == "deer"
                and self.data is None
                and hasattr(self.client, "async_obtine_instantaneu_minim")
                and hasattr(self.client, "async_obtine_instantaneu_complet")
            ):
                instantaneu = await self.client.async_obtine_instantaneu_minim()
                self._porneste_refresh_initial_deer_in_fundal()
                return instantaneu

            instantaneu = await self.client.async_obtine_instantaneu()

            try:
                snapshot = self._construieste_snapshot_notificari(instantaneu)
                await self._manager_notificari.proceseaza(snapshot)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Procesarea notificărilor a eșuat pentru %s: %s",
                    self.cheie_furnizor,
                    err,
                )

            await self._sincronizeaza_citiri_din_portal(instantaneu)

            return instantaneu

        except EroareAutentificare as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EroareConectare as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Eroare neașteptată la {self.cheie_furnizor}: {err}") from err


    async def _sincronizeaza_citiri_din_portal(self, instantaneu: InstantaneuFurnizor) -> None:
        if getattr(instantaneu, "furnizor", self.cheie_furnizor) != "apa_canal":
            return

        for cont in getattr(instantaneu, "conturi", None) or []:
            id_cont = str(getattr(cont, "id_cont", "") or "").strip()
            raw = getattr(cont, "date_brute", None) or {}
            if not id_cont or not isinstance(raw, dict):
                continue

            ultima = raw.get("last_meter_reading") or {}
            if not isinstance(ultima, dict):
                continue

            valoare = self._float_or_none(ultima.get("value"))
            data_citire = self._normalize_date_like(ultima.get("date")) or ultima.get("date")
            motiv = str(ultima.get("reason") or ultima.get("category") or "").strip().lower()

            if valoare is None or not data_citire:
                continue
            if motiv and not any(token in motiv for token in ("client", "citire client", "customer")):
                continue

            existent = obtine_citire_cache(self.hass, "apa_canal", id_cont) or {}
            existent_valoare = self._float_or_none(existent.get("valoare"))
            existent_timestamp = str(existent.get("timestamp") or "")[:10]
            if existent_valoare == valoare and existent_timestamp == str(data_citire)[:10]:
                continue

            await async_salveaza_citire(
                self.hass,
                "apa_canal",
                id_cont,
                valoare,
                timestamp=str(data_citire),
                sursa="portal",
                extra={
                    "motiv": ultima.get("reason"),
                    "categorie": ultima.get("category"),
                    "serie_contor": ultima.get("serial_number"),
                    "unitate": ultima.get("unit"),
                },
            )

    def _construieste_snapshot_notificari(
        self, instantaneu: InstantaneuFurnizor
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "facturi": self._extrage_facturi_pentru_notificari(instantaneu),
            "ferestre_index": self._extrage_ferestre_index_pentru_notificari(instantaneu),
        }

    def _extrage_facturi_pentru_notificari(
        self, instantaneu: InstantaneuFurnizor
    ) -> list[dict[str, Any]]:
        facturi_normalizate: list[dict[str, Any]] = []
        facturi = getattr(instantaneu, "facturi", None) or []
        conturi = getattr(instantaneu, "conturi", None) or []
        furnizor = getattr(instantaneu, "furnizor", self.cheie_furnizor)

        conturi_map: dict[str, dict[str, Any]] = {}
        for cont in conturi:
            id_cont = getattr(cont, "id_cont", None)
            if not id_cont:
                continue
            conturi_map[str(id_cont)] = {
                "adresa": getattr(cont, "adresa", None),
                "nume_cont": getattr(cont, "nume", None),
                "tip_utilitate": getattr(cont, "tip_utilitate", None),
                "tip_serviciu": getattr(cont, "tip_serviciu", None),
            }

        for factura in facturi:
            factura_id = self._construieste_id_factura(factura, instantaneu)
            if not factura_id:
                continue

            id_cont = getattr(factura, "id_cont", None)
            info_cont = conturi_map.get(str(id_cont), {}) if id_cont is not None else {}

            este_platita = self._factura_este_platita(factura)

            facturi_normalizate.append(
                {
                    "id": factura_id,
                    "furnizor": furnizor,
                    "titlu": getattr(factura, "titlu", None),
                    "suma": getattr(factura, "valoare", None),
                    "moneda": getattr(factura, "moneda", None),
                    "scadenta": self._date_to_iso(getattr(factura, "data_scadenta", None)),
                    "data_emitere": self._date_to_iso(getattr(factura, "data_emitere", None)),
                    "platita": este_platita,
                    "stare": getattr(factura, "stare", None),
                    "categorie": getattr(factura, "categorie", None),
                    "id_cont": id_cont,
                    "id_contract": getattr(factura, "id_contract", None),
                    "tip_utilitate": getattr(factura, "tip_utilitate", None) or info_cont.get("tip_utilitate"),
                    "tip_serviciu": getattr(factura, "tip_serviciu", None) or info_cont.get("tip_serviciu"),
                    "este_prosumator": getattr(factura, "este_prosumator", None),
                    "adresa": info_cont.get("adresa"),
                    "nume_cont": info_cont.get("nume_cont"),
                    "date_brute": getattr(factura, "date_brute", None),
                }
            )

        return facturi_normalizate

    def _extrage_ferestre_index_pentru_notificari(
        self, instantaneu: InstantaneuFurnizor
    ) -> list[dict[str, Any]]:
        ferestre: list[dict[str, Any]] = []
        furnizor = getattr(instantaneu, "furnizor", self.cheie_furnizor)

        # Notificările pentru transmiterea indexului se generează doar pentru
        # furnizorii pentru care integrarea are efectiv flux de autocitire.
        # DEER, de exemplu, expune date tehnice de contor, dar nu oferă în
        # integrarea noastră un flux real de transmitere index; fără această
        # filtrare poate apărea o notificare falsă.
        furnizori_cu_autocitire = {
            "apa_canal",
            "ebloc",
            "eon",
            "hidroelectrica",
            "myelectrica",
        }
        if furnizor not in furnizori_cu_autocitire:
            return ferestre

        conturi = getattr(instantaneu, "conturi", None) or []

        for cont in conturi:
            if not self._citire_index_permisa_din_instantaneu(instantaneu, cont):
                continue

            fereastra = self._extrage_fereastra_index_din_cont(cont)
            if not fereastra:
                # Dacă furnizorul confirmă explicit că transmiterea este permisă,
                # dar nu avem o perioadă parsabilă, folosim o fereastră minimă
                # strict pentru notificare. Nu folosim niciodată citiri anterioare
                # drept dovadă că perioada este activă.
                azi = date.today()
                fereastra = (azi.isoformat(), (azi + timedelta(days=5)).isoformat())

            start, end = fereastra
            if not start or not end:
                continue

            ferestre.append(
                {
                    "furnizor": furnizor,
                    "cont": getattr(cont, "id_cont", None),
                    "nume_cont": getattr(cont, "nume", None),
                    "adresa": getattr(cont, "adresa", None),
                    "tip_utilitate": getattr(cont, "tip_utilitate", None),
                    "tip_serviciu": getattr(cont, "tip_serviciu", None),
                    "start": start,
                    "end": end,
                    "citire_permisa": True,
                    "date_brute": getattr(cont, "date_brute", None),
                }
            )

        return ferestre

    def _citire_index_permisa_din_instantaneu(
        self,
        instantaneu: InstantaneuFurnizor,
        cont: Any,
    ) -> bool:
        id_cont = str(getattr(cont, "id_cont", "") or "").strip()
        consumuri = getattr(instantaneu, "consumuri", None) or []

        for consum in consumuri:
            if str(getattr(consum, "id_cont", "") or "").strip() != id_cont:
                continue
            if getattr(consum, "cheie", None) not in {"citire_permisa", "citire_index_permisa"}:
                continue

            permis = self._valoare_booleana_stricta(getattr(consum, "valoare", None))
            if permis is not None:
                return permis

        raw = getattr(cont, "date_brute", None) or {}
        if not isinstance(raw, dict):
            return False

        permis = self._citire_index_permisa_din_raw(raw)
        return permis is True

    def _citire_index_permisa_din_raw(self, raw: dict[str, Any]) -> bool | None:
        chei_directe = (
            "citire_permisa",
            "citire_index_permisa",
            "reading_allowed",
            "readingAvailable",
            "reading_available",
            "self_reading_allowed",
            "can_submit_index",
            "canSubmitIndex",
            "index_submission_allowed",
            "autocitire_permisa",
            "PACIndicator",
        )

        for cheie in chei_directe:
            if cheie not in raw:
                continue
            permis = self._valoare_booleana_stricta(raw.get(cheie))
            if permis is not None:
                return permis

        window_data = raw.get("window_data") or raw.get("meter_reading_window") or {}
        if isinstance(window_data, dict):
            for cheie in (
                "Is_Window_Open",
                "is_window_open",
                "IsWindowOpen",
                "window_open",
                "open",
                "active",
                "citire_permisa",
                "reading_allowed",
            ):
                if cheie not in window_data:
                    continue
                permis = self._valoare_booleana_stricta(window_data.get(cheie))
                if permis is not None:
                    return permis

        meter_list = raw.get("meter_list") or {}
        if isinstance(meter_list, dict):
            for cheie in ("PACIndicator", "citire_permisa", "reading_allowed"):
                if cheie not in meter_list:
                    continue
                permis = self._valoare_booleana_stricta(meter_list.get(cheie))
                if permis is not None:
                    return permis

        return None

    @staticmethod
    def _valoare_booleana_stricta(valoare: Any) -> bool | None:
        if valoare is None:
            return None

        if isinstance(valoare, bool):
            return valoare

        if isinstance(valoare, (int, float)):
            if int(valoare) == 1:
                return True
            if int(valoare) == 0:
                return False
            return None

        text = str(valoare).strip().lower()
        if not text:
            return None

        valori_true = {
            "da",
            "true",
            "1",
            "yes",
            "on",
            "open",
            "opened",
            "deschis",
            "activ",
            "activa",
            "activă",
            "permisa",
            "permisă",
            "permis",
            "allowed",
            "available",
            "x",
            "y",
        }
        valori_false = {
            "nu",
            "false",
            "0",
            "no",
            "off",
            "closed",
            "inchis",
            "închis",
            "inactiv",
            "inactiva",
            "inactivă",
            "nepermis",
            "nepermisa",
            "nepermisă",
            "not_allowed",
            "unavailable",
            "indisponibil",
            "unknown",
            "necunoscut",
        }

        if text in valori_true:
            return True
        if text in valori_false:
            return False

        return None

    def _extrage_fereastra_index_din_cont(
        self, cont: Any
    ) -> tuple[str | None, str | None] | None:
        raw = getattr(cont, "date_brute", None) or {}
        if not isinstance(raw, dict):
            return None

        start = self._normalize_date_like(
            raw.get("fereastra_citire_start")
            or raw.get("reading_period_start")
            or raw.get("readingStartDate")
            or raw.get("start_date")
        )
        end = self._normalize_date_like(
            raw.get("fereastra_citire_end")
            or raw.get("reading_period_end")
            or raw.get("readingEndDate")
            or raw.get("end_date")
        )
        if start and end:
            return start, end

        window_data = raw.get("window_data") or raw.get("meter_reading_window") or {}
        if isinstance(window_data, dict):
            start = self._normalize_date_like(
                window_data.get("StartDate")
                or window_data.get("StartDateENC")
                or window_data.get("start_date")
                or window_data.get("startDate")
            )
            end = self._normalize_date_like(
                window_data.get("EndDate")
                or window_data.get("EndDateENC")
                or window_data.get("end_date")
                or window_data.get("endDate")
            )
            if start and end:
                return start, end

        start = self._normalize_date_like(
            raw.get("StartDatePAC")
            or raw.get("inceput_perioada")
            or raw.get("indecsi_start")
        )
        end = self._normalize_date_like(
            raw.get("EndDatePAC")
            or raw.get("sfarsit_perioada")
            or raw.get("indecsi_end")
        )
        if start and end:
            return start, end

        contoare = raw.get("contoare") or []
        if isinstance(contoare, list):
            for contor in contoare:
                if not isinstance(contor, dict):
                    continue

                start = self._normalize_date_like(
                    contor.get("indecsi_start")
                    or contor.get("inceput_perioada")
                    or contor.get("start")
                )
                end = self._normalize_date_like(
                    contor.get("indecsi_end")
                    or contor.get("sfarsit_perioada")
                    or contor.get("end")
                )
                if start and end:
                    return start, end

        return None

    def _factura_este_platita(self, factura: Any) -> bool:
        stare = str(getattr(factura, "stare", None) or "").strip().lower()
        raw = getattr(factura, "date_brute", None) or {}

        if isinstance(raw, list):
            if raw:
                return False
            raw = {}

        if not isinstance(raw, dict):
            raw = {}

        if stare in {
            "platita",
            "plătită",
            "platit",
            "plătit",
            "achitat",
            "paid",
            "closed",
            "settled",
        }:
            return True

        if stare in {
            "neplatita",
            "neplătită",
            "neachitat",
            "unpaid",
            "remaining",
            "restant",
            "open",
            "due",
        }:
            return False

        restante_candidates = [
            raw.get("rest_plata"),
            raw.get("amount_remaining"),
            raw.get("AmountRemaining"),
            raw.get("remainingAmount"),
            raw.get("remaining"),
            raw.get("remainingValue"),
            raw.get("rest"),
            raw.get("restToPay"),
            raw.get("amountToPay"),
            raw.get("UnpaidValue"),
            raw.get("AmountDue"),
        ]
        for valoare in restante_candidates:
            numar = self._float_or_none(valoare)
            if numar is None:
                continue
            if numar > 0:
                return False
            if numar == 0:
                return True

        status_text = str(
            raw.get("invoice_status")
            or raw.get("InvoiceStatus")
            or raw.get("status")
            or raw.get("Status")
            or ""
        ).strip().lower()

        if status_text in {
            "paid",
            "platita",
            "plătită",
            "achitat",
            "settled",
            "closed",
        }:
            return True

        if status_text in {
            "unpaid",
            "neplatita",
            "neplătită",
            "restant",
            "remaining",
            "open",
            "due",
        }:
            return False

        # fallback corect:
        # dacă nu știm sigur că e plătită → o considerăm NEPLĂTITĂ
        return False

    def _construieste_id_factura(self, factura: Any, instantaneu: InstantaneuFurnizor) -> str | None:
        id_factura = getattr(factura, "id_factura", None)
        if id_factura:
            return str(id_factura)

        parti = [
            getattr(instantaneu, "furnizor", self.cheie_furnizor),
            getattr(factura, "id_cont", None),
            getattr(factura, "id_contract", None),
            getattr(factura, "titlu", None),
            self._date_to_iso(getattr(factura, "data_emitere", None)),
            self._date_to_iso(getattr(factura, "data_scadenta", None)),
            str(getattr(factura, "valoare", None))
            if getattr(factura, "valoare", None) is not None
            else None,
        ]
        valori = [str(x).strip() for x in parti if x not in (None, "", "None")]
        return "|".join(valori) if valori else None

    @staticmethod
    def _float_or_none(valoare: Any) -> float | None:
        if valoare in (None, "", "None"):
            return None
        try:
            text = str(valoare).strip().replace(" ", "")
            text = text.replace(".", "").replace(",", ".") if "," in text and "." in text else text.replace(",", ".")
            return float(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date_to_iso(valoare: date | datetime | str | None) -> str | None:
        if valoare is None:
            return None
        if isinstance(valoare, datetime):
            return valoare.date().isoformat()
        if isinstance(valoare, date):
            return valoare.isoformat()
        if isinstance(valoare, str):
            return CoordonatorUtilitatiRomania._normalize_date_like(valoare)
        return None

    @staticmethod
    def _normalize_date_like(valoare: Any) -> str | None:
        if valoare in (None, ""):
            return None

        if isinstance(valoare, datetime):
            return valoare.date().isoformat()

        if isinstance(valoare, date):
            return valoare.isoformat()

        text = str(valoare).strip()
        if not text:
            return None

        text = text.replace("Z", "+00:00")

        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            pass

        for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue

        if "T" in text:
            baza = text.split("T", 1)[0]
            try:
                return datetime.strptime(baza, "%Y-%m-%d").date().isoformat()
            except ValueError:
                pass

        return None