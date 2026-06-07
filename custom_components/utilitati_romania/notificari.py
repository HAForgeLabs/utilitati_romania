from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 5
STORAGE_KEY = "utilitati_romania_notificari"

EVENT_NOTIFICARE = "utilitati_romania_notificare"

PREFERINTE_IMPLICITE = {
    "facturi_noi": True,
    "scadente": True,
    "indexuri": True,
    "praguri_scadenta": [5, 3, 1],
}


def _normalizeaza_preferinte_notificari(preferinte: dict[str, Any] | None) -> dict[str, Any]:
    date = dict(PREFERINTE_IMPLICITE)
    if isinstance(preferinte, dict):
        for cheie in ("facturi_noi", "scadente", "indexuri"):
            if cheie in preferinte:
                date[cheie] = bool(preferinte.get(cheie))

        praguri = preferinte.get("praguri_scadenta")
        if isinstance(praguri, list):
            praguri_curate: list[int] = []
            for prag in praguri:
                try:
                    prag_int = int(prag)
                except (TypeError, ValueError):
                    continue
                if 0 <= prag_int <= 30 and prag_int not in praguri_curate:
                    praguri_curate.append(prag_int)
            if praguri_curate:
                date["praguri_scadenta"] = sorted(praguri_curate, reverse=True)

    return date


async def async_salveaza_preferinte_notificari(hass: HomeAssistant, preferinte: dict[str, Any]) -> dict[str, Any]:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    preferinte_normalizate = _normalizeaza_preferinte_notificari(preferinte)
    data["preferinte"] = preferinte_normalizate
    data.setdefault("notificate", [])
    data.setdefault("initializat", False)
    await store.async_save(data)
    return preferinte_normalizate


class ManagerNotificari:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._date_notificate: set[str] = set()
        self._surse_facturi_initializate: set[str] = set()
        self._preferinte = dict(PREFERINTE_IMPLICITE)
        self._initializat = False
        self._lock = asyncio.Lock()

    async def async_incarca(self) -> None:
        data = await self._store.async_load()
        if not data:
            return

        self._date_notificate = set(data.get("notificate", []))
        self._surse_facturi_initializate = set(data.get("surse_facturi_initializate", []))
        self._preferinte = _normalizeaza_preferinte_notificari(data.get("preferinte"))
        self._initializat = bool(data.get("initializat", False))

    async def _salveaza(self) -> None:
        await self._store.async_save(
            {
                "notificate": sorted(self._date_notificate),
                "surse_facturi_initializate": sorted(self._surse_facturi_initializate),
                "initializat": self._initializat,
                "preferinte": self._preferinte,
            }
        )

    async def proceseaza(self, snapshot: dict[str, Any]) -> None:
        async with self._lock:
            data = await self._store.async_load() or {}
            if data:
                self._date_notificate = set(data.get("notificate", []))
                self._surse_facturi_initializate = set(data.get("surse_facturi_initializate", []))
                self._initializat = bool(data.get("initializat", self._initializat))
            self._preferinte = _normalizeaza_preferinte_notificari(data.get("preferinte"))

            facturi = snapshot.get("facturi", [])
            ferestre_index = snapshot.get("ferestre_index", [])

            _LOGGER.debug(
                "Utilitati Romania notificari: initializat=%s, facturi=%s, ferestre_index=%s",
                self._initializat,
                len(facturi),
                len(ferestre_index),
            )

            if not self._initializat:
                if not facturi and not ferestre_index:
                    _LOGGER.debug(
                        "Notificările nu se initializează încă; nu există facturi sau ferestre de index."
                    )
                    return

                changed = False
                fortat = not self._initializat
                surse_noi = self._surse_facturi_neinitializate(facturi)
                changed |= await self._proceseaza_facturi(facturi, surse_initializate=surse_noi)
                self._surse_facturi_initializate.update(surse_noi)
                changed |= await self._proceseaza_index(
                    ferestre_index,
                    fortat=fortat,
                )

                self._initializat = True
                await self._salveaza()

                _LOGGER.debug(
                    "Notificările Utilități România au fost inițializate și starea curentă a fost marcată."
                )
                return

            changed = False
            surse_noi = self._surse_facturi_neinitializate(facturi)
            changed |= await self._proceseaza_facturi(facturi, surse_initializate=surse_noi)
            if surse_noi:
                self._surse_facturi_initializate.update(surse_noi)
                changed = True
            changed |= await self._proceseaza_index(ferestre_index)

            if changed:
                await self._salveaza()

    def _surse_facturi_neinitializate(self, facturi: list[dict[str, Any]]) -> set[str]:
        surse: set[str] = set()
        for factura in facturi:
            cheie = self._cheie_sursa_factura(factura)
            if cheie and cheie not in self._surse_facturi_initializate:
                surse.add(cheie)
        return surse

    @staticmethod
    def _cheie_sursa_factura(factura: dict[str, Any]) -> str:
        furnizor = str(factura.get("furnizor") or "furnizor").strip().lower()
        cont = str(factura.get("id_cont") or factura.get("id_contract") or "cont").strip().lower()
        return f"{furnizor}:{cont}"

    async def _proceseaza_facturi(
        self,
        facturi: list[dict[str, Any]],
        *,
        surse_initializate: set[str] | None = None,
    ) -> bool:
        azi = datetime.now().date()
        changed = False
        surse_initializate = surse_initializate or set()

        for factura in facturi:
            factura_id = factura.get("id")
            furnizor = self._format_furnizor(factura.get("furnizor"))
            suma = factura.get("suma")
            moneda = self._safe_text(factura.get("moneda"), "lei")
            scadenta = factura.get("scadenta")
            platita = bool(factura.get("platita", False))
            adresa = self._safe_text(factura.get("adresa"))
            nume_cont = self._safe_text(factura.get("nume_cont"))

            if not factura_id:
                continue

            if suma is None:
                continue

            if self._float_or_none(suma) == 0:
                continue

            locatie = self._format_locatie(adresa, nume_cont, furnizor)

            if not platita and self._preferinte.get("facturi_noi", True):
                key_emitere = f"{factura_id}_emisa"
                sursa_factura = self._cheie_sursa_factura(factura)
                if sursa_factura in surse_initializate and key_emitere not in self._date_notificate:
                    self._date_notificate.add(key_emitere)
                    changed = True
                    _LOGGER.debug(
                        "Factura existentă %s pentru sursa nouă %s a fost marcată ca deja cunoscută, fără notificare.",
                        factura_id,
                        sursa_factura,
                    )
                elif key_emitere not in self._date_notificate:
                    await self._trimite(
                        cheie=key_emitere,
                        tip="factura_emisa",
                        titlu="Factură emisă",
                        mesaj=(
                            f"{furnizor}: factură nouă emisă "
                            f"({self._format_suma(suma, moneda)}){locatie}"
                        ),
                        extra=factura,
                    )
                    self._date_notificate.add(key_emitere)
                    changed = True

            if platita or not scadenta or not self._preferinte.get("scadente", True):
                continue

            try:
                data_scadenta = datetime.fromisoformat(scadenta).date()
            except Exception:
                continue

            zile_ramase = (data_scadenta - azi).days

            for prag in self._preferinte.get("praguri_scadenta", [5, 3, 1]):
                key_due = f"{factura_id}_due_{prag}"
                if zile_ramase == prag and key_due not in self._date_notificate:
                    await self._trimite(
                        cheie=key_due,
                        tip="factura_scadenta",
                        titlu="Factură de plătit",
                        mesaj=(
                            f"{furnizor}: factură scadentă în {prag} "
                            f"{'zi' if prag == 1 else 'zile'} "
                            f"({self._format_suma(suma, moneda)}){locatie}"
                        ),
                        extra=factura,
                    )
                    self._date_notificate.add(key_due)
                    changed = True

        return changed

    async def _proceseaza_index(
        self,
        ferestre: list[dict[str, Any]],
        fortat: bool = False,
    ) -> bool:
        if not self._preferinte.get("indexuri", True):
            return False

        azi = datetime.now().date()
        changed = False

        for fereastra in ferestre:
            start = fereastra.get("start")
            end = fereastra.get("end")
            furnizor = self._format_furnizor(fereastra.get("furnizor"))
            cont = fereastra.get("cont")
            adresa = self._safe_text(fereastra.get("adresa"))
            nume_cont = self._safe_text(fereastra.get("nume_cont"))

            if not start or not end or not cont:
                continue

            try:
                start_d = datetime.fromisoformat(start).date()
                end_d = datetime.fromisoformat(end).date()
            except Exception:
                continue

            key_index = f"{furnizor}_{cont}_index_start_{start}"
            locatie = self._format_locatie(adresa, nume_cont, furnizor)

            if start_d <= azi <= end_d and (fortat or key_index not in self._date_notificate):
                await self._trimite(
                    cheie=key_index,
                    tip="index_start",
                    titlu="Transmitere index",
                    mesaj=f"{furnizor}: a început perioada de transmitere index{locatie}",
                    extra=fereastra,
                )
                self._date_notificate.add(key_index)
                changed = True

        return changed

    async def _trimite(
        self,
        cheie: str,
        tip: str,
        titlu: str,
        mesaj: str,
        extra: dict[str, Any],
    ) -> None:
        _LOGGER.debug("Notificare %s: %s", tip, mesaj)

        notification_id = f"utilitati_romania_{cheie}"

        persistent_notification.async_create(
            self.hass,
            mesaj,
            title=titlu,
            notification_id=notification_id,
        )

        self.hass.bus.async_fire(
            EVENT_NOTIFICARE,
            {
                "tip": tip,
                "titlu": titlu,
                "mesaj": mesaj,
                "data": extra,
                "cheie": cheie,
            },
        )

    @staticmethod
    def _safe_text(value: Any, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, dict):
            return ManagerNotificari._format_adresa_dict(value) or default
        if isinstance(value, (list, tuple, set)):
            return default
        text = str(value).strip()
        return text or default

    @staticmethod
    def _format_adresa_dict(value: dict[str, Any]) -> str:
        valori: list[str] = []
        for cheie in (
            "street", "street_name", "streetName", "Street",
            "number", "street_number", "streetNumber", "building", "block",
            "entrance", "floor", "apartment",
            "postal_code", "postalCode", "postcode",
            "city", "City", "locality",
            "county", "County", "district", "district_code",
        ):
            item = value.get(cheie)
            if item is None:
                continue
            text = str(item).strip()
            if text and text not in valori:
                valori.append(text)
        return ", ".join(valori)


    @staticmethod
    def _format_furnizor(value: Any) -> str:
        text = ManagerNotificari._safe_text(value, "Furnizor necunoscut")
        if text.lower() == "engie":
            return "ENGIE"
        return text

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip()
        if not text:
            return None

        text = text.replace(" ", "")
        text = text.replace(",", ".")

        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _format_suma(suma: Any, moneda: str) -> str:
        if suma is None:
            return f"sumă necunoscută {moneda}".strip()
        return f"{suma} {moneda}".strip()

    @staticmethod
    def _format_locatie(adresa: str, nume_cont: str, furnizor: str = "") -> str:
        if furnizor.strip().lower() == "engie" and nume_cont:
            return f" — {nume_cont}"
        if adresa and nume_cont:
            if adresa.lower() == nume_cont.lower():
                return f" — {adresa}"
            return f" — {nume_cont}, {adresa}"
        if adresa:
            return f" — {adresa}"
        if nume_cont:
            return f" — {nume_cont}"
        return ""