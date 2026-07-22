from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from homeassistant.components import persistent_notification, websocket_api
import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_FURNIZOR,
    CONF_PREMISE_LABEL,
    CONF_ACCOUNT_ID,
    CONF_CONTRACT_ID,
    CONF_MOBILE_NOTIFY_SERVICE,
    DOMENIU,
    PLATFORME,
    FURNIZOR_ADMIN_GLOBAL,
    SERVICIU_RELOAD_ALL,
    SERVICIU_OPEN_PROVIDER,
    SERVICIU_SET_INVOICE_STATUS,
    SERVICIU_SUBMIT_READING,
    SERVICIU_SET_NOTIFICATION_PREFERENCES,
    SERVICIU_SET_CONSUMPTION_POINT_VISIBILITY,
    SERVICIU_SET_DISTRIBUTION_SUPPLIER_LINKS,
)
from .coordonator import CoordonatorUtilitatiRomania
from .grupare_facturi import async_incarca_grupari_facturi
from .locuri_ignorate import (
    async_incarca_locuri_ignorate,
    async_seteaza_loc_consum_ignorat,
)
from .facturi_status_manual import (
    async_incarca_statusuri_facturi_manuale,
    async_seteaza_status_manual_factura,
)
from .deer_device import alias_loc_deer, slug_loc_deer
from .eon_device import alias_loc_eon, slug_loc_eon
from .hidro_device import alias_loc_consum, slug_loc_consum
from .myelectrica_device import alias_loc_myelectrica, slug_loc_myelectrica
from .ebloc_device import alias_loc_ebloc, slug_loc_ebloc
from .naming import build_provider_slug, extract_street_slug
from .storage_citiri import async_salveaza_citire
from .notificari import async_salveaza_preferinte_notificari
from .asocieri_distributie import (
    async_incarca_asocieri_distributie,
    async_salveaza_asocieri_distributie,
)
from .licentiere import async_salveaza_licenta_in_intrare, async_verifica_licenta

_LOGGER = logging.getLogger(__name__)

_FRONTEND_VERSION = "1.17.1b2"
_LOVELACE_RESOURCE_BASE_URL = "/utilitati_romania/utilitati_romania-card.js"
_PANEL_RESOURCE_BASE_URL = "/utilitati_romania/utilitati-romania-panel.js"
_LOVELACE_RESOURCE_URL = f"{_LOVELACE_RESOURCE_BASE_URL}?v={_FRONTEND_VERSION}"
_PANEL_RESOURCE_URL = f"{_PANEL_RESOURCE_BASE_URL}?v={_FRONTEND_VERSION}"
_LOVELACE_NOTIFICATION_ID = "utilitati_romania_card_resource"
_ADMIN_PLATFORME = [Platform.SENSOR, Platform.BUTTON, Platform.TEXT, Platform.SELECT]



@websocket_api.websocket_command({vol.Required("type"): "utilitati_romania/distribution_supplier_links"})
@websocket_api.async_response
async def _websocket_distribution_supplier_links(hass, connection, msg):
    links = await async_incarca_asocieri_distributie(hass)
    connection.send_result(msg["id"], {"links": links})


@websocket_api.websocket_command({vol.Required("type"): "utilitati_romania/dashboard_payload"})
@websocket_api.async_response
async def _websocket_dashboard_payload(hass, connection, msg):
    domain_data = hass.data.get(DOMENIU, {})
    payload = domain_data.get("dashboard_facturi_payload")
    if not isinstance(payload, dict):
        payload = {"locatii": []}
    connection.send_result(msg["id"], payload)

def _slug_legacy(text: str | None) -> str:
    value = str(text or "cont").lower()
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_")[:100] or "cont"




def _mesaj_eroare_engie(data) -> str | None:
    if not isinstance(data, dict):
        return None

    mesaje: list[str] = []

    def adauga(value) -> None:
        if value in (None, "") or isinstance(value, bool):
            return
        text = str(value).strip()
        if text and text.lower() not in {"true", "false", "none", "null"} and text not in mesaje:
            mesaje.append(text)

    errors = data.get("errors")
    if isinstance(errors, dict):
        for cheie in ("erori", "error", "message", "errorMessage", "description"):
            adauga(errors.get(cheie))
        for value in errors.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        for cheie in ("erori", "error", "message", "errorMessage", "description"):
                            adauga(item.get(cheie))
                    else:
                        adauga(item)
    elif isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                for cheie in ("erori", "error", "message", "errorMessage", "description"):
                    adauga(item.get(cheie))
            else:
                adauga(item)
    else:
        adauga(errors)

    for cheie in ("message", "error_description", "error", "detail", "title"):
        adauga(data.get(cheie))

    return "; ".join(mesaje) if mesaje else None


def _engie_index_ascuns(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
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


APA_CANAL_OBJECT_KEY_MAP = {
    "last_consumption": "ultimul_consum",
    "last_meter_reading": "ultimul_index",
    "current_balance": "sold_curent",
    "last_invoice": "ultima_factura",
    "last_payment": "ultima_plata",
}


def _safe_entity_id(domain: str, object_id: str) -> str:
    value = str(object_id or "").lower()

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
        value = value.replace(src, dst)

    normalized: list[str] = []
    for ch in value:
        if ch.isalnum() or ch == "_":
            normalized.append(ch)
        else:
            normalized.append("_")

    value = "".join(normalized)

    while "__" in value:
        value = value.replace("__", "_")

    value = value.strip("_")[:240] or "entitate"

    return f"{domain}.{value}"


async def _async_register_static_paths(hass: HomeAssistant) -> None:
    hass.data.setdefault(DOMENIU, {})
    if hass.data[DOMENIU].get("_static_paths_registered"):
        return

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                "/utilitati_romania",
                hass.config.path("custom_components", "utilitati_romania", "www"),
                cache_headers=False,
            )
        ]
    )

    hass.data[DOMENIU]["_static_paths_registered"] = True


def _async_register_dashboard_panel(hass: HomeAssistant) -> None:
    hass.data.setdefault(DOMENIU, {})
    if hass.data[DOMENIU].get("_dashboard_panel_registered"):
        return

    try:
        from homeassistant.components.frontend import async_register_built_in_panel
    except Exception:
        _LOGGER.exception("Nu am putut importa funcția de înregistrare a panoului frontend")
        return

    try:
        async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="Utilități România",
            sidebar_icon="mdi:receipt-text-outline",
            frontend_url_path="utilitati-romania",
            require_admin=False,
            config={
                "_panel_custom": {
                    "name": "utilitati-romania-panel",
                    "module_url": _PANEL_RESOURCE_URL,
                },
                "domain": DOMENIU,
                "summary_entity": "sensor.administrare_integrare_facturi_utilitati",
            },
        )
        hass.data[DOMENIU]["_dashboard_panel_registered"] = True
    except Exception:
        _LOGGER.exception("Nu am putut înregistra panoul Utilități România")


async def _extract_lovelace_resource_urls_from_storage(hass: HomeAssistant) -> set[str]:
    storage_path = Path(hass.config.path(".storage", "lovelace_resources"))
    if not storage_path.exists():
        return set()

    def _read_file() -> dict | list:
        try:
            return json.loads(storage_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    raw = await asyncio.to_thread(_read_file)

    items = []
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict):
            maybe_items = data.get("items")
            if isinstance(maybe_items, list):
                items = maybe_items
        elif isinstance(raw.get("items"), list):
            items = raw.get("items") or []

    urls: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            urls.add(url.strip())

    return urls


def _frontend_resource_matches(url: str, base_url: str) -> bool:
    clean_url = (url or "").strip()
    return clean_url == base_url or clean_url.startswith(f"{base_url}?")


def _storage_lovelace_mode_likely(hass: HomeAssistant) -> bool:
    return (
        Path(hass.config.path(".storage", "lovelace_resources")).exists()
        or Path(hass.config.path(".storage", "lovelace_dashboards")).exists()
        or Path(hass.config.path(".storage", "lovelace")).exists()
    )


def _resource_registered_in_memory(hass: HomeAssistant, url: str) -> bool:
    lovelace_data = hass.data.get("lovelace")
    if not isinstance(lovelace_data, dict):
        return False

    resources = lovelace_data.get("resources")
    if resources is None:
        return False

    try:
        items = resources.async_items()
    except Exception:
        return False

    for item in items:
        if not isinstance(item, dict):
            continue
        item_url = item.get("url")
        if isinstance(item_url, str) and item_url.strip() == url:
            return True

    return False


async def _async_notify_missing_lovelace_resource(hass: HomeAssistant) -> None:
    hass.data.setdefault(DOMENIU, {})

    if hass.data[DOMENIU].get("_resource_notification_checked"):
        return

    hass.data[DOMENIU]["_resource_notification_checked"] = True

    if _resource_registered_in_memory(hass, _LOVELACE_RESOURCE_URL) or _resource_registered_in_memory(hass, _LOVELACE_RESOURCE_BASE_URL):
        persistent_notification.async_dismiss(hass, _LOVELACE_NOTIFICATION_ID)
        return

    stored_urls = await _extract_lovelace_resource_urls_from_storage(hass)
    if any(_frontend_resource_matches(url, _LOVELACE_RESOURCE_BASE_URL) for url in stored_urls):
        persistent_notification.async_dismiss(hass, _LOVELACE_NOTIFICATION_ID)
        return

    if not _storage_lovelace_mode_likely(hass):
        return

    persistent_notification.async_create(
        hass,
        (
            "Cardul Lovelace pentru **Utilități România** este livrat deja de integrare, "
            "dar resursa frontend nu este încă adăugată în dashboard.\n\n"
            "**Adaugă această resursă:**\n"
            f"`{_LOVELACE_RESOURCE_URL}`\n\n"
            "**Type:** `module`\n\n"
            "Pași:\n"
            "Settings → Dashboards → Resources → Add Resource"
        ),
        title="Utilități România",
        notification_id=_LOVELACE_NOTIFICATION_ID,
    )


def _async_get_admin_entry(hass: HomeAssistant) -> ConfigEntry | None:
    for existing_entry in hass.config_entries.async_entries(DOMENIU):
        if existing_entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
            return existing_entry
    return None


async def _async_ensure_admin_entry(hass: HomeAssistant, source_entry: ConfigEntry) -> None:
    if _async_get_admin_entry(hass) is not None:
        return

    lock = hass.data[DOMENIU].setdefault("_admin_entry_lock", asyncio.Lock())
    async with lock:
        if _async_get_admin_entry(hass) is not None:
            return

        user_input = {
            "utilizator": str(
                source_entry.options.get(
                    "utilizator",
                    source_entry.data.get("utilizator", ""),
                )
            ).strip(),
            "cheie_licenta": (
                str(
                    source_entry.options.get(
                        "cheie_licenta",
                        source_entry.data.get("cheie_licenta", "TRIAL"),
                    )
                ).strip()
                or "TRIAL"
            ),
        }

        await hass.config_entries.flow.async_init(
            DOMENIU,
            context={"source": "admin_bootstrap"},
            data=user_input,
        )


async def _async_reload_all_entries(hass: HomeAssistant) -> None:
    for existing_entry in list(hass.config_entries.async_entries(DOMENIU)):
        if existing_entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
            continue
        await hass.config_entries.async_reload(existing_entry.entry_id)


def _admin_notify_select_entity_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_admin_dispozitiv_mobil_open_provider"
    return registry.async_get_entity_id("select", DOMENIU, unique_id)


def _provider_open_target(provider: str | None) -> dict[str, str] | None:
    key = str(provider or "").strip().lower()

    if key == "digi":
        return {
            "mode": "launch_app",
            "package_name": "ro.rcsrds.mydigi",
            "fallback": "https://www.digi.ro/my-account/invoices",
        }

    if key == "eon":
        return {
            "mode": "launch_app",
            "package_name": "ro.eon.myline",
            "fallback": "https://www.eon.ro/myline/login",
        }

    if key == "myelectrica":
        return {
            "mode": "launch_app",
            "package_name": "ro.tremend.electrica",
            "fallback": "https://myelectrica.ro/",
        }

    if key == "hidroelectrica":
        return {
            "mode": "launch_app",
            "package_name": "com.sew.hidroelectrica",
            "fallback": "https://client.hidroelectrica.ro/",
        }

    if key == "nova":
        return {
            "mode": "launch_app",
            "package_name": "com.nova.mobile",
            "fallback": "https://nova-energy.ro/",
        }

    if key == "apa_canal":
        return {
            "mode": "url",
            "fallback": "https://portal.apacansb.ro/sap/bc/ui5_ui5/sap/UMCUI5_MOBILE/",
        }

    if key == "deer":
        return {
            "mode": "url",
            "fallback": "https://datemasura.distributie-energie.ro/date_ee/do?action=loginForm",
        }

    if key == "ebloc":
        return {
            "mode": "launch_app",
            "package_name": "com.xisoft.ebloc.ro",
            "fallback": "https://www.e-bloc.ro/",
        }

    if key == "orange":
        return {
            "mode": "launch_app",
            "package_name": "com.orange.contultauorange",
            "fallback": "https://www.orange.ro/myaccount/",
        }

    if key == "rervest":
        return {
            "mode": "url",
            "fallback": "https://rervest.ro/contul-meu/",
        }

    if key == "aquatim":
        return {
            "mode": "launch_app",
            "package_name": "com.aquatim.dev",
            "fallback": "https://self.aquatim.ro/",
        }

    if key == "retim":
        return {
            "mode": "url",
            "fallback": "https://retim.ro/contul-meu/",
        }

    if key == "comprest":
        return {
            "mode": "url",
            "fallback": "https://client.comprest.ro/",
        }

    if key == "apa_oradea":
        return {
            "mode": "url",
            "fallback": "https://plataonline.apaoradea.ro/",
        }

    if key == "aparegio":
        return {
            "mode": "url",
            "fallback": "https://aparegio.emsys.ro/CUSTOMER_PORTAL/login.jsp",
        }

    if key == "polaris":
        return {
            "mode": "url",
            "fallback": "https://my.polaris.ro/Login.aspx",
        }

    if key == "hidro_prahova":
        return {
            "mode": "url",
            "fallback": "https://www.client-hph.ro/index.php?action=login",
        }

    return None


def _async_ensure_services(hass: HomeAssistant) -> None:
    if hass.data[DOMENIU].get("_services_registered"):
        return

    async def _async_handle_reload_all(call: ServiceCall) -> None:
        await _async_reload_all_entries(hass)

    async def _async_handle_open_provider(call: ServiceCall) -> None:
        provider = str(call.data.get("provider") or "").strip().lower()
        target = _provider_open_target(provider)
        if not target:
            persistent_notification.async_create(
                hass,
                f"Nu există încă o destinație configurată pentru furnizorul **{provider or '-'}**.",
                title="Utilități România",
                notification_id="utilitati_romania_open_provider_missing_target",
            )
            return

        admin_entry = _async_get_admin_entry(hass)
        if admin_entry is None:
            persistent_notification.async_create(
                hass,
                "Nu există intrarea de administrare a integrării.",
                title="Utilități România",
                notification_id="utilitati_romania_open_provider_missing_admin",
            )
            return

        selected_notify_service = str(admin_entry.options.get(CONF_MOBILE_NOTIFY_SERVICE) or "").strip()
        select_entity_id = _admin_notify_select_entity_id(hass, admin_entry)
        if (not selected_notify_service or selected_notify_service == "none") and select_entity_id:
            selected_state = hass.states.get(select_entity_id)
            selected_notify_service = str(selected_state.state if selected_state else "").strip()

        if not selected_notify_service or selected_notify_service == "none":
            persistent_notification.async_create(
                hass,
                (
                    "Selectează mai întâi un dispozitiv mobil în secțiunea **Administrare integrare** "
                    "→ **Dispozitiv mobil pentru deschidere furnizori**."
                ),
                title="Utilități România",
                notification_id="utilitati_romania_open_provider_missing_device",
            )
            return

        notify_services = hass.services.async_services().get("notify", {})
        if selected_notify_service not in notify_services:
            persistent_notification.async_create(
                hass,
                (
                    f"Serviciul de notificare **notify.{selected_notify_service}** nu mai este disponibil. "
                    "Alege din nou dispozitivul mobil în Administrare integrare."
                ),
                title="Utilități România",
                notification_id="utilitati_romania_open_provider_invalid_device",
            )
            return

        if target.get("mode") == "launch_app" and target.get("package_name"):
            try:
                await hass.services.async_call(
                    "notify",
                    selected_notify_service,
                    {
                        "message": "command_launch_app",
                        "data": {
                            "package_name": target["package_name"],
                        },
                    },
                    blocking=True,
                )
                return
            except Exception:
                _LOGGER.exception("Nu am putut lansa aplicația pentru furnizorul %s", provider)

        fallback = target.get("fallback")
        if fallback:
            try:
                await hass.services.async_call(
                    "notify",
                    selected_notify_service,
                    {
                        "message": "command_activity",
                        "data": {
                            "intent_action": "android.intent.action.VIEW",
                            "intent_uri": fallback,
                        },
                    },
                    blocking=True,
                )
                return
            except Exception:
                _LOGGER.exception("Nu am putut deschide fallback-ul web pentru furnizorul %s", provider)

        persistent_notification.async_create(
            hass,
            (
                f"Nu am putut deschide furnizorul **{provider}** pe dispozitivul selectat. "
                "Verifică aplicația Home Assistant Companion și permisiunile de notificare."
            ),
            title="Utilități România",
            notification_id="utilitati_romania_open_provider_failed",
        )

    async def _async_handle_set_invoice_status(call: ServiceCall) -> None:
        entry_id = str(call.data.get("entry_id") or "").strip()
        provider = str(call.data.get("provider") or "").strip().lower()
        status = str(call.data.get("status") or "").strip().lower()

        if not entry_id or not provider:
            raise ValueError("entry_id și provider sunt obligatorii.")

        if status not in {"paid", "clear"}:
            raise ValueError("status trebuie să fie paid sau clear.")

        ok = await async_seteaza_status_manual_factura(
            hass,
            entry_id=entry_id,
            furnizor=provider,
            id_cont=call.data.get("id_cont"),
            invoice_id=call.data.get("invoice_id"),
            invoice_title=call.data.get("invoice_title"),
            issue_date=call.data.get("issue_date"),
            amount=call.data.get("amount"),
            currency=call.data.get("currency"),
            status=("paid" if status == "paid" else None),
        )
        if not ok:
            raise ValueError("Factura nu a putut fi identificată pentru marcarea manuală.")

        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": ["sensor.administrare_integrare_facturi_utilitati"]},
            blocking=False,
        )


    async def _async_handle_set_consumption_point_visibility(call: ServiceCall) -> None:
        ignored = bool(call.data.get("ignored", False))
        key = await async_seteaza_loc_consum_ignorat(
            hass,
            cheie=call.data.get("cheie"),
            entry_id=call.data.get("entry_id"),
            furnizor=call.data.get("provider") or call.data.get("furnizor"),
            id_cont=call.data.get("id_cont"),
            id_contract=call.data.get("id_contract"),
            locatie_cheie=call.data.get("locatie_cheie"),
            eticheta=call.data.get("eticheta") or call.data.get("label"),
            ignored=ignored,
        )
        if not key:
            raise ValueError("Locul de consum nu a putut fi identificat.")

        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": ["sensor.administrare_integrare_facturi_utilitati"]},
            blocking=False,
        )


    async def _async_handle_set_notification_preferences(call: ServiceCall) -> None:
        preferinte = {
            "facturi_noi": call.data.get("facturi_noi", True),
            "scadente": call.data.get("scadente", True),
            "indexuri": call.data.get("indexuri", True),
            "praguri_scadenta": call.data.get("praguri_scadenta", [5, 3, 1]),
        }
        await async_salveaza_preferinte_notificari(hass, preferinte)


    async def _async_handle_submit_reading(call: ServiceCall) -> None:
        provider = str(call.data.get("provider") or "").strip().lower()
        entry_id = str(call.data.get("entry_id") or "").strip()
        id_cont = str(call.data.get("id_cont") or "").strip()
        id_contract = str(call.data.get("id_contract") or "").strip()
        raw_value = call.data.get("value")

        if provider not in {"apa_canal", "engie"}:
            raise ValueError("Serviciul de transmitere directă acceptă momentan doar furnizorii Apă Canal Sibiu și ENGIE.")

        try:
            index_value = int(float(str(raw_value).replace(",", ".")))
        except (TypeError, ValueError) as err:
            raise ValueError("Valoarea indexului introdus nu este validă.") from err

        if index_value < 0:
            raise ValueError("Valoarea indexului introdus nu poate fi negativă.")

        coordonator = None
        for value in hass.data.get(DOMENIU, {}).values():
            if not isinstance(value, CoordonatorUtilitatiRomania):
                continue
            data = getattr(value, "data", None)
            if data is None or getattr(data, "furnizor", None) != provider:
                continue
            if entry_id and getattr(value.intrare, "entry_id", None) != entry_id:
                continue
            coordonator = value
            break

        if coordonator is None or getattr(coordonator, "data", None) is None:
            nume_furnizor = "ENGIE" if provider == "engie" else "Apă Canal Sibiu"
            raise ValueError(f"Nu am găsit intrarea {nume_furnizor} pentru transmiterea indexului.")

        cont_selectat = None
        for cont in getattr(coordonator.data, "conturi", []) or []:
            cont_id = str(getattr(cont, "id_cont", "") or "").strip()
            contract_id = str(getattr(cont, "id_contract", "") or "").strip()
            if id_cont and cont_id == id_cont:
                cont_selectat = cont
                break
            if id_contract and contract_id == id_contract:
                cont_selectat = cont
                break

        if cont_selectat is None:
            nume_furnizor = "ENGIE" if provider == "engie" else "Apă Canal Sibiu"
            raise ValueError(f"Nu am găsit contractul {nume_furnizor} pentru transmiterea indexului.")

        if provider == "engie":
            raw = getattr(cont_selectat, "date_brute", None) or {}
            poc, division, installation = _engie_date_tehnice_index(raw, cont_selectat)
            alias = getattr(cont_selectat, "nume", None) or getattr(cont_selectat, "adresa", None) or id_cont

            if not poc or not division or not installation:
                mesaj = "Nu am putut identifica datele tehnice necesare pentru transmiterea indexului ENGIE."
                persistent_notification.async_create(
                    hass,
                    mesaj,
                    title="Utilități România – ENGIE",
                    notification_id=f"utilitati_romania_engie_trimite_index_eroare_{getattr(cont_selectat, 'id_cont', id_cont)}",
                )
                raise ValueError(mesaj)

            if _engie_index_ascuns(raw):
                mesaj = "ENGIE nu permite transmiterea indexului pentru acest loc de consum în acest moment."
                persistent_notification.async_create(
                    hass,
                    mesaj,
                    title="Utilități România – ENGIE",
                    notification_id=f"utilitati_romania_engie_trimite_index_eroare_{getattr(cont_selectat, 'id_cont', id_cont)}",
                )
                raise ValueError(mesaj)

            api = getattr(coordonator.client, "api", None)
            transmitere = getattr(api, "async_transmite_index", None)
            if not callable(transmitere):
                mesaj = "Clientul ENGIE nu are disponibilă metoda de transmitere index."
                persistent_notification.async_create(
                    hass,
                    mesaj,
                    title="Utilități România – ENGIE",
                    notification_id=f"utilitati_romania_engie_trimite_index_eroare_{getattr(cont_selectat, 'id_cont', id_cont)}",
                )
                raise ValueError(mesaj)

            rezultat = await transmitere(poc, division, installation, index_value)
            if not isinstance(rezultat, dict):
                mesaj = "Transmiterea indexului ENGIE nu a returnat un răspuns valid."
                persistent_notification.async_create(
                    hass,
                    mesaj,
                    title="Utilități România – ENGIE",
                    notification_id=f"utilitati_romania_engie_trimite_index_eroare_{getattr(cont_selectat, 'id_cont', id_cont)}",
                )
                raise ValueError(mesaj)

            if rezultat.get("error") is True or int(rezultat.get("http_status") or 200) >= 400:
                mesaj = _mesaj_eroare_engie(rezultat) or "ENGIE a refuzat transmiterea indexului."
                persistent_notification.async_create(
                    hass,
                    mesaj,
                    title="Utilități România – ENGIE",
                    notification_id=f"utilitati_romania_engie_trimite_index_eroare_{getattr(cont_selectat, 'id_cont', id_cont)}",
                )
                raise ValueError(mesaj)

            await async_salveaza_citire(
                hass,
                "engie",
                getattr(cont_selectat, "id_cont", None),
                float(index_value),
                sursa="panel",
                extra={
                    "poc": poc,
                    "division": division,
                    "installation_number": installation,
                    "unitate": "m³" if division == "gaz" else "kWh",
                },
            )

            persistent_notification.async_create(
                hass,
                f"Indexul **{index_value}** a fost transmis cu succes pentru **{alias}**.",
                title="Utilități România – ENGIE",
                notification_id=f"utilitati_romania_engie_trimite_index_{getattr(cont_selectat, 'id_cont', id_cont)}",
            )
            await coordonator.async_request_refresh()
            return

        raw = getattr(cont_selectat, "date_brute", None) or {}
        window = raw.get("meter_reading_window") or {}
        registers = window.get("registers") or []
        registru = registers[0] if registers else {}

        contract_id = str(getattr(cont_selectat, "id_contract", None) or id_contract or "").strip()
        device_id = str(registru.get("device_id") or "").strip()
        register_id = str(registru.get("register_id") or "").strip()

        if not contract_id or not device_id or not register_id:
            raise ValueError("Nu am putut identifica datele tehnice necesare pentru transmiterea indexului Apă Canal Sibiu.")

        rezultat = await coordonator.client.async_transmite_index(
            contract_id,
            device_id,
            register_id,
            index_value,
        )
        if not isinstance(rezultat, dict):
            raise ValueError("Transmiterea indexului Apă Canal Sibiu nu a returnat un răspuns valid.")

        await async_salveaza_citire(
            hass,
            "apa_canal",
            getattr(cont_selectat, "id_cont", None),
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
            hass,
            f"Indexul **{index_value}** a fost transmis cu succes pentru **{getattr(cont_selectat, 'nume', None) or getattr(cont_selectat, 'adresa', None) or id_cont}**.",
            title="Utilități România – Apă Canal Sibiu",
            notification_id=f"utilitati_romania_apa_canal_trimite_index_{getattr(cont_selectat, 'id_cont', id_cont)}",
        )

        await coordonator.async_request_refresh()

    hass.services.async_register(DOMENIU, SERVICIU_RELOAD_ALL, _async_handle_reload_all)
    hass.services.async_register(DOMENIU, SERVICIU_OPEN_PROVIDER, _async_handle_open_provider)
    hass.services.async_register(DOMENIU, SERVICIU_SET_INVOICE_STATUS, _async_handle_set_invoice_status)
    hass.services.async_register(DOMENIU, SERVICIU_SUBMIT_READING, _async_handle_submit_reading)
    hass.services.async_register(DOMENIU, SERVICIU_SET_NOTIFICATION_PREFERENCES, _async_handle_set_notification_preferences)
    async def _async_handle_set_distribution_supplier_links(call: ServiceCall) -> None:
        links = call.data.get("links") or {}
        if not isinstance(links, dict):
            raise ValueError("Asocierile distribuitor-furnizor trebuie sa fie un obiect JSON.")
        await async_salveaza_asocieri_distributie(hass, links)

    hass.services.async_register(DOMENIU, SERVICIU_SET_CONSUMPTION_POINT_VISIBILITY, _async_handle_set_consumption_point_visibility)
    hass.services.async_register(DOMENIU, SERVICIU_SET_DISTRIBUTION_SUPPLIER_LINKS, _async_handle_set_distribution_supplier_links)
    websocket_api.async_register_command(hass, _websocket_distribution_supplier_links)
    websocket_api.async_register_command(hass, _websocket_dashboard_payload)
    hass.data[DOMENIU]["_services_registered"] = True


def _async_remove_services_if_unused(hass: HomeAssistant) -> None:
    remaining = [e for e in hass.config_entries.async_entries(DOMENIU) if e.state is not None]
    if remaining:
        return
    if hass.services.has_service(DOMENIU, SERVICIU_RELOAD_ALL):
        hass.services.async_remove(DOMENIU, SERVICIU_RELOAD_ALL)
    if hass.services.has_service(DOMENIU, SERVICIU_OPEN_PROVIDER):
        hass.services.async_remove(DOMENIU, SERVICIU_OPEN_PROVIDER)
    if hass.services.has_service(DOMENIU, SERVICIU_SET_INVOICE_STATUS):
        hass.services.async_remove(DOMENIU, SERVICIU_SET_INVOICE_STATUS)
    if hass.services.has_service(DOMENIU, SERVICIU_SUBMIT_READING):
        hass.services.async_remove(DOMENIU, SERVICIU_SUBMIT_READING)
    if hass.services.has_service(DOMENIU, SERVICIU_SET_DISTRIBUTION_SUPPLIER_LINKS):
        hass.services.async_remove(DOMENIU, SERVICIU_SET_DISTRIBUTION_SUPPLIER_LINKS)
    if hass.services.has_service(DOMENIU, SERVICIU_SET_NOTIFICATION_PREFERENCES):
        hass.services.async_remove(DOMENIU, SERVICIU_SET_NOTIFICATION_PREFERENCES)
    if hass.services.has_service(DOMENIU, SERVICIU_SET_CONSUMPTION_POINT_VISIBILITY):
        hass.services.async_remove(DOMENIU, SERVICIU_SET_CONSUMPTION_POINT_VISIBILITY)
    hass.data[DOMENIU]["_services_registered"] = False


async def _async_cleanup_admin_registry_links(hass: HomeAssistant) -> None:
    """Curăță legăturile vechi de registry după mutarea grupărilor de facturi.

    Obiective:
    - păstrăm un singur device principal „Administrare integrare”
    - păstrăm un singur device „Grupare facturi”
    - scoatem aceste device-uri din secțiunile furnizorilor dacă au rămas legate acolo
    - ștergem entitățile vechi de grupare rămase pe device-ul principal de administrare
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    admin_entry_ids = {
        existing_entry.entry_id
        for existing_entry in hass.config_entries.async_entries(DOMENIU)
        if existing_entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL
    }
    if not admin_entry_ids:
        return

    admin_device_ids: set[str] = set()
    grouping_device_ids: set[str] = set()

    for device in list(device_registry.devices.values()):
        identifiers = set(device.identifiers or set())

        for raw_identifier in identifiers:
            if not isinstance(raw_identifier, tuple) or len(raw_identifier) < 2:
                continue

            domain = raw_identifier[0]
            identifier = raw_identifier[1]

            if domain == DOMENIU and identifier in admin_entry_ids:
                admin_device_ids.add(device.id)

            if domain == DOMENIU and identifier == "grupare_facturi":
                grouping_device_ids.add(device.id)

    for device_id in admin_device_ids:
        for entity_entry in list(
            er.async_entries_for_device(
                entity_registry,
                device_id,
                include_disabled_entities=True,
            )
        ):
            if (
                entity_entry.platform == DOMENIU
                and entity_entry.domain == "text"
                and "_grupare_facturi" in str(entity_entry.unique_id)
            ):
                try:
                    entity_registry.async_remove(entity_entry.entity_id)
                except Exception:
                    continue

    protected_device_ids = admin_device_ids | grouping_device_ids

    for device_id in protected_device_ids:
        device = device_registry.async_get(device_id)
        if device is None:
            continue

        linked_entry_ids = set(getattr(device, "config_entries", set()) or set())
        for linked_entry_id in list(linked_entry_ids):
            if linked_entry_id in admin_entry_ids:
                continue

            has_entities_for_linked_entry = False
            for entity_entry in er.async_entries_for_device(
                entity_registry,
                device_id,
                include_disabled_entities=True,
            ):
                if entity_entry.config_entry_id == linked_entry_id:
                    has_entities_for_linked_entry = True
                    break

            if has_entities_for_linked_entry:
                continue

            try:
                device_registry.async_update_device(
                    device_id,
                    remove_config_entry_id=linked_entry_id,
                )
            except Exception:
                continue

    for device in list(device_registry.devices.values()):
        if device.id in protected_device_ids:
            continue

        if device.name != "Administrare integrare":
            continue

        identifiers = set(device.identifiers or set())
        has_domain_identifier = any(
            isinstance(raw_identifier, tuple)
            and len(raw_identifier) >= 1
            and raw_identifier[0] == DOMENIU
            for raw_identifier in identifiers
        )
        if not has_domain_identifier:
            continue

        entities = list(
            er.async_entries_for_device(
                entity_registry,
                device.id,
                include_disabled_entities=True,
            )
        )

        if not entities:
            try:
                device_registry.async_remove_device(device.id)
            except Exception:
                pass
            continue

        removable = True
        for entity_entry in entities:
            if not (
                entity_entry.platform == DOMENIU
                and entity_entry.domain == "text"
                and "_grupare_facturi" in str(entity_entry.unique_id)
            ):
                removable = False
                break

        if not removable:
            continue

        for entity_entry in entities:
            try:
                entity_registry.async_remove(entity_entry.entity_id)
            except Exception:
                continue

        try:
            device_registry.async_remove_device(device.id)
        except Exception:
            continue


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


async def _async_verifica_licenta_la_pornire(hass: HomeAssistant, entry: ConfigEntry) -> None:
    try:
        rezultat = await async_verifica_licenta(hass, entry)
        if rezultat.valida or not rezultat.eroare_conectare:
            await async_salveaza_licenta_in_intrare(hass, entry, rezultat)
            await _async_actualizeaza_senzorii_licentei(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Verificarea licenței la pornire a eșuat: %s", err)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMENIU, {})
    _async_ensure_services(hass)
    _async_schedule_admin_reload_after_start(hass)
    await async_incarca_grupari_facturi(hass)
    await async_incarca_statusuri_facturi_manuale(hass)
    await async_incarca_locuri_ignorate(hass)
    await _async_register_static_paths(hass)
    _async_register_dashboard_panel(hass)
    await _async_notify_missing_lovelace_resource(hass)
    return True




def _nume_curat_apa_brasov_din_coordonator(entry: ConfigEntry, coordonator: CoordonatorUtilitatiRomania) -> str | None:
    if entry.data.get(CONF_FURNIZOR) != "apa_brasov" or coordonator.data is None:
        return None
    conturi = getattr(coordonator.data, "conturi", []) or []
    if not conturi:
        return None
    cont = conturi[0]
    nume = str(getattr(cont, "nume", None) or "").strip()
    if not nume:
        return None
    if nume.lower().startswith("selector"):
        return "Apă Brașov"
    return f"Apă Brașov - {nume}"


async def _async_curata_intrare_apa_brasov(hass: HomeAssistant, entry: ConfigEntry, coordonator: CoordonatorUtilitatiRomania) -> None:
    nume_curat = _nume_curat_apa_brasov_din_coordonator(entry, coordonator)
    if not nume_curat:
        return

    date_noi = dict(entry.data)
    cont = (getattr(coordonator.data, "conturi", []) or [None])[0]
    if cont is not None:
        date_noi[CONF_PREMISE_LABEL] = str(getattr(cont, "nume", None) or date_noi.get(CONF_PREMISE_LABEL) or "").strip()
        id_cont = str(getattr(cont, "id_cont", None) or "").strip()
        if id_cont:
            date_noi[CONF_ACCOUNT_ID] = id_cont
            date_noi[CONF_CONTRACT_ID] = id_cont

    if entry.title != nume_curat or date_noi != dict(entry.data):
        hass.config_entries.async_update_entry(entry, title=nume_curat, data=date_noi)

    device_registry = dr.async_get(hass)
    for device in list(device_registry.devices.values()):
        if (DOMENIU, entry.entry_id) not in set(device.identifiers or set()):
            continue
        try:
            device_registry.async_update_device(device.id, name=nume_curat)
        except TypeError:
            # Versiunile mai vechi de Home Assistant pot să nu permită setarea numelui aici.
            pass
        except Exception:
            _LOGGER.debug("Nu am putut actualiza numele device-ului Apă Brașov", exc_info=True)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMENIU, {})

    _async_ensure_services(hass)
    await async_incarca_grupari_facturi(hass)
    await async_incarca_statusuri_facturi_manuale(hass)
    await async_incarca_locuri_ignorate(hass)

    await _async_register_static_paths(hass)
    _async_register_dashboard_panel(hass)
    await _async_notify_missing_lovelace_resource(hass)

    if entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
        hass.data[DOMENIU][entry.entry_id] = {"admin": True}
        await hass.config_entries.async_forward_entry_setups(entry, _ADMIN_PLATFORME)
        hass.async_create_task(_async_verifica_licenta_la_pornire(hass, entry))
        await _async_cleanup_admin_registry_links(hass)
        return True

    await _async_ensure_admin_entry(hass, entry)

    coordonator = CoordonatorUtilitatiRomania(hass, entry)
    try:
        await coordonator.async_config_entry_first_refresh()
    except Exception:
        await coordonator.async_inchide()
        raise

    # Apă Brașov are separat un device de furnizor și device-uri pentru locații.
    # Nu modificăm automat titlul config entry-ului după datele primei locații,
    # deoarece asta redenumește greșit grupul furnizorului și poate produce duplicate
    # în device registry după mai multe beta-uri.
    await _migrare_unique_ids(hass, entry, coordonator)
    await _async_normalize_retele_electrice_entity_ids(hass, entry)
    hass.data[DOMENIU][entry.entry_id] = coordonator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORME)

    # Entitățile pentru „Grupare facturi” aparțin intrării globale de administrare.
    # Dacă un furnizor este adăugat după ce administrarea era deja încărcată,
    # adăugăm dinamic doar entitățile de grupare lipsă pentru acel furnizor.
    try:
        from .text import async_adauga_entitati_grupare_pentru_intrare

        await async_adauga_entitati_grupare_pentru_intrare(hass, entry.entry_id)
    except Exception:
        _LOGGER.exception(
            "Nu am putut adăuga entitățile de grupare facturi pentru %s",
            entry.title,
        )

    if entry.data.get(CONF_FURNIZOR) == "ebloc":
        await _async_force_migrare_entity_ids_ebloc(hass, entry, coordonator)
    await _async_cleanup_admin_registry_links(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
        descarcat = await hass.config_entries.async_unload_platforms(entry, _ADMIN_PLATFORME)
        if descarcat:
            hass.data[DOMENIU].pop(entry.entry_id, None)
        _async_remove_services_if_unused(hass)
        return descarcat

    coordonator = hass.data.get(DOMENIU, {}).get(entry.entry_id)

    descarcat = await hass.config_entries.async_unload_platforms(entry, PLATFORME)
    if descarcat:
        if coordonator is not None:
            await coordonator.async_inchide()
        hass.data[DOMENIU].pop(entry.entry_id, None)
    _async_remove_services_if_unused(hass)
    return descarcat


def _async_schedule_admin_reload_after_start(hass: HomeAssistant) -> None:
    hass.data.setdefault(DOMENIU, {})
    if hass.data[DOMENIU].get("_admin_reload_after_start_registered"):
        return

    async def _reload_admin(_event) -> None:
        admin_entry = _async_get_admin_entry(hass)
        if admin_entry is not None:
            await hass.config_entries.async_reload(admin_entry.entry_id)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _reload_admin)
    hass.data[DOMENIU]["_admin_reload_after_start_registered"] = True


def _migrare_senzori_hidro(entry_id: str, data) -> dict[str, tuple[str, str]]:
    from .sensor import SENZORI_CONT_HIDRO

    mapping: dict[str, tuple[str, str]] = {}
    for cont in data.conturi:
        alias_nou = alias_loc_consum(cont.nume, cont.adresa, cont.id_cont)
        slug_nou = slug_loc_consum(cont.id_cont, alias_nou, cont.adresa)
        old_slugs = {
            _slug_legacy(getattr(cont, "id_cont", None) or getattr(cont, "nume", None) or getattr(cont, "adresa", None)),
            build_provider_slug("hidro", getattr(cont, "adresa", None) or alias_nou, getattr(cont, "id_cont", None)),
            build_provider_slug("hidroelectrica", getattr(cont, "adresa", None) or alias_nou, getattr(cont, "id_cont", None)),
        }
        for descriere in SENZORI_CONT_HIDRO:
            new_unique = f"{entry_id}_hidro_{cont.id_cont}_{descriere.key}"
            new_object_id = f"hidro_{cont.id_cont}_{slug_nou}_{descriere.key}"
            mapping[new_unique] = (new_unique, new_object_id)
            mapping[f"{entry_id}_{slug_nou}_{descriere.key}"] = (new_unique, new_object_id)
            for old_slug in old_slugs:
                mapping[f"{entry_id}_hidro_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
                mapping[f"{entry_id}_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
    return mapping


def _migrare_senzori_eon(entry_id: str, data) -> dict[str, tuple[str, str]]:
    from .sensor import SENZORI_CONT_EON, SENZORI_CONT_EON_EXTINS, _an_curent_loc_eon

    mapping: dict[str, tuple[str, str]] = {}
    for cont in data.conturi:
        alias_nou = alias_loc_eon(cont.nume, cont.adresa, cont.id_cont)
        slug_nou = slug_loc_eon(cont.id_cont, alias_nou, cont.adresa)
        old_slugs = {
            _slug_legacy(getattr(cont, "id_cont", None) or getattr(cont, "nume", None) or "cont"),
            build_provider_slug("eon", getattr(cont, "adresa", None) or alias_nou, getattr(cont, "id_cont", None)),
            build_provider_slug("eon", getattr(cont, "nume", None) or alias_nou, getattr(cont, "id_cont", None)),
        }
        for descriere in SENZORI_CONT_EON:
            new_unique = f"{entry_id}_{slug_nou}_{descriere.key}"
            new_object_id = f"{slug_nou}_{descriere.key}"
            mapping[new_unique] = (new_unique, new_object_id)
            for old_slug in old_slugs:
                mapping[f"{entry_id}_eon_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
                mapping[f"{entry_id}_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
        an = _an_curent_loc_eon(cont)
        tip = getattr(cont, "tip_serviciu", None) or getattr(cont, "tip_utilitate", None) or "curent"
        for descriere in SENZORI_CONT_EON_EXTINS:
            suffix = an if descriere.key.startswith("arhiva_") else "base"
            if descriere.key == "arhiva_consum":
                object_suffix = f"arhiva_consum_{'gaz' if tip == 'gaz' else 'energie_electrica'}_{an}"
            elif descriere.key == "arhiva_index":
                object_suffix = f"arhiva_index_{'gaz' if tip == 'gaz' else 'energie_electrica'}_{an}"
            elif descriere.key == "arhiva_plati":
                object_suffix = f"arhiva_plati_{an}"
            else:
                object_suffix = descriere.key
            new_unique = f"{entry_id}_{slug_nou}_{descriere.key}_{suffix}"
            new_object_id = f"{slug_nou}_{object_suffix}"
            mapping[new_unique] = (new_unique, new_object_id)
            for old_slug in old_slugs:
                mapping[f"{entry_id}_eon_{old_slug}_{descriere.key}_{suffix}"] = (new_unique, new_object_id)
                mapping[f"{entry_id}_{old_slug}_{descriere.key}_{suffix}"] = (new_unique, new_object_id)
    return mapping


def _migrare_senzori_myelectrica(entry_id: str, data) -> dict[str, tuple[str, str]]:
    from .sensor import SENZORI_CONT_MYELECTRICA

    mapping: dict[str, tuple[str, str]] = {}
    for cont in data.conturi:
        alias_nou = alias_loc_myelectrica(cont.nume, cont.adresa, cont.id_cont)
        slug_nou = slug_loc_myelectrica(cont.id_cont, alias_nou, cont.adresa)
        alias_vechi = str(getattr(cont, "adresa", None) or "").split(",")[0].strip() or str(getattr(cont, "nume", None) or f"NLC {cont.id_cont}")
        old_slugs = {
            _slug_legacy(f"{cont.id_cont}_{alias_vechi}"),
            build_provider_slug("myelectrica", getattr(cont, "adresa", None) or alias_nou, getattr(cont, "id_cont", None)),
            build_provider_slug("myelectrica", getattr(cont, "nume", None) or alias_nou, getattr(cont, "id_cont", None)),
        }
        for descriere in SENZORI_CONT_MYELECTRICA:
            new_unique = f"{entry_id}_{slug_nou}_{descriere.key}"
            new_object_id = f"{slug_nou}_{descriere.key}"
            mapping[new_unique] = (new_unique, new_object_id)
            for old_slug in old_slugs:
                mapping[f"{entry_id}_myelectrica_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
                mapping[f"{entry_id}_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
    return mapping


def _migrare_senzori_deer(entry_id: str, data) -> dict[str, tuple[str, str]]:
    from .sensor import SENZORI_CONT_DEER

    mapping: dict[str, tuple[str, str]] = {}
    for cont in data.conturi:
        alias_nou = alias_loc_deer(cont.nume, cont.adresa, cont.id_cont)
        slug_nou = slug_loc_deer(cont.id_cont, alias_nou, cont.adresa)
        old_slugs = {
            _slug_legacy(f"{cont.id_cont}_{getattr(cont, 'adresa', None) or getattr(cont, 'nume', None) or ''}"),
            build_provider_slug("deer", getattr(cont, "adresa", None), getattr(cont, "id_cont", None)),
            build_provider_slug("deer", getattr(cont, "nume", None), getattr(cont, "id_cont", None)),
        }
        street_only = extract_street_slug(getattr(cont, "adresa", None), getattr(cont, "id_cont", None))
        if street_only:
            old_slugs.add(f"deer_loc_{street_only}")
            old_slugs.add(f"deer_{street_only}")
        for descriere in SENZORI_CONT_DEER:
            new_unique = f"{entry_id}_{slug_nou}_{descriere.key}"
            new_object_id = f"{slug_nou}_{descriere.key}"
            mapping[new_unique] = (new_unique, new_object_id)
            for old_slug in old_slugs:
                mapping[f"{entry_id}_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
    return mapping


def _migrare_senzori_apa_canal(entry, data) -> dict[str, tuple[str, str]]:
    from .sensor import SENZORI_APA_CANAL

    premise_label = str(entry.data.get(CONF_PREMISE_LABEL) or entry.title or "contract").strip()
    slug_nou = build_provider_slug("apa_canal_sibiu", premise_label, premise_label)
    old_slugs = {
        "apa_canal",
        build_provider_slug("apa_canal_sibiu", premise_label, premise_label),
        _slug_legacy(f"apa_canal_sibiu_{premise_label}"),
    }
    mapping: dict[str, tuple[str, str]] = {}
    for descriere in SENZORI_APA_CANAL:
        object_key = APA_CANAL_OBJECT_KEY_MAP.get(descriere.key, descriere.key)
        new_unique = f"{entry.entry_id}_{slug_nou}_{object_key}"
        new_object_id = f"{slug_nou}_{object_key}"
        mapping[new_unique] = (new_unique, new_object_id)
        mapping[f"{entry.entry_id}_apa_canal_{descriere.key}"] = (new_unique, new_object_id)
        for old_slug in old_slugs:
            mapping[f"{entry.entry_id}_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
    return mapping



def _migrare_senzori_ebloc(entry_id: str, data) -> dict[str, tuple[str, str]]:
    from .sensor import SENZORI_CONT_EBLOC

    mapping: dict[str, tuple[str, str]] = {}

    for cont in data.conturi:
        alias_nou = alias_loc_ebloc(cont.nume, cont.adresa, cont.id_cont, cont=cont)
        slug_nou = slug_loc_ebloc(cont.id_cont, alias_nou, cont.adresa, cont=cont)

        old_slugs = {
            _slug_legacy(getattr(cont, "id_cont", None) or getattr(cont, "nume", None) or getattr(cont, "adresa", None)),
            build_provider_slug("ebloc", getattr(cont, "adresa", None) or alias_nou, getattr(cont, "id_cont", None)),
            build_provider_slug("ebloc", getattr(cont, "nume", None) or alias_nou, getattr(cont, "id_cont", None)),
        }

        for descriere in SENZORI_CONT_EBLOC:
            new_unique = f"{entry_id}_ebloc_{cont.id_cont}_{descriere.key}"
            new_object_id = f"{slug_nou}_{descriere.key}"
            mapping[new_unique] = (new_unique, new_object_id)

            for old_slug in old_slugs:
                mapping[f"{entry_id}_ebloc_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)
                mapping[f"{entry_id}_{old_slug}_{descriere.key}"] = (new_unique, new_object_id)

        numar_persoane_setare_unique = f"{entry_id}_ebloc_{cont.id_cont}_numar_persoane_setare"
        mapping[numar_persoane_setare_unique] = (
            numar_persoane_setare_unique,
            f"{slug_nou}_numar_persoane_setare",
        )

        trimite_numar_persoane_unique = f"{entry_id}_ebloc_{cont.id_cont}_trimite_numar_persoane"
        mapping[trimite_numar_persoane_unique] = (
            trimite_numar_persoane_unique,
            f"{slug_nou}_trimite_numar_persoane",
        )

    return mapping


def _cleanup_entitati_ebloc_scoase(registry, entry: ConfigEntry) -> None:
    """Șterge entitățile e-bloc scoase din modelul curent.

    Home Assistant păstrează în registry entitățile create anterior, chiar dacă platforma
    nu le mai creează. Pentru e-bloc, le eliminăm controlat după unique_id, ca să nu
    rămână senzori indisponibili după curățarea modelului de date.
    """
    chei_scoase = {
        "id_ultima_factura",
        "numar_facturi",
        "numar_plati",
        "valoare_ultima_factura",
        "sold_curent",
    }

    for entity_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if entity_entry.platform != DOMENIU or entity_entry.domain != "sensor":
            continue

        unique_id = str(entity_entry.unique_id or "")
        if f"{entry.entry_id}_ebloc_" not in unique_id:
            continue

        if any(unique_id.endswith(f"_{cheie}") for cheie in chei_scoase):
            try:
                registry.async_remove(entity_entry.entity_id)
            except Exception:
                continue



def _migrare_senzori_nova(entry_id: str, data) -> dict[str, tuple[str, str]]:
    from .sensor import SENZORI_REZUMAT, SENZORI_REZUMAT_FINANCIAR

    mapping: dict[str, tuple[str, str]] = {}
    conturi = data.conturi or []
    if len(conturi) == 1:
        slug = build_provider_slug("nova", getattr(conturi[0], "adresa", None), getattr(conturi[0], "id_cont", None))
    elif len(conturi) > 1:
        slug = "nova_multi"
    else:
        slug = "nova"
    for descriere in list(SENZORI_REZUMAT) + list(SENZORI_REZUMAT_FINANCIAR):
        new_unique = f"{entry_id}_{descriere.key}"
        new_object_id = f"{slug}_{descriere.key}"
        mapping[new_unique] = (new_unique, new_object_id)
    return mapping



async def _async_normalize_retele_electrice_entity_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Normalizează la lowercase entity_id-urile existente Rețele Electrice."""
    if entry.data.get(CONF_FURNIZOR) != "retele_electrice":
        return

    registry = er.async_get(hass)
    entries = list(er.async_entries_for_config_entry(registry, entry.entry_id))
    occupied_ids = {item.entity_id for item in entries}

    for entity_entry in entries:
        if entity_entry.platform != DOMENIU:
            continue
        if "_retele_electrice_" not in str(entity_entry.unique_id or ""):
            continue

        desired_entity_id = entity_entry.entity_id.lower()
        if desired_entity_id == entity_entry.entity_id:
            continue
        if desired_entity_id in occupied_ids:
            _LOGGER.warning(
                "Nu pot normaliza entity_id-ul Rețele Electrice %s deoarece %s există deja",
                entity_entry.entity_id,
                desired_entity_id,
            )
            continue

        try:
            registry.async_update_entity(
                entity_entry.entity_id,
                new_entity_id=desired_entity_id,
            )
            occupied_ids.discard(entity_entry.entity_id)
            occupied_ids.add(desired_entity_id)
        except Exception:
            _LOGGER.debug(
                "Nu am putut normaliza entity_id-ul Rețele Electrice %s",
                entity_entry.entity_id,
                exc_info=True,
            )


async def _async_force_migrare_entity_ids_ebloc(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordonator: CoordonatorUtilitatiRomania,
) -> None:
    """Forțează entity_id-urile e-bloc către naming-ul stabil nou.

    Unique ID-urile au rămas stabile ca să păstrăm istoricul, dar entity_id-urile vechi
    bazate pe nume/asociație trebuie redenumite automat.
    """
    data = coordonator.data
    if not data:
        return

    from .sensor import SENZORI_CONT_EBLOC

    registry = er.async_get(hass)
    entries = list(er.async_entries_for_config_entry(registry, entry.entry_id))

    def _entry_dupa_unique(unique_id: str):
        for existing in entries:
            if existing.platform == DOMENIU and existing.unique_id == unique_id:
                return existing
        return None

    def _entity_id_existent(entity_id: str):
        for existing in entries:
            if existing.entity_id == entity_id:
                return existing
        return None

    dorite: list[tuple[str, str, str]] = []

    for cont in data.conturi:
        alias = alias_loc_ebloc(cont.nume, cont.adresa, cont.id_cont, cont=cont)
        slug = slug_loc_ebloc(cont.id_cont, alias, cont.adresa, cont=cont)

        for descriere in SENZORI_CONT_EBLOC:
            dorite.append(
                (
                    "sensor",
                    f"{entry.entry_id}_ebloc_{cont.id_cont}_{descriere.key}",
                    f"sensor.{slug}_{descriere.key}",
                )
            )

        dorite.append(
            (
                "number",
                f"{entry.entry_id}_ebloc_{cont.id_cont}_numar_persoane_setare",
                f"number.{slug}_numar_persoane_setare",
            )
        )
        dorite.append(
            (
                "button",
                f"{entry.entry_id}_ebloc_{cont.id_cont}_trimite_numar_persoane",
                f"button.{slug}_trimite_numar_persoane",
            )
        )

    for domain, unique_id, entity_id_dorit in dorite:
        entity_entry = _entry_dupa_unique(unique_id)
        if entity_entry is None or entity_entry.domain != domain:
            continue

        if entity_entry.entity_id == entity_id_dorit:
            continue

        ocupat = _entity_id_existent(entity_id_dorit)
        if ocupat is not None and ocupat.entity_id != entity_entry.entity_id:
            # Dacă entitatea nouă există deja pentru alt unique_id, nu riscăm să stricăm registry-ul.
            continue

        try:
            registry.async_update_entity(entity_entry.entity_id, new_entity_id=entity_id_dorit)
        except Exception:
            _LOGGER.debug(
                "Nu am putut migra entity_id-ul e-bloc %s către %s",
                entity_entry.entity_id,
                entity_id_dorit,
                exc_info=True,
            )



async def _migrare_unique_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordonator: CoordonatorUtilitatiRomania,
) -> None:
    data = coordonator.data
    if not data:
        return

    furnizor = entry.data.get("furnizor")
    if furnizor == "hidroelectrica":
        mapping = _migrare_senzori_hidro(entry.entry_id, data)
    elif furnizor == "eon":
        mapping = _migrare_senzori_eon(entry.entry_id, data)
    elif furnizor == "myelectrica":
        mapping = _migrare_senzori_myelectrica(entry.entry_id, data)
    elif furnizor == "deer":
        mapping = _migrare_senzori_deer(entry.entry_id, data)
    elif furnizor == "apa_canal":
        mapping = _migrare_senzori_apa_canal(entry, data)
    elif furnizor == "nova":
        mapping = _migrare_senzori_nova(entry.entry_id, data)
    elif furnizor == "ebloc":
        mapping = _migrare_senzori_ebloc(entry.entry_id, data)
    else:
        return

    registry = er.async_get(hass)
    if furnizor == "ebloc":
        _cleanup_entitati_ebloc_scoase(registry, entry)

    entities = getattr(registry, "entities", {})
    entries = list(entities.values()) if hasattr(entities, "values") else []

    def _find_by_unique(domain: str, unique_id: str):
        for existing in entries:
            if (
                getattr(existing, "domain", None) == domain
                and getattr(existing, "platform", None) == DOMENIU
                and getattr(existing, "unique_id", None) == unique_id
            ):
                return existing
        return None

    def _find_by_entity_id(entity_id: str):
        for existing in entries:
            if getattr(existing, "entity_id", None) == entity_id:
                return existing
        return None

    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        mapped = mapping.get(entity_entry.unique_id)
        if not mapped:
            continue

        new_unique_id, new_object_id = mapped
        desired_entity_id = _safe_entity_id(entity_entry.domain, new_object_id)
        existing_target = _find_by_unique(entity_entry.domain, new_unique_id)

        if existing_target and existing_target.entity_id != entity_entry.entity_id:
            if hasattr(registry, "async_remove"):
                try:
                    registry.async_remove(entity_entry.entity_id)
                except Exception:
                    pass
            continue

        try:
            kwargs = {}
            if new_unique_id != entity_entry.unique_id:
                kwargs["new_unique_id"] = new_unique_id
            existing_entity_id = _find_by_entity_id(desired_entity_id)
            if desired_entity_id != entity_entry.entity_id and not existing_entity_id:
                kwargs["new_entity_id"] = desired_entity_id
            if kwargs:
                registry.async_update_entity(entity_entry.entity_id, **kwargs)
        except Exception:
            continue

    if furnizor == "deer":
        seen: dict[str, str] = {}
        for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity_entry.domain != "sensor":
                continue
            key = entity_entry.entity_id
            if not key.startswith("sensor.deer_"):
                continue
            if key in seen and hasattr(registry, "async_remove"):
                try:
                    registry.async_remove(entity_entry.entity_id)
                except Exception:
                    pass
            else:
                seen[key] = entity_entry.entity_id
