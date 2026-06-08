from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.helpers import entity_registry as er

from .const import DOMENIU, FURNIZOR_ADMIN_GLOBAL
from .coordonator import CoordonatorUtilitatiRomania
from .grupare_facturi import obtine_grupare_factura
from .facturi_status_manual import construieste_cheie_status_factura
from .helpers_facturi_locatie import (
    build_facturi_location_label,
    normalize_facturi_location_key,
)
from .modele import ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .naming import normalize_text


_PROVIDER_LABELS = {
    "apa_canal": "Apă Canal Sibiu",
    "apa_brasov": "Apă Brașov",
    "deer": "DEER",
    "digi": "Digi",
    "engie": "ENGIE",
    "eon": "E.ON",
    "ebloc": "e-bloc.ro",
    "hidroelectrica": "Hidroelectrica",
    "myelectrica": "myElectrica",
    "nova": "Nova",
    "orange": "Orange",
    "rervest": "RER Vest",
}

_STATUS_PAID_TOKENS = {
    "achitat",
    "achitata",
    "paid",
    "platit",
    "platita",
    "plătită",
    "stins",
    "stinsa",
}

_STATUS_UNPAID_TOKENS = {
    "de plata",
    "de_plata",
    "neachitat",
    "neachitata",
    "neplatit",
    "neplatita",
    "neplătită",
    "restant",
    "restanta",
    "scadent",
    "scadenta",
    "unpaid",
    "overdue",
    "da",
    "yes",
    "true",
    "1",
}


def _normalize_status_token(value: Any) -> str:
    return normalize_text(str(value or "")).strip().lower().replace("_", " ")


_NORMALIZED_STATUS_PAID_TOKENS = {_normalize_status_token(item) for item in _STATUS_PAID_TOKENS}
_NORMALIZED_STATUS_UNPAID_TOKENS = {_normalize_status_token(item) for item in _STATUS_UNPAID_TOKENS}

def _manual_invoice_status(hass, item: dict[str, Any]) -> dict[str, Any] | None:
    domain_data = hass.data.get(DOMENIU, {}) if hasattr(hass, "data") else {}
    cache = domain_data.get("_status_facturi_manual")
    if not isinstance(cache, dict) or not cache:
        return None

    cheie = construieste_cheie_status_factura(
        item.get("entry_id"),
        item.get("furnizor"),
        item.get("id_cont"),
        item.get("invoice_id"),
        item.get("invoice_title"),
        item.get("issue_date"),
        item.get("amount"),
        item.get("currency"),
    )
    if not cheie:
        return None

    value = cache.get(cheie)
    return dict(value) if isinstance(value, dict) else None


def _apply_manual_invoice_status(hass, item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None

    override = _manual_invoice_status(hass, item)
    if not override:
        item["manual_status_override"] = False
        return item

    status = str(override.get("status") or "").strip().lower()
    if status != "paid":
        item["manual_status_override"] = False
        return item

    original_status = item.get("status")
    item["status_original"] = original_status
    item["manual_status_override"] = True
    item["manual_status_label"] = "Marcată manual ca plătită"
    item["manual_status_updated_at"] = override.get("updated_at")
    item["status"] = "paid"
    item["payment_status"] = "paid"
    item["is_paid"] = True
    item["unpaid_amount"] = 0.0
    return item


def _status_in(value: Any, candidates: set[str]) -> bool:
    token = _normalize_status_token(value)
    return bool(token) and token in candidates

_UNPAID_RAW_KEYS = (
    "rest",
    "rest_plata",
    "sold",
    "remaining",
    "amount_remaining",
    "AmountRemaining",
    "remainingAmount",
    "UnpaidValue",
    "restToPay",
    "amountToPay",
    "remainingValue",
    "amountRemaining",
)

_PDF_RAW_KEYS = (
    "pdf_url",
    "download_url",
    "document_url",
    "pdf",
    "url",
)


def _provider_label(provider: str | None) -> str:
    key = str(provider or "").strip().lower()
    return _PROVIDER_LABELS.get(key, key.replace("_", " ").title() or "Furnizor")


def _to_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(" ", "").replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_key_for_date(value: date | datetime | str | None) -> tuple[int, str]:
    if value is None:
        return (0, "")
    if isinstance(value, datetime):
        return (1, value.isoformat())
    if isinstance(value, date):
        return (1, value.isoformat())
    return (1, str(value))


def _format_date(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _raw_dict(obj: Any) -> dict[str, Any]:
    raw = getattr(obj, "date_brute", None)
    return raw if isinstance(raw, dict) else {}


def _consum_value(
    instantaneu: InstantaneuFurnizor,
    key: str,
    id_cont: str | None = None,
) -> Any:
    for consum in instantaneu.consumuri or []:
        if getattr(consum, "cheie", None) != key:
            continue
        if id_cont is not None and getattr(consum, "id_cont", None) != id_cont:
            continue
        return getattr(consum, "valoare", None)
    return None


def _cont_for_factura(
    instantaneu: InstantaneuFurnizor,
    factura: FacturaUtilitate,
) -> ContUtilitate | None:
    factura_id_cont = getattr(factura, "id_cont", None)
    if factura_id_cont:
        for cont in instantaneu.conturi or []:
            if getattr(cont, "id_cont", None) == factura_id_cont:
                return cont

    factura_id_contract = getattr(factura, "id_contract", None)
    if factura_id_contract:
        for cont in instantaneu.conturi or []:
            if getattr(cont, "id_contract", None) == factura_id_contract:
                return cont

    if len(instantaneu.conturi or []) == 1:
        return instantaneu.conturi[0]

    return None




def _factura_este_ultima_curenta(
    instantaneu: InstantaneuFurnizor,
    factura: FacturaUtilitate,
    cont: ContUtilitate | None,
) -> bool:
    """Verifică dacă factura este factura curentă pentru cont.

    Unii furnizori expun soldul curent doar la nivel de cont. Dacă aplicăm acel
    sold tuturor facturilor din istoric, cardul ajunge să multiplice restanța cu
    numărul de facturi istorice. Folosim fallback-ul de cont doar pentru factura
    curentă identificabilă.
    """
    id_cont = getattr(cont, "id_cont", None) if cont else getattr(factura, "id_cont", None)
    raw = _raw_dict(factura)

    factura_id = str(getattr(factura, "id_factura", None) or "").strip()
    last_id = str(_consum_value(instantaneu, "id_ultima_factura", id_cont) or "").strip()
    if factura_id and last_id and (factura_id == last_id or factura_id in last_id or last_id in factura_id):
        return True

    for key in ("invoice_number", "number", "series_number", "serie_numar"):
        value = str(raw.get(key) or "").strip()
        if value and last_id and (value == last_id or value in last_id or last_id in value):
            return True

    amount = _to_float(getattr(factura, "valoare", None))
    last_amount = _to_float(_consum_value(instantaneu, "valoare_ultima_factura", id_cont))
    due_date = _format_date(getattr(factura, "data_scadenta", None))
    last_due = _format_date(_consum_value(instantaneu, "urmatoarea_scadenta", id_cont))
    if amount is not None and last_amount is not None and abs(amount - last_amount) < 0.01:
        if not last_due or due_date == last_due:
            return True

    return False

def _extract_unpaid_amount(
    instantaneu: InstantaneuFurnizor,
    factura: FacturaUtilitate,
    cont: ContUtilitate | None,
) -> float | None:
    raw = _raw_dict(factura)

    for key in _UNPAID_RAW_KEYS:
        value = _to_float(raw.get(key))
        if value is not None:
            return value

    id_cont = getattr(cont, "id_cont", None) if cont else getattr(factura, "id_cont", None)

    # Apă Brașov expune soldul curent la nivel de locație, dar include și istoric
    # de facturi. Nu aplicăm soldul curent tuturor facturilor istorice.
    if instantaneu.furnizor in {"apa_brasov"} and not _factura_este_ultima_curenta(instantaneu, factura, cont):
        return None

    for key in ("sold_factura", "de_plata", "total_neachitat", "sold_curent"):
        value = _to_float(_consum_value(instantaneu, key, id_cont))
        if value is not None:
            return value

    for key in ("factura_restanta",):
        value = normalize_text(_consum_value(instantaneu, key, id_cont)).lower()
        if value in {"da", "yes", "true", "1"}:
            amount = _to_float(getattr(factura, "valoare", None))
            return amount if amount is not None else 1.0
        if value in {"nu", "no", "false", "0"}:
            return 0.0

    return None


def _extract_pdf_url(factura: FacturaUtilitate) -> str | None:
    raw = _raw_dict(factura)
    for key in _PDF_RAW_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _derive_payment_status(
    instantaneu: InstantaneuFurnizor,
    factura: FacturaUtilitate,
    cont: ContUtilitate | None,
) -> tuple[str, bool | None, float | None]:
    amount_value = _to_float(getattr(factura, "valoare", None))
    category = normalize_text(getattr(factura, "categorie", None)).lower()
    status_text = normalize_text(getattr(factura, "stare", None)).lower()

    if category == "injectie" or (amount_value is not None and amount_value < 0):
        return "credit", True, 0.0

    # Semnalele financiare concrete bat tokenii textuali ambigui.
    # Dacă furnizorul expune sold/de_plata/total_neachitat pentru cont,
    # folosim acea valoare ca sursă de adevăr înainte de interpretarea
    # generică a câmpului "stare".
    unpaid_amount = _extract_unpaid_amount(instantaneu, factura, cont)
    if unpaid_amount is not None:
        if unpaid_amount > 0:
            return "unpaid", False, unpaid_amount
        if _status_in(status_text, _NORMALIZED_STATUS_PAID_TOKENS):
            return "paid", True, 0.0

    if _status_in(status_text, _NORMALIZED_STATUS_UNPAID_TOKENS):
        return "unpaid", False, unpaid_amount

    if _status_in(status_text, _NORMALIZED_STATUS_PAID_TOKENS):
        return "paid", True, 0.0

    if unpaid_amount is not None:
        return "paid", True, 0.0

    return "unknown", None, None



def _refresh_button_entity_id(coordonator: CoordonatorUtilitatiRomania) -> str | None:
    hass = getattr(coordonator, "hass", None)
    entry_id = getattr(getattr(coordonator, "intrare", None), "entry_id", None)
    if not hass or not entry_id:
        return None

    registry = er.async_get(hass)
    return registry.async_get_entity_id("button", DOMENIU, f"{entry_id}_actualizare_acum")


def _location_fields(
    coordonator: CoordonatorUtilitatiRomania,
    instantaneu: InstantaneuFurnizor,
    cont: ContUtilitate | None,
    fallback_value: Any,
) -> tuple[str, str, str | None]:
    id_cont = getattr(cont, "id_cont", None) if cont else None
    manual_group_label = None
    if id_cont:
        manual_group_label = obtine_grupare_factura(
            coordonator.hass,
            coordonator.intrare.entry_id,
            instantaneu.furnizor,
            id_cont,
        )

    if manual_group_label:
        return (
            normalize_facturi_location_key(manual_group_label),
            manual_group_label,
            manual_group_label,
        )

    return (
        normalize_facturi_location_key(fallback_value),
        build_facturi_location_label(fallback_value),
        None,
    )

def _build_invoice_item(
    coordonator: CoordonatorUtilitatiRomania,
    instantaneu: InstantaneuFurnizor,
    factura: FacturaUtilitate,
) -> dict[str, Any]:
    cont = _cont_for_factura(instantaneu, factura)
    location_key, location_label, manual_group_label = _location_fields(
        coordonator,
        instantaneu,
        cont,
        cont or getattr(factura, "id_cont", None) or instantaneu.titlu,
    )
    payment_status, is_paid, unpaid_amount = _derive_payment_status(
        instantaneu,
        factura,
        cont,
    )

    invoice_title = getattr(factura, "titlu", None) or "Ultima factură"

    # Curățăm titlurile tehnice E.ON de forma "Factura eon_xxx_ultima"
    if (
        instantaneu.furnizor == "eon"
        and isinstance(invoice_title, str)
        and invoice_title.lower().startswith("factura eon_")
    ):
        consum_id = _consum_value(
            instantaneu,
            "id_ultima_factura",
            getattr(factura, "id_cont", None),
        )
        consum_id_text = str(consum_id or "").strip()
        invoice_title = consum_id_text or "Ultima factură"

    item = {
        "entry_id": coordonator.intrare.entry_id,
        "entry_title": coordonator.intrare.title,
        "furnizor": instantaneu.furnizor,
        "furnizor_label": _provider_label(instantaneu.furnizor),
        "locatie_cheie": location_key,
        "eticheta_locatie": location_label,
        "adresa_originala": getattr(cont, "adresa", None) if cont else None,
        "eticheta_grupare_manuala": manual_group_label,
        "id_cont": getattr(factura, "id_cont", None) or (getattr(cont, "id_cont", None) if cont else None),
        "id_contract": getattr(factura, "id_contract", None) or (getattr(cont, "id_contract", None) if cont else None),
        "nume_cont": getattr(cont, "nume", None) if cont else None,
        "tip_utilitate": getattr(factura, "tip_utilitate", None) or (getattr(cont, "tip_utilitate", None) if cont else None),
        "tip_serviciu": getattr(factura, "tip_serviciu", None) or (getattr(cont, "tip_serviciu", None) if cont else None),
        "invoice_id": getattr(factura, "id_factura", None),
        "invoice_title": invoice_title,
        "issue_date": _format_date(getattr(factura, "data_emitere", None)),
        "due_date": _format_date(getattr(factura, "data_scadenta", None)),
        "amount": getattr(factura, "valoare", None),
        "currency": getattr(factura, "moneda", None) or "RON",
        "status_raw": getattr(factura, "stare", None),
        "status": payment_status,
        "payment_status": payment_status,
        "is_paid": is_paid,
        "unpaid_amount": unpaid_amount,
        "pdf_url": _extract_pdf_url(factura),
        "refresh_button_entity_id": _refresh_button_entity_id(coordonator),
        "can_refresh": _refresh_button_entity_id(coordonator) is not None,
    }

    if instantaneu.furnizor in {"ebloc", "apa_canal", "apa_brasov"}:
        id_cont = getattr(cont, "id_cont", None) if cont else getattr(factura, "id_cont", None)

        citire_permisa = _consum_value(instantaneu, "citire_index_permisa", id_cont)
        perioada_citire = _consum_value(instantaneu, "perioada_citire", id_cont)
        zile_pana_citire = _consum_value(instantaneu, "zile_pana_citire_index", id_cont)

        if instantaneu.furnizor == "ebloc":
            item["invoice_title"] = (
                invoice_title
                if invoice_title and invoice_title != "Ultima factură"
                else "Întreținere"
            )
            item["tip_serviciu"] = "Întreținere"

        item["reading_available"] = True
        item["reading_is_open"] = normalize_text(citire_permisa).lower() in {"da", "yes", "true", "1", "on"}
        item["reading_period"] = perioada_citire
        item["reading_days_until"] = zile_pana_citire

    return item


def _build_eon_fallback_item(
    coordonator: CoordonatorUtilitatiRomania,
    instantaneu: InstantaneuFurnizor,
    cont: ContUtilitate,
) -> dict[str, Any] | None:
    id_cont = getattr(cont, "id_cont", None)
    if not id_cont:
        return None

    hass = coordonator.hass
    cont_raw = cont.date_brute if isinstance(cont.date_brute, dict) else {}

    factura_id = (
        _consum_value(instantaneu, "id_ultima_factura", id_cont)
        or cont_raw.get("id_ultima_factura")
    )
    valoare = _to_float(
        _consum_value(instantaneu, "valoare_ultima_factura", id_cont)
        or cont_raw.get("valoare_ultima_factura")
    )
    data_emitere = (
        _consum_value(instantaneu, "data_ultima_factura", id_cont)
        or cont_raw.get("data_ultima_factura")
    )
    data_scadenta = (
        _consum_value(instantaneu, "urmatoarea_scadenta", id_cont)
        or _consum_value(instantaneu, "data_scadenta", id_cont)
        or _consum_value(instantaneu, "next_due_date", id_cont)
        or cont_raw.get("urmatoarea_scadenta")
        or cont_raw.get("data_scadenta")
        or cont_raw.get("next_due_date")
    )

    if hass:
        if not data_scadenta:
            for state in hass.states.async_all():
                entity_id = state.entity_id
                if not entity_id.startswith("sensor.eon_"):
                    continue

                attrs = state.attributes or {}
                if str(attrs.get("id_cont")) != str(id_cont):
                    continue

                if "urmatoarea_scadenta" in entity_id:
                    state_value = str(state.state or "").strip()
                    if state_value and state_value.lower() not in {"unknown", "unavailable", "none"}:
                        data_scadenta = state_value
                        break

        if not data_emitere:
            for state in hass.states.async_all():
                entity_id = state.entity_id
                if not entity_id.startswith("sensor.eon_"):
                    continue

                attrs = state.attributes or {}
                if str(attrs.get("id_cont")) != str(id_cont):
                    continue

                if "data_ultimei_facturi" in entity_id or "data_ultima_factura" in entity_id:
                    state_value = str(state.state or "").strip()
                    if state_value and state_value.lower() not in {"unknown", "unavailable", "none"}:
                        data_emitere = state_value
                        break

    factura_restanta = (
        _consum_value(instantaneu, "factura_restanta", id_cont)
        or cont_raw.get("factura_restanta")
    )
    de_plata = _to_float(
        _consum_value(instantaneu, "de_plata", id_cont)
        or cont_raw.get("de_plata")
    )
    sold_curent = _to_float(
        _consum_value(instantaneu, "sold_curent", id_cont)
        or cont_raw.get("sold_curent")
    )

    if factura_id in (None, "") and valoare is None and data_scadenta in (None, ""):
        return None

    factura_id_text = (
        str(factura_id).strip()
        if factura_id not in (None, "")
        else f"eon_{id_cont}_ultima"
    )

    factura_restanta_text = normalize_text(factura_restanta).lower()

    if factura_restanta_text in {"da", "yes", "true", "1"}:
        status = "unpaid"
        is_paid = False
        unpaid_amount = (
            de_plata if de_plata is not None and de_plata > 0
            else sold_curent if sold_curent is not None and sold_curent > 0
            else valoare if valoare is not None and valoare > 0
            else 0.0
        )
    elif de_plata is not None and de_plata > 0:
        status = "unpaid"
        is_paid = False
        unpaid_amount = de_plata
    elif sold_curent is not None and sold_curent > 0:
        status = "unpaid"
        is_paid = False
        unpaid_amount = sold_curent
    elif factura_restanta_text in {"nu", "no", "false", "0"}:
        status = "paid"
        is_paid = True
        unpaid_amount = 0.0
    else:
        status = "unknown"
        is_paid = None
        unpaid_amount = None

    issue_date = _format_date(data_emitere)
    due_date = _format_date(data_scadenta)
    location_key, location_label, manual_group_label = _location_fields(
        coordonator,
        instantaneu,
        cont,
        cont,
    )

    return {
        "entry_id": coordonator.intrare.entry_id,
        "entry_title": coordonator.intrare.title,
        "furnizor": instantaneu.furnizor,
        "furnizor_label": _provider_label(instantaneu.furnizor),
        "locatie_cheie": location_key,
        "eticheta_locatie": location_label,
        "adresa_originala": getattr(cont, "adresa", None),
        "eticheta_grupare_manuala": manual_group_label,
        "id_cont": id_cont,
        "id_contract": getattr(cont, "id_contract", None),
        "nume_cont": getattr(cont, "nume", None),
        "tip_utilitate": getattr(cont, "tip_utilitate", None),
        "tip_serviciu": getattr(cont, "tip_serviciu", None),
        "invoice_id": factura_id_text,
        "invoice_title": factura_id_text if factura_id_text and not factura_id_text.lower().startswith("eon_") else "Ultima factură",
        "issue_date": issue_date,
        "due_date": due_date,
        "amount": valoare,
        "currency": "RON",
        "status_raw": factura_restanta,
        "status": status,
        "payment_status": status,
        "is_paid": is_paid,
        "unpaid_amount": unpaid_amount,
        "pdf_url": None,
        "refresh_button_entity_id": _refresh_button_entity_id(coordonator),
        "can_refresh": _refresh_button_entity_id(coordonator) is not None,
    }




def _build_hidroelectrica_fallback_item(
    coordonator: CoordonatorUtilitatiRomania,
    instantaneu: InstantaneuFurnizor,
    cont: ContUtilitate,
) -> dict[str, Any] | None:
    id_cont = getattr(cont, "id_cont", None)
    if not id_cont:
        return None

    cont_raw = cont.date_brute if isinstance(cont.date_brute, dict) else {}

    factura_id = (
        _consum_value(instantaneu, "id_ultima_factura", id_cont)
        or cont_raw.get("ultima_factura_id")
        or cont_raw.get("last_invoice_id")
    )
    valoare = _to_float(
        _consum_value(instantaneu, "valoare_ultima_factura", id_cont)
        or cont_raw.get("valoare_ultima_factura")
        or cont_raw.get("last_invoice_amount")
    )
    data_scadenta = (
        _consum_value(instantaneu, "urmatoarea_scadenta", id_cont)
        or cont_raw.get("urmatoarea_scadenta")
        or cont_raw.get("next_due_date")
    )
    sold_factura = _to_float(
        _consum_value(instantaneu, "sold_factura", id_cont)
        or _consum_value(instantaneu, "sold_curent", id_cont)
        or cont_raw.get("sold_factura")
        or cont_raw.get("rembalance")
    )
    factura_restanta = (
        _consum_value(instantaneu, "factura_restanta", id_cont)
        or cont_raw.get("factura_restanta")
    )

    if factura_id in (None, "") and valoare is None and data_scadenta in (None, ""):
        return None

    restanta_text = normalize_text(factura_restanta).lower()
    if restanta_text in {"da", "yes", "true", "1"} or (sold_factura is not None and sold_factura > 0):
        status = "unpaid"
        is_paid = False
        unpaid_amount = sold_factura if sold_factura is not None and sold_factura > 0 else valoare
    elif restanta_text in {"nu", "no", "false", "0"} or sold_factura is not None:
        status = "paid"
        is_paid = True
        unpaid_amount = 0.0
    else:
        status = "unknown"
        is_paid = None
        unpaid_amount = None

    factura_id_text = str(factura_id or "").strip()
    location_key, location_label, manual_group_label = _location_fields(
        coordonator,
        instantaneu,
        cont,
        cont,
    )

    return {
        "entry_id": coordonator.intrare.entry_id,
        "entry_title": coordonator.intrare.title,
        "furnizor": instantaneu.furnizor,
        "furnizor_label": _provider_label(instantaneu.furnizor),
        "locatie_cheie": location_key,
        "eticheta_locatie": location_label,
        "adresa_originala": getattr(cont, "adresa", None),
        "eticheta_grupare_manuala": manual_group_label,
        "id_cont": id_cont,
        "id_contract": getattr(cont, "id_contract", None),
        "nume_cont": getattr(cont, "nume", None),
        "tip_utilitate": getattr(cont, "tip_utilitate", None),
        "tip_serviciu": getattr(cont, "tip_serviciu", None),
        "invoice_id": factura_id_text or f"hidroelectrica_{id_cont}_ultima",
        "invoice_title": factura_id_text or "Ultima factură",
        "issue_date": None,
        "due_date": _format_date(data_scadenta),
        "amount": valoare,
        "currency": "RON",
        "status_raw": factura_restanta,
        "status": status,
        "payment_status": status,
        "is_paid": is_paid,
        "unpaid_amount": unpaid_amount,
        "pdf_url": None,
        "refresh_button_entity_id": _refresh_button_entity_id(coordonator),
        "can_refresh": _refresh_button_entity_id(coordonator) is not None,
    }



def _money_to_lei(value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None

    if isinstance(value, str):
        raw = value.strip().replace(' ', '')
        if raw.isdigit() and len(raw) >= 4:
            return round(float(raw) / 100.0, 2)
    if isinstance(value, int) and abs(value) >= 1000:
        return round(float(value) / 100.0, 2)
    return round(parsed, 2)


def _cheie_grupare_factura(item: dict[str, Any]) -> tuple[str, ...]:
    """Construiește cheia stabilă folosită pentru agregarea facturilor în dashboard."""
    locatie = item["locatie_cheie"]
    furnizor = normalize_text(item["furnizor"]).lower()

    # Unii furnizori pot avea mai multe contracte/servicii pe aceeași locație.
    # Dacă am grupa doar după locație + furnizor, facturile se suprascriu între ele.
    if furnizor in {"eon", "orange"}:
        identificator_serviciu = (
            item.get("id_cont")
            or item.get("id_contract")
            or item.get("tip_utilitate")
            or item.get("tip_serviciu")
            or item.get("invoice_id")
            or item.get("invoice_title")
            or ""
        )
        return (locatie, furnizor, normalize_text(identificator_serviciu).lower())

    if furnizor == "digi":
        identificator_serviciu = (
            item.get("invoice_title")
            or item.get("tip_serviciu")
            or item.get("tip_utilitate")
            or item.get("invoice_id")
            or ""
        )
        return (locatie, furnizor, normalize_text(identificator_serviciu).lower())

    if furnizor == "apa_brasov":
        # Apă Brașov are câte o factură curentă pentru fiecare loc de consum.
        # Dacă grupăm doar după titlul generic al facturii („Factură apă/canal”),
        # factura plătită de pe o locație poate fi combinată cu factura restantă
        # de pe altă locație și dispare din card. Identificatorul stabil al
        # locației păstrează rândurile separate, dar tot sub gruparea comună
        # „Apă Brașov” atunci când utilizatorul grupează manual furnizorul.
        identificator_locatie = (
            item.get("id_cont")
            or item.get("id_contract")
            or item.get("adresa_originala")
            or item.get("nume_cont")
            or item.get("invoice_id")
            or item.get("invoice_title")
            or ""
        )
        return (locatie, furnizor, normalize_text(identificator_locatie).lower())

    return (locatie, furnizor)


def _valoare_neplatita_item(item: dict[str, Any]) -> float:
    if item.get("status") != "unpaid":
        return 0.0

    amount = _to_float(item.get("unpaid_amount"))
    if amount is None or amount <= 0:
        amount = _to_float(item.get("amount"))
    return round(max(float(amount or 0), 0.0), 2)


def _numar_neplatite_item(item: dict[str, Any]) -> int:
    """Returnează numărul de facturi neplătite reprezentate de un rând din card."""
    if item.get("status") != "unpaid":
        return 0

    # La DIGI facturile de servicii diferite pot veni în aceeași structură de cont,
    # dar în card le afișăm deja ca rânduri separate. Dacă folosim `unpaid_count`
    # sau lista comună de invoice-uri, antetul grupei dublează numărul de neplătite
    # deși totalul valoric este corect. Pentru fiecare rând DIGI afișat contăm o
    # singură factură restantă.
    if normalize_text(item.get("furnizor")).lower() == "digi":
        return 1

    explicit_count = item.get("unpaid_count")
    try:
        count = int(explicit_count)
    except (TypeError, ValueError):
        count = 0

    if count > 0:
        return count

    unpaid_invoice_ids = item.get("unpaid_invoice_ids")
    if isinstance(unpaid_invoice_ids, list) and unpaid_invoice_ids:
        return len(unpaid_invoice_ids)

    return 1


def _initializeaza_agregare_item(item: dict[str, Any]) -> dict[str, Any]:
    count = 1 if item.get("status") == "unpaid" else 0
    total = _valoare_neplatita_item(item)

    item["unpaid_count"] = count
    item["unpaid_total"] = total
    if count:
        item["unpaid_amount"] = total

    invoice_id = item.get("invoice_id")
    item["invoice_ids"] = [invoice_id] if invoice_id not in (None, "") else []
    item["unpaid_invoice_ids"] = item["invoice_ids"][:] if count else []
    return item


def _combina_itemuri_grupate(current: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    current = _initializeaza_agregare_item(current)
    item = _initializeaza_agregare_item(item)

    current_unpaid_count = int(current.get("unpaid_count") or 0)
    item_unpaid_count = int(item.get("unpaid_count") or 0)
    current_unpaid_total = _to_float(current.get("unpaid_total")) or _valoare_neplatita_item(current)
    item_unpaid_total = _to_float(item.get("unpaid_total")) or _valoare_neplatita_item(item)

    if _sort_key_for_date(item.get("issue_date")) > _sort_key_for_date(current.get("issue_date")):
        display = dict(item)
        other = current
    else:
        display = dict(current)
        other = item

    invoice_ids = []
    unpaid_invoice_ids = []
    for source in (current, item):
        for invoice_id in source.get("invoice_ids") or []:
            if invoice_id not in (None, "") and invoice_id not in invoice_ids:
                invoice_ids.append(invoice_id)
        for invoice_id in source.get("unpaid_invoice_ids") or []:
            if invoice_id not in (None, "") and invoice_id not in unpaid_invoice_ids:
                unpaid_invoice_ids.append(invoice_id)

    unpaid_count = current_unpaid_count + item_unpaid_count
    unpaid_total = round(current_unpaid_total + item_unpaid_total, 2)

    display["unpaid_count"] = unpaid_count
    display["unpaid_total"] = unpaid_total
    display["invoice_ids"] = invoice_ids
    display["unpaid_invoice_ids"] = unpaid_invoice_ids

    if unpaid_count > 0:
        display["status"] = "unpaid"
        display["payment_status"] = "unpaid"
        display["is_paid"] = False
        display["unpaid_amount"] = unpaid_total

        if unpaid_count > 1:
            display["invoice_count_label"] = f"{unpaid_count} facturi neplătite"
            display["invoice_title"] = display.get("invoice_title") or other.get("invoice_title")

    return display



def _factura_latest_key(factura: FacturaUtilitate) -> tuple[str, str]:
    """Cheie stabilă pentru alegerea ultimei facturi pe cont."""
    id_cont = str(getattr(factura, "id_cont", None) or "").strip()
    if id_cont:
        return ("cont", id_cont)
    id_contract = str(getattr(factura, "id_contract", None) or "").strip()
    if id_contract:
        return ("contract", id_contract)
    return ("global", "")


def _latest_invoice_ids_by_group(facturi: list[FacturaUtilitate]) -> set[str]:
    """Returnează id-urile ultimelor facturi, câte una pentru fiecare cont."""
    latest: dict[tuple[str, str], FacturaUtilitate] = {}
    for factura in facturi or []:
        key = _factura_latest_key(factura)
        current = latest.get(key)
        if current is None:
            latest[key] = factura
            continue
        factura_sort = (
            _sort_key_for_date(getattr(factura, "data_emitere", None)),
            _sort_key_for_date(getattr(factura, "data_scadenta", None)),
            str(getattr(factura, "id_factura", "") or ""),
        )
        current_sort = (
            _sort_key_for_date(getattr(current, "data_emitere", None)),
            _sort_key_for_date(getattr(current, "data_scadenta", None)),
            str(getattr(current, "id_factura", "") or ""),
        )
        if factura_sort > current_sort:
            latest[key] = factura
    return {str(getattr(factura, "id_factura", "") or "") for factura in latest.values()}


def colecteaza_facturi_agregate(hass) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    domain_data = hass.data.get(DOMENIU, {}) if hasattr(hass, "data") else {}

    for maybe_coord in domain_data.values():
        if not isinstance(maybe_coord, CoordonatorUtilitatiRomania):
            continue

        if maybe_coord.intrare.data.get("furnizor") == FURNIZOR_ADMIN_GLOBAL:
            continue

        instantaneu = maybe_coord.data
        if not isinstance(instantaneu, InstantaneuFurnizor):
            continue

        facturi_de_afisat = list(instantaneu.facturi or [])
        if instantaneu.furnizor in {"engie", "apa_brasov"}:
            # Pentru MyENGIE și Apă Brașov păstrăm în card doar ultima factură pe fiecare loc de consum.
            # Istoricul vechi poate veni fără indicator explicit de plată și ar fi marcat
            # greșit ca restant de logica generică a dashboardului.
            latest_ids = _latest_invoice_ids_by_group(facturi_de_afisat)
            facturi_de_afisat = [
                factura for factura in facturi_de_afisat
                if str(getattr(factura, "id_factura", "") or "") in latest_ids
            ]

        # 1. Facturi reale, dacă există
        for factura in facturi_de_afisat:
            item = _apply_manual_invoice_status(hass, _build_invoice_item(maybe_coord, instantaneu, factura))

            # Pentru cardul de "ultima factură" ignorăm documentele de tip credit/storno.
            # Altfel, la unii furnizori (ex. myElectrica) putem afișa greșit un credit
            # în locul ultimei facturi reale de consum.
            if item.get("status") == "credit":
                continue

            group_key = _cheie_grupare_factura(item)

            current = grouped.get(group_key)
            if current is None:
                grouped[group_key] = _initializeaza_agregare_item(item)
            else:
                grouped[group_key] = _combina_itemuri_grupate(current, item)

        # 2. Fallback specific E.ON din consumuri, doar pentru corectarea statusului curent
        if instantaneu.furnizor == "eon":
            for cont in instantaneu.conturi or []:
                fallback_item = _apply_manual_invoice_status(hass, _build_eon_fallback_item(maybe_coord, instantaneu, cont))
                if fallback_item is None:
                    continue

                group_key = _cheie_grupare_factura(fallback_item)

                current = grouped.get(group_key)

                if current is None:
                    grouped[group_key] = fallback_item
                    continue

                # Doar dacă fallback-ul spune clar că este neplătită, suprascriem statusul.
                # Dacă fallback-ul spune plătită, lăsăm itemul existent în pace,
                # pentru a nu strica situațiile deja corecte.
                if fallback_item.get("status") == "unpaid":
                    if current.get("manual_status_override"):
                        continue

                    current["status_raw"] = fallback_item.get("status_raw")
                    current["status"] = "unpaid"
                    current["payment_status"] = "unpaid"
                    current["is_paid"] = False
                    current["unpaid_amount"] = fallback_item.get("unpaid_amount")
                    if fallback_item.get("due_date"):
                        current["due_date"] = fallback_item.get("due_date")
                    if fallback_item.get("issue_date"):
                        current["issue_date"] = fallback_item.get("issue_date")
                    if fallback_item.get("amount") is not None:
                        current["amount"] = fallback_item.get("amount")
                    if fallback_item.get("invoice_id"):
                        current["invoice_id"] = fallback_item.get("invoice_id")
                    if fallback_item.get("invoice_title"):
                        current["invoice_title"] = fallback_item.get("invoice_title")

        # 3. Fallback Hidroelectrica din senzorii de cont.
        # Uneori istoricul de facturi nu mai întoarce o factură utilizabilă,
        # dar coordonatorul are în continuare ultima valoare, scadența și statusul.
        if instantaneu.furnizor == "hidroelectrica":
            for cont in instantaneu.conturi or []:
                fallback_item = _apply_manual_invoice_status(
                    hass,
                    _build_hidroelectrica_fallback_item(maybe_coord, instantaneu, cont),
                )
                if fallback_item is None:
                    continue

                group_key = _cheie_grupare_factura(fallback_item)

                current = grouped.get(group_key)
                if current is None:
                    grouped[group_key] = fallback_item
                    continue

                if fallback_item.get("status") == "unpaid" and not current.get("manual_status_override"):
                    current["status_raw"] = fallback_item.get("status_raw")
                    current["status"] = "unpaid"
                    current["payment_status"] = "unpaid"
                    current["is_paid"] = False
                    current["unpaid_amount"] = fallback_item.get("unpaid_amount")
                    if fallback_item.get("due_date"):
                        current["due_date"] = fallback_item.get("due_date")
                    if fallback_item.get("amount") is not None:
                        current["amount"] = fallback_item.get("amount")
                    if fallback_item.get("invoice_id"):
                        current["invoice_id"] = fallback_item.get("invoice_id")
                    if fallback_item.get("invoice_title"):
                        current["invoice_title"] = fallback_item.get("invoice_title")

    
    items = list(grouped.values())
    items.sort(
        key=lambda item: (
            normalize_text(item.get("eticheta_locatie")).lower(),
            normalize_text(item.get("furnizor_label")).lower(),
        )
    )
    return items


def sumar_facturi(items: list[dict[str, Any]]) -> dict[str, Any]:
    total_unpaid = 0.0
    grouped_locations: dict[str, dict[str, Any]] = {}

    for item in items:
        if item.get("status") == "unpaid":
            unpaid_amount = _to_float(item.get("unpaid_amount"))
            if unpaid_amount is not None and unpaid_amount > 0:
                total_unpaid += unpaid_amount

        location = grouped_locations.setdefault(
            item.get("locatie_cheie") or "locatie",
            {
                "locatie_cheie": item.get("locatie_cheie") or "locatie",
                "eticheta_locatie": item.get("eticheta_locatie") or "Locație",
                "furnizori": [],
            },
        )
        location["furnizori"].append(item)

    total = 0
    paid = 0
    unpaid = 0
    unknown = 0

    for location in grouped_locations.values():
        for item in location["furnizori"]:
            total += 1

            status = item.get("status")
            if status in {"paid", "credit"}:
                paid += 1
            elif status == "unpaid":
                unpaid += _numar_neplatite_item(item)
            else:
                unknown += 1

    locations = list(grouped_locations.values())
    locations.sort(key=lambda loc: normalize_text(loc.get("eticheta_locatie")).lower())

    for location in locations:
        location["furnizori"].sort(
            key=lambda item: normalize_text(item.get("furnizor_label")).lower()
        )

        location_total_unpaid = 0.0
        for item in location["furnizori"]:
            if item.get("status") != "unpaid":
                continue
            unpaid_amount = _to_float(item.get("unpaid_amount"))
            if unpaid_amount is not None and unpaid_amount > 0:
                location_total_unpaid += unpaid_amount

        location_total_unpaid = round(location_total_unpaid, 2)
        location["total_neplatit"] = location_total_unpaid
        location["total_neplatit_formatat"] = f"{location_total_unpaid:.2f} RON"

    total_unpaid = round(total_unpaid, 2)

    return {
        "numar_facturi": total,
        "numar_platite": paid,
        "numar_neplatite": unpaid,
        "numar_necunoscute": unknown,
        "numar_status_necunoscut": unknown,
        "total_neplatit": total_unpaid,
        "total_neplatit_formatat": f"{total_unpaid:.2f} RON",
        "moneda": "RON",
        "locatii": locations,
    }
