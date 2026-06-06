from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CHEIE_LICENTA,
    CONF_FURNIZOR,
    CONF_UTILIZATOR,
    DATE_VERIFICARE_LICENTA,
    DOMENIU,
    FURNIZOR_ADMIN_GLOBAL,
    IMPLICIT_ZILE_GRATIE_LICENTA,
    LICENTA_STATUS_ACTIVA,
    LICENTA_STATUS_ACTIVATION_LIMIT,
    LICENTA_STATUS_EXPIRATA,
    LICENTA_STATUS_INVALIDA,
    LICENTA_STATUS_PRODUS_INVALID,
    LICENTA_STATUS_REVOCATA,
    LICENTA_STATUS_TRIAL,
    LICENTA_STATUS_NECUNOSCUT,
    URL_API_LICENTA,
)
from .exceptions import EroareLicenta

_LOGGER = logging.getLogger(__name__)

STATUSURI_ACCEPTATE = {LICENTA_STATUS_ACTIVA, LICENTA_STATUS_TRIAL}
VERSIUNE_STORAGE_LICENTA = 1
CHEIE_STORAGE_LICENTA = f"{DOMENIU}_licenta"


def normalizeaza_cheia_licenta(cheie: str | None) -> str:
    """Normalizează codul de licență introdus de utilizator."""
    text = str(cheie or "").strip()
    for caracter in ("‐", "‑", "‒", "–", "—", "―"):
        text = text.replace(caracter, "-")
    text = "".join(text.split())
    return text.upper() or "TRIAL"


@dataclass(slots=True)
class RezultatLicenta:
    valida: bool
    status: str
    plan: str | None = None
    expira_la: str | None = None
    mesaj: str | None = None
    verificata_la: str | None = None
    eroare_conectare: bool = False
    username: str | None = None

    def ca_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valida,
            "status": self.status,
            "plan": self.plan,
            "expires_at": self.expira_la,
            "message": self.mesaj,
            "checked_at": self.verificata_la,
            "connection_error": self.eroare_conectare,
            "username": self.username,
        }


def construieste_fingerprint_instanta(hass: HomeAssistant) -> str:
    parti = [
        DOMENIU,
        getattr(hass.config, "config_dir", "") or "",
        getattr(hass.config, "location_name", "") or "",
        getattr(hass.config, "internal_url", "") or "",
        getattr(hass.config, "external_url", "") or "",
    ]
    return hashlib.sha256("|".join(parti).encode("utf-8")).hexdigest()


async def async_obtine_licenta_globala(hass: HomeAssistant) -> dict[str, Any]:
    store = Store[dict[str, Any]](hass, VERSIUNE_STORAGE_LICENTA, CHEIE_STORAGE_LICENTA)
    data = await store.async_load()
    return data if isinstance(data, dict) else {}


async def async_salveaza_licenta_globala(
    hass: HomeAssistant,
    cheie_licenta: str,
    utilizator: str,
    rezultat: RezultatLicenta | None = None,
) -> None:
    store = Store[dict[str, Any]](hass, VERSIUNE_STORAGE_LICENTA, CHEIE_STORAGE_LICENTA)

    utilizator_final = str(
        (rezultat.username if rezultat and rezultat.username else utilizator) or ""
    ).strip()

    payload: dict[str, Any] = {
        CONF_CHEIE_LICENTA: normalizeaza_cheia_licenta(cheie_licenta),
        CONF_UTILIZATOR: utilizator_final,
    }

    if rezultat is not None:
        payload[DATE_VERIFICARE_LICENTA] = rezultat.ca_dict()

    await store.async_save(payload)


def _entry_is_admin(intrare: ConfigEntry | None) -> bool:
    if intrare is None:
        return False
    return intrare.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL


async def async_obtine_context_licenta(
    hass: HomeAssistant,
    intrare: ConfigEntry | None = None,
    utilizator: str | None = None,
    cheie_licenta: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    storage = await async_obtine_licenta_globala(hass)
    storage_utilizator = str(storage.get(CONF_UTILIZATOR, "")).strip()
    storage_cheie = normalizeaza_cheia_licenta(storage.get(CONF_CHEIE_LICENTA, "")) if storage.get(CONF_CHEIE_LICENTA) else ""

    entry_utilizator = ""
    entry_cheie = ""
    if intrare is not None and _entry_is_admin(intrare):
        entry_utilizator = str(
            intrare.options.get(CONF_UTILIZATOR, intrare.data.get(CONF_UTILIZATOR, ""))
        ).strip()
        entry_cheie = normalizeaza_cheia_licenta(
            intrare.options.get(CONF_CHEIE_LICENTA, intrare.data.get(CONF_CHEIE_LICENTA, ""))
        )

    utilizator_final = str(utilizator).strip() if utilizator is not None else (storage_utilizator or entry_utilizator)
    cheie_finala = normalizeaza_cheia_licenta(cheie_licenta) if cheie_licenta is not None else (storage_cheie or entry_cheie or "TRIAL")
    return utilizator_final, cheie_finala, storage


def _date_licenta_din_storage_sunt_pentru_contextul_curent(
    date_licenta_globala: dict[str, Any],
    cheie_licenta: str,
    utilizator: str,
) -> bool:
    cheie_ok = normalizeaza_cheia_licenta(date_licenta_globala.get(CONF_CHEIE_LICENTA, "")) == normalizeaza_cheia_licenta(cheie_licenta)

    utilizator_stocat = str(date_licenta_globala.get(CONF_UTILIZATOR, "")).strip()
    if not utilizator or not utilizator_stocat:
        return cheie_ok

    return cheie_ok and utilizator_stocat == str(utilizator).strip()


async def async_valideaza_licenta(
    hass: HomeAssistant,
    cheie_licenta: str,
    utilizator: str,
) -> RezultatLicenta:
    sesiune = async_get_clientsession(hass)
    payload = {
        "license_key": normalizeaza_cheia_licenta(cheie_licenta),
        "fingerprint": construieste_fingerprint_instanta(hass),
        "product": DOMENIU,
        "username": utilizator,
    }

    try:
        async with sesiune.post(URL_API_LICENTA, json=payload, timeout=20) as raspuns:
            data = await raspuns.json(content_type=None)

            return RezultatLicenta(
                valida=bool(data.get("valid", False)),
                status=str(data.get("status", LICENTA_STATUS_NECUNOSCUT)),
                plan=data.get("plan"),
                expira_la=data.get("expires_at"),
                mesaj=data.get("message") or data.get("error"),
                verificata_la=_acum_utc_iso(),
                eroare_conectare=False,
                username=str(data.get("username", "")).strip() or None,
            )

    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.warning("Validarea licenței a eșuat: %s", err)
        return RezultatLicenta(
            valida=False,
            status=LICENTA_STATUS_NECUNOSCUT,
            mesaj=str(err),
            verificata_la=_acum_utc_iso(),
            eroare_conectare=True,
        )


def extrage_date_licenta_stocate(intrare: ConfigEntry) -> dict[str, Any]:
    stocate = intrare.options.get(DATE_VERIFICARE_LICENTA) or intrare.data.get(DATE_VERIFICARE_LICENTA) or {}
    return stocate if isinstance(stocate, dict) else {}


def licenta_este_acceptata(date_licenta: dict[str, Any]) -> bool:
    return bool(date_licenta.get("valid")) and date_licenta.get("status") in STATUSURI_ACCEPTATE


def se_poate_folosi_licenta_din_cache(
    date_licenta: dict[str, Any],
    zile_gratie: int = IMPLICIT_ZILE_GRATIE_LICENTA,
) -> bool:
    if not licenta_este_acceptata(date_licenta):
        return False

    verificata_la = date_licenta.get("checked_at")
    if not verificata_la:
        return False

    try:
        dt = datetime.fromisoformat(str(verificata_la).replace("Z", "+00:00"))
    except ValueError:
        return False

    return datetime.now(UTC) <= dt + timedelta(days=zile_gratie)


def mascheaza_cheia_licenta(cheie: str | None) -> str:
    if not cheie:
        return ""
    if len(cheie) <= 4:
        return "*" * len(cheie)
    return f"{cheie[:4]}***{cheie[-2:]}"


async def async_verifica_licenta(
    hass: HomeAssistant,
    intrare: ConfigEntry | None = None,
) -> RezultatLicenta:
    if intrare is None:
        utilizator, cheie, storage = await async_obtine_context_licenta(hass)
        if not utilizator:
            return RezultatLicenta(
                valida=True,
                status=LICENTA_STATUS_TRIAL,
                plan="trial_local",
                mesaj="Trial local activ.",
                verificata_la=_acum_utc_iso(),
            )
    else:
        utilizator, cheie, storage = await async_obtine_context_licenta(hass, intrare=intrare)

    rezultat = await async_valideaza_licenta(hass, cheie, utilizator)

    if rezultat.valida:
        return rezultat

    if rezultat.eroare_conectare:
        cache = extrage_date_licenta_stocate(intrare) if intrare is not None else {}
        if se_poate_folosi_licenta_din_cache(cache):
            return RezultatLicenta(
                valida=True,
                status=cache.get("status", LICENTA_STATUS_NECUNOSCUT),
                plan=cache.get("plan"),
                expira_la=cache.get("expires_at"),
                mesaj=cache.get("message"),
                verificata_la=cache.get("checked_at"),
                username=cache.get("username"),
            )

        cache_global = storage.get(DATE_VERIFICARE_LICENTA) if isinstance(storage, dict) else {}
        if (
            isinstance(cache_global, dict)
            and _date_licenta_din_storage_sunt_pentru_contextul_curent(storage, cheie, utilizator)
            and se_poate_folosi_licenta_din_cache(cache_global)
        ):
            return RezultatLicenta(
                valida=True,
                status=cache_global.get("status", LICENTA_STATUS_NECUNOSCUT),
                plan=cache_global.get("plan"),
                expira_la=cache_global.get("expires_at"),
                mesaj=cache_global.get("message"),
                verificata_la=cache_global.get("checked_at"),
                username=cache_global.get("username"),
            )

    return rezultat


def valideaza_rezultat_licenta(rezultat: RezultatLicenta) -> None:
    if rezultat.valida:
        return

    if rezultat.eroare_conectare:
        raise EroareLicenta(rezultat.mesaj or "server_licenta_indisponibil")
    if rezultat.status == LICENTA_STATUS_INVALIDA:
        raise EroareLicenta("licenta_invalida")
    if rezultat.status == LICENTA_STATUS_EXPIRATA:
        raise EroareLicenta("licenta_expirata")
    if rezultat.status == LICENTA_STATUS_REVOCATA:
        raise EroareLicenta("licenta_revocata")
    if rezultat.status == LICENTA_STATUS_PRODUS_INVALID:
        raise EroareLicenta("licenta_produs_invalid")
    if rezultat.status == LICENTA_STATUS_ACTIVATION_LIMIT:
        raise EroareLicenta("licenta_limita_activari")

    raise EroareLicenta(rezultat.mesaj or "licenta_necunoscuta")


async def async_salveaza_licenta_in_intrare(
    hass: HomeAssistant,
    intrare: ConfigEntry,
    rezultat: RezultatLicenta,
) -> None:
    utilizator, cheie, _storage = await async_obtine_context_licenta(hass, intrare=intrare)
    await async_salveaza_licenta_globala(hass, cheie, utilizator, rezultat)
    hass.config_entries.async_update_entry(
        intrare,
        data={**intrare.data, DATE_VERIFICARE_LICENTA: rezultat.ca_dict()},
    )


def _acum_utc_iso() -> str:
    return datetime.now(UTC).isoformat()