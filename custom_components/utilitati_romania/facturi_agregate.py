from __future__ import annotations

from dataclasses import replace
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
from .locuri_ignorate import (
    construieste_cheie_loc_consum,
    este_loc_consum_ignorat,
    obtine_locuri_ignorate,
)
from .modele import ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .naming import normalize_text, slugify_text


_PROVIDER_LABELS = {
    "apa_canal": "Apă Canal Sibiu",
    "apa_brasov": "Apă Brașov",
    "apa_oradea": "Apă Oradea",
    "apa_galati": "Apă Canal Galați",
    "aparegio": "ApaRegio Gorj",
    "hidro_prahova": "Hidro Prahova",
    "comprest": "Comprest",
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
    "retim": "RETIM",
    "aquatim": "Aquatim",
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




def _pare_identificator_tehnic_factura(value: Any) -> bool:
    """Detectează tokenuri interne care nu trebuie afișate drept număr de factură."""
    text = str(value or "").strip()
    if not text:
        return False
    if text.endswith("==") or (len(text) >= 20 and any(ch in text for ch in "+/=")):
        return True
    if len(text) >= 32 and all(ch.isalnum() for ch in text):
        return any(ch.islower() for ch in text) and any(ch.isupper() for ch in text) and any(ch.isdigit() for ch in text)
    return False


def _curata_identificator_factura(value: Any) -> str:
    text = str(value or "").strip()
    if not text or _pare_identificator_tehnic_factura(text):
        return ""
    return text

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

    # Unii furnizori expun soldul curent la nivel de locație, dar includ și
    # istoricul facturilor. Nu aplicăm soldul curent tuturor facturilor istorice,
    # altfel dashboardul multiplică artificial totalul neplătit.
    if instantaneu.furnizor in {"apa_brasov", "hidroelectrica"} and not _factura_este_ultima_curenta(instantaneu, factura, cont):
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




def _text_punct_nova(value: Any) -> str:
    """Normalizează valorile de identificare pentru punctele de consum Nova."""
    return str(value or "").strip()


def _identificator_punct_nova(raw: dict[str, Any]) -> str:
    """Alege cel mai stabil identificator disponibil pentru punctul de consum Nova."""
    for key in (
        "meteringPointNumber",
        "meteringPointCode",
        "meteringPointId",
        "specificIdForUtilityType",
        "contractId",
    ):
        value = _text_punct_nova(raw.get(key))
        if value:
            return value
    puncte = raw.get("meteringPoints")
    if isinstance(puncte, list):
        for punct in puncte:
            if not isinstance(punct, dict):
                continue
            for key in (
                "meteringPointNumber",
                "meteringPointCode",
                "number",
                "specificIdForUtilityType",
                "meteringPointId",
                "id",
                "contractId",
            ):
                value = _text_punct_nova(punct.get(key))
                if value:
                    return value
    return ""


def _adresa_punct_nova(raw: dict[str, Any], cont: ContUtilitate | None) -> str | None:
    """Extrage adresa punctului de consum Nova din factură sau din cont."""
    for key in ("meteringPointAddress", "address", "serviceAddress"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    puncte = raw.get("meteringPoints")
    if isinstance(puncte, list):
        for punct in puncte:
            if not isinstance(punct, dict):
                continue
            value = punct.get("address") or punct.get("meteringPointAddress")
            if isinstance(value, str) and value.strip():
                return value.strip()
    adresa_cont = getattr(cont, "adresa", None) if cont else None
    return adresa_cont if isinstance(adresa_cont, str) and adresa_cont.strip() else None


def _asigura_cheie_locatie_nova(
    item: dict[str, Any],
    factura: FacturaUtilitate,
    cont: ContUtilitate | None,
) -> None:
    """Separă în dashboard locurile de consum Nova din același cont client.

    Nova poate returna un singur cont client cu mai multe puncte de consum, iar
    facturile conțin identificatorul punctului prin meteringPointNumber sau
    meteringPointCode. Entitățile existente rămân neschimbate; ajustăm doar cheia
    internă folosită de dashboard pentru gruparea facturilor pe loc de consum.
    """
    if normalize_text(item.get("furnizor")).lower() != "nova":
        return

    if item.get("eticheta_grupare_manuala"):
        return

    raw = _raw_dict(factura)
    identificator = _identificator_punct_nova(raw)
    if not identificator:
        return

    adresa = _adresa_punct_nova(raw, cont)
    baza_locatie = adresa or item.get("adresa_originala") or item.get("eticheta_locatie") or identificator
    baza_cheie = normalize_facturi_location_key(baza_locatie)
    item["locatie_cheie"] = f"{baza_cheie}__nova_{normalize_text(identificator).lower()}"

    if adresa:
        item["eticheta_locatie"] = build_facturi_location_label(adresa)
        item["adresa_originala"] = adresa

    item["id_punct_consum"] = identificator
    item["cod_punct_consum"] = _text_punct_nova(raw.get("meteringPointCode")) or None
    item["numar_punct_consum"] = _text_punct_nova(raw.get("meteringPointNumber")) or None

def _asigura_cheie_locatie_hidroelectrica(item: dict[str, Any]) -> None:
    """Păstrează separat contractele Hidroelectrica în dashboard.

    Hidroelectrica poate avea mai multe contracte/locuri de consum în același
    cont, iar adresele pot produce aceeași etichetă scurtă în interfață. Dacă
    cheia de locație rămâne doar eticheta/adresa scurtată, dashboardul poate
    grupa două contracte distincte într-un singur loc de consum.

    Nu modificăm numele afișat și nu atingem identificatorii entităților Home
    Assistant; stabilizăm doar cheia internă folosită de agregatorul de facturi.
    Grupările manuale rămân prioritare și nu sunt suprascrise.
    """
    if normalize_text(item.get("furnizor")).lower() != "hidroelectrica":
        return

    if item.get("eticheta_grupare_manuala"):
        return

    identificator = (
        item.get("id_cont")
        or item.get("id_contract")
        or item.get("nume_cont")
        or item.get("invoice_id")
    )
    identificator_text = str(identificator or "").strip()
    if not identificator_text:
        return

    baza = str(item.get("locatie_cheie") or "locatie").strip() or "locatie"
    item["locatie_cheie"] = f"{baza}__hidro_{normalize_text(identificator_text).lower()}"


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

    raw_factura = _raw_dict(factura)

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

    real_invoice_number = (
        raw_factura.get("numar_factura")
        or raw_factura.get("invoice_number")
        or raw_factura.get("number")
        or raw_factura.get("series_number")
        or raw_factura.get("serie_numar")
        or raw_factura.get("document_number")
    )
    raw_invoice_id = getattr(factura, "id_factura", None)
    if instantaneu.furnizor == "digi" and real_invoice_number is not None:
        real_text = str(real_invoice_number).strip()
        raw_id_text = str(raw_invoice_id or "").strip()
        # Digi foloseste un ID intern numeric pentru deschiderea detaliilor.
        # Daca nu am reusit sa extragem seria/numarul real din popup, nu il afisam ca document.
        if raw_id_text and real_text == raw_id_text:
            real_invoice_number = None

    cont_raw = cont.date_brute if cont and isinstance(cont.date_brute, dict) else {}

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
        "id_apartament": (
            raw_factura.get("id_apartament")
            or raw_factura.get("id_ap")
            or cont_raw.get("id_apartament")
            or cont_raw.get("id_ap")
        ),
        "id_contract": getattr(factura, "id_contract", None) or (getattr(cont, "id_contract", None) if cont else None),
        "nume_cont": getattr(cont, "nume", None) if cont else None,
        "tip_utilitate": getattr(factura, "tip_utilitate", None) or (getattr(cont, "tip_utilitate", None) if cont else None),
        "tip_serviciu": getattr(factura, "tip_serviciu", None) or (getattr(cont, "tip_serviciu", None) if cont else None),
        "invoice_id": _curata_identificator_factura(getattr(factura, "id_factura", None)) if instantaneu.furnizor == "hidroelectrica" else getattr(factura, "id_factura", None),
        "invoice_number": real_invoice_number,
        "numar_factura": real_invoice_number,
        "document_number": real_invoice_number,
        "invoice_title": (_curata_identificator_factura(invoice_title) or "Ultima factură") if instantaneu.furnizor == "hidroelectrica" else invoice_title,
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

    _asigura_cheie_locatie_nova(item, factura, cont)
    _asigura_cheie_locatie_hidroelectrica(item)

    if instantaneu.furnizor in {"ebloc", "apa_canal", "apa_brasov", "apa_oradea", "apa_galati", "aparegio", "hidro_prahova"}:
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

    factura_id_text = _curata_identificator_factura(factura_id)
    location_key, location_label, manual_group_label = _location_fields(
        coordonator,
        instantaneu,
        cont,
        cont,
    )

    item = {
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
    _asigura_cheie_locatie_hidroelectrica(item)
    return item



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




def _hidroelectrica_rest_de_plata(factura: FacturaUtilitate) -> float | None:
    """Returnează restul de plată declarat explicit de Hidroelectrica pentru factură."""
    raw = _raw_dict(factura)
    for key in _UNPAID_RAW_KEYS:
        value = _to_float(raw.get(key))
        if value is not None:
            return value
    return None


def _hidroelectrica_are_rest_de_plata(factura: FacturaUtilitate) -> bool:
    """Verifică dacă o factură Hidroelectrica este declarată explicit ca neachitată."""
    rest = _hidroelectrica_rest_de_plata(factura)
    if rest is not None and rest > 0:
        return True

    status_text = normalize_text(getattr(factura, "stare", None)).lower()
    return _status_in(status_text, _NORMALIZED_STATUS_UNPAID_TOKENS)


def _hidroelectrica_este_factura_curenta_sintetica(factura: FacturaUtilitate) -> bool:
    """Identifică factura curentă construită din sumarul Hidroelectrica."""
    raw = _raw_dict(factura)
    return bool(raw.get("_synthetic_current_bill"))


def _ajusteaza_facturi_hidroelectrica(facturi: list[FacturaUtilitate]) -> list[FacturaUtilitate]:
    """Elimină factura sintetică Hidroelectrica când există restanțe reale.

    `GetBill.rembalance` poate reprezenta soldul total al contului. Lista de
    facturi din dashboard trebuie să provină din facturile individuale din
    istoricul de facturare. De aceea, dacă pentru același cont există deja
    cel puțin o factură reală neachitată, rândul sintetic construit din soldul
    total nu mai este afișat ca factură separată.
    """
    rezultat: list[FacturaUtilitate] = []
    facturi_pe_cont: dict[tuple[str, str], list[FacturaUtilitate]] = {}

    for factura in facturi or []:
        facturi_pe_cont.setdefault(_factura_latest_key(factura), []).append(factura)

    for grup in facturi_pe_cont.values():
        exista_factura_reala_neachitata = any(
            not _hidroelectrica_este_factura_curenta_sintetica(factura)
            and _hidroelectrica_are_rest_de_plata(factura)
            for factura in grup
        )

        for factura in grup:
            if (
                exista_factura_reala_neachitata
                and _hidroelectrica_este_factura_curenta_sintetica(factura)
            ):
                continue
            rezultat.append(factura)

    return rezultat



def _build_ebloc_fallback_item(
    coordonator: CoordonatorUtilitatiRomania,
    instantaneu: InstantaneuFurnizor,
    cont: ContUtilitate,
) -> dict[str, Any] | None:
    """Construieste un rand de factura e-bloc din senzorii de cont.

    e-bloc nu expune intotdeauna lista curenta de intretinere intr-un format din
    care putem construi o factura reala. In aceste cazuri avem totusi date utile
    la nivel de cont: sold curent, valoare lista plata sau ultima plata. Pentru
    dashboard afisam un rand informativ, astfel incat locul de consum sa ramana
    vizibil in tabul Facturi si in sumar, fara sa cream entitati suplimentare.
    """
    id_cont = getattr(cont, "id_cont", None)
    if not id_cont:
        return None

    location_key, location_label, manual_group_label = _location_fields(
        coordonator,
        instantaneu,
        cont,
        cont,
    )

    luna = _consum_value(instantaneu, "luna_lista_plata", id_cont)
    scadenta = _format_date(_consum_value(instantaneu, "urmatoarea_scadenta", id_cont))
    data_ultima_plata = _format_date(_consum_value(instantaneu, "data_ultima_plata", id_cont))

    valori_restante = [
        _to_float(_consum_value(instantaneu, "de_plata", id_cont)),
        _to_float(_consum_value(instantaneu, "sold_curent", id_cont)),
        _to_float(_consum_value(instantaneu, "total_neachitat", id_cont)),
    ]
    rest_de_plata = next((valoare for valoare in valori_restante if valoare is not None and valoare > 0), None)

    valoare_lista = _to_float(_consum_value(instantaneu, "valoare_lista_plata", id_cont))
    valoare_ultima_factura = _to_float(_consum_value(instantaneu, "valoare_ultima_factura", id_cont))
    valoare_ultima_plata = _to_float(_consum_value(instantaneu, "valoare_ultima_plata", id_cont))

    if rest_de_plata is not None and rest_de_plata > 0:
        amount = round(rest_de_plata, 2)
        status = "unpaid"
        is_paid = False
        unpaid_amount = amount
        invoice_title = f"Intretinere {luna}" if luna else "Intretinere"
        issue_date = None
        invoice_suffix = luna or scadenta or "curenta"
    elif valoare_lista is not None and valoare_lista > 0:
        amount = round(valoare_lista, 2)
        status = "paid"
        is_paid = True
        unpaid_amount = 0.0
        invoice_title = f"Intretinere {luna}" if luna else "Intretinere"
        issue_date = None
        invoice_suffix = luna or scadenta or "achitata"
    elif valoare_ultima_factura is not None and valoare_ultima_factura > 0:
        amount = round(valoare_ultima_factura, 2)
        status = "paid"
        is_paid = True
        unpaid_amount = 0.0
        invoice_title = f"Intretinere {luna}" if luna else "Intretinere achitata"
        issue_date = None
        invoice_suffix = luna or data_ultima_plata or "ultima_factura"
    elif valoare_ultima_plata is not None and valoare_ultima_plata > 0:
        amount = round(valoare_ultima_plata, 2)
        status = "paid"
        is_paid = True
        unpaid_amount = 0.0
        invoice_title = "Ultima plata"
        issue_date = data_ultima_plata
        invoice_suffix = data_ultima_plata or "ultima_plata"
    else:
        return None

    invoice_id = f"ebloc_{id_cont}_{slugify_text(str(invoice_suffix))}"

    item = {
        "entry_id": coordonator.intrare.entry_id,
        "entry_title": coordonator.intrare.title,
        "furnizor": instantaneu.furnizor,
        "furnizor_label": _provider_label(instantaneu.furnizor),
        "locatie_cheie": location_key,
        "eticheta_locatie": location_label,
        "adresa_originala": getattr(cont, "adresa", None),
        "eticheta_grupare_manuala": manual_group_label,
        "id_cont": id_cont,
        "id_apartament": (
            cont.date_brute.get("id_apartament")
            if isinstance(cont.date_brute, dict)
            else None
        ),
        "id_contract": getattr(cont, "id_contract", None),
        "nume_cont": getattr(cont, "nume", None),
        "tip_utilitate": "administrare_bloc",
        "tip_serviciu": "Intretinere",
        "invoice_id": invoice_id,
        "invoice_number": None,
        "numar_factura": None,
        "document_number": None,
        "invoice_title": invoice_title,
        "issue_date": issue_date,
        "due_date": scadenta,
        "amount": amount,
        "currency": "RON",
        "status_raw": status,
        "status": status,
        "payment_status": status,
        "is_paid": is_paid,
        "unpaid_amount": unpaid_amount,
        "pdf_url": None,
        "refresh_button_entity_id": _refresh_button_entity_id(coordonator),
        "can_refresh": _refresh_button_entity_id(coordonator) is not None,
        "sursa": "fallback_consum",
        "reading_available": True,
        "reading_is_open": normalize_text(_consum_value(instantaneu, "citire_index_permisa", id_cont)).lower() in {"da", "yes", "true", "1", "on"},
        "reading_period": _consum_value(instantaneu, "perioada_citire", id_cont),
        "reading_days_until": _consum_value(instantaneu, "zile_pana_citire_index", id_cont),
    }
    return item

def _exista_restanta_hidroelectrica_pentru_cont(grouped: dict[tuple[str, ...], dict[str, Any]], item: dict[str, Any]) -> bool:
    id_cont = str(item.get("id_cont") or "").strip()
    id_contract = str(item.get("id_contract") or "").strip()
    if not id_cont and not id_contract:
        return False
    for existent in grouped.values():
        if normalize_text(existent.get("furnizor")).lower() != "hidroelectrica":
            continue
        if existent.get("status") != "unpaid":
            continue
        if id_cont and str(existent.get("id_cont") or "").strip() == id_cont:
            return True
        if id_contract and str(existent.get("id_contract") or "").strip() == id_contract:
            return True
    return False

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

    if furnizor == "nova":
        identificator_punct = (
            item.get("id_punct_consum")
            or item.get("numar_punct_consum")
            or item.get("cod_punct_consum")
            or item.get("id_contract")
            or item.get("id_cont")
            or item.get("invoice_id")
            or ""
        )
        return (locatie, furnizor, normalize_text(identificator_punct).lower())

    if furnizor == "hidroelectrica":
        identificator_cont = (
            item.get("id_cont")
            or item.get("id_contract")
            or item.get("nume_cont")
            or ""
        )
        factura_id = _curata_identificator_factura(item.get("invoice_id"))
        if item.get("status") == "unpaid" and factura_id:
            return (locatie, furnizor, normalize_text(identificator_cont).lower(), normalize_text(factura_id).lower())
        return (locatie, furnizor, normalize_text(identificator_cont).lower())

    if furnizor == "ebloc":
        # Doua apartamente din aceeasi asociatie au aceeasi locatie de grupare.
        # Fara un discriminator stabil per apartament, al doilea rand il
        # suprascrie pe primul in dictionarul de agregare al dashboard-ului.
        identificator_apartament = (
            item.get("id_cont")
            or item.get("id_apartament")
            or item.get("nume_cont")
            or item.get("invoice_id")
            or ""
        )
        return (locatie, furnizor, normalize_text(identificator_apartament).lower())

    if furnizor in {"apa_brasov", "apa_oradea", "apa_galati", "aparegio", "hidro_prahova"}:
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

    # La unii furnizori istoricul și sumarul curent pot ajunge în același rând
    # afișat în dashboard. Rândul este deja agregarea vizibilă pentru utilizator,
    # deci la numărătoarea din antet trebuie contată o singură factură restantă.
    # Altfel, valoarea financiară rămâne corectă, dar badge-ul „Neplătite” se dublează.
    if normalize_text(item.get("furnizor")).lower() in {"digi", "hidroelectrica"}:
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



def _loc_consum_key_for_item(item: dict[str, Any]) -> str | None:
    entry_id = item.get("entry_id")
    furnizor = item.get("furnizor")
    furnizor_key = normalize_text(furnizor).lower()

    if furnizor_key == "nova":
        # Nova poate intoarce mai multe puncte de consum sau conturi vechi sub
        # acelasi cont client. Daca folosim prioritar id_cont, toate facturile
        # Nova ajung sub aceeasi cheie si utilizatorul nu poate ascunde separat
        # punctele vechi aparute in dashboard. Pentru Nova aliniem cheia de
        # vizibilitate cu gruparea facturii din dashboard: locatie + punct/cont.
        locatie = str(item.get("locatie_cheie") or "").strip()
        identificator = (
            item.get("id_punct_consum")
            or item.get("numar_punct_consum")
            or item.get("cod_punct_consum")
            or item.get("id_contract")
            or item.get("id_cont")
            or item.get("invoice_id")
            or item.get("invoice_title")
        )
        identificator_text = normalize_text(str(identificator or "")).lower()
        locatie_text = normalize_text(locatie).lower()
        if locatie_text or identificator_text:
            entry = str(entry_id or "").strip()
            provider = normalize_text(str(furnizor or "")).lower()
            if entry and provider:
                return f"{entry}:{provider}:locatie_factura:{locatie_text}:{identificator_text}"

    return construieste_cheie_loc_consum(
        entry_id,
        furnizor,
        id_cont=item.get("id_cont"),
        id_contract=item.get("id_contract"),
        locatie_cheie=item.get("locatie_cheie"),
        eticheta=item.get("eticheta_locatie"),
    )


def _aplica_metadate_loc_consum(hass, item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    cheie = _loc_consum_key_for_item(item)
    item["loc_consum_key"] = cheie
    item["loc_consum_ignorat"] = este_loc_consum_ignorat(hass, cheie)
    return item


def _item_este_ignorat(hass, item: dict[str, Any] | None) -> bool:
    if item is None:
        return False
    cheie = item.get("loc_consum_key") or _loc_consum_key_for_item(item)
    return este_loc_consum_ignorat(hass, cheie)


def _loc_consum_din_cont(coordonator: CoordonatorUtilitatiRomania, instantaneu: InstantaneuFurnizor, cont: ContUtilitate) -> dict[str, Any] | None:
    location_key, location_label, manual_group_label = _location_fields(
        coordonator,
        instantaneu,
        cont,
        cont,
    )
    entry_id = getattr(coordonator.intrare, "entry_id", None)
    furnizor = getattr(instantaneu, "furnizor", None)
    id_cont = getattr(cont, "id_cont", None)
    id_contract = getattr(cont, "id_contract", None)
    cheie = construieste_cheie_loc_consum(
        entry_id,
        furnizor,
        id_cont=id_cont,
        id_contract=id_contract,
        locatie_cheie=location_key,
        eticheta=location_label,
    )
    if not cheie:
        return None
    ignored = este_loc_consum_ignorat(coordonator.hass, cheie)
    return {
        "cheie": cheie,
        "ignored": ignored,
        "entry_id": entry_id,
        "entry_title": getattr(coordonator.intrare, "title", None),
        "furnizor": furnizor,
        "furnizor_label": _provider_label(furnizor),
        "locatie_cheie": location_key,
        "eticheta_locatie": location_label,
        "eticheta_grupare_manuala": manual_group_label,
        "id_cont": id_cont,
        "id_contract": id_contract,
        "nume_cont": getattr(cont, "nume", None),
        "adresa_originala": getattr(cont, "adresa", None),
        "tip_utilitate": getattr(cont, "tip_utilitate", None),
        "tip_serviciu": getattr(cont, "tip_serviciu", None),
        "sursa": "cont",
    }


def _loc_consum_din_factura_item(
    coordonator: CoordonatorUtilitatiRomania,
    instantaneu: InstantaneuFurnizor,
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Construiește un loc de consum dintr-un item de factură.

    Unele platforme, în special Nova, pot returna facturi pentru conturi sau
    puncte de consum care nu mai sunt expuse ca obiecte de cont active. Dacă
    locurile sunt colectate doar din ``instantaneu.conturi``, utilizatorul nu le
    poate ascunde din Setări, deși facturile lor ajung în dashboard. De aceea
    expunem și locațiile rezultate din facturi, folosind aceeași cheie de
    ignorare folosită ulterior la agregare.
    """

    if not isinstance(item, dict):
        return None

    cheie = _loc_consum_key_for_item(item)
    if not cheie:
        return None

    furnizor = item.get("furnizor") or getattr(instantaneu, "furnizor", None)
    hass = getattr(coordonator, "hass", None)
    return {
        "cheie": cheie,
        "ignored": este_loc_consum_ignorat(hass, cheie) if hass is not None else False,
        "entry_id": item.get("entry_id") or getattr(getattr(coordonator, "intrare", None), "entry_id", None),
        "entry_title": item.get("entry_title") or getattr(getattr(coordonator, "intrare", None), "title", None),
        "furnizor": furnizor,
        "furnizor_label": item.get("furnizor_label") or _provider_label(furnizor),
        "locatie_cheie": item.get("locatie_cheie"),
        "eticheta_locatie": item.get("eticheta_locatie") or item.get("locatie_cheie") or cheie,
        "eticheta_grupare_manuala": item.get("eticheta_grupare_manuala"),
        "id_cont": item.get("id_cont"),
        "id_contract": item.get("id_contract"),
        "id_punct_consum": item.get("id_punct_consum"),
        "cod_punct_consum": item.get("cod_punct_consum"),
        "numar_punct_consum": item.get("numar_punct_consum"),
        "nume_cont": item.get("nume_cont"),
        "adresa_originala": item.get("adresa_originala"),
        "tip_utilitate": item.get("tip_utilitate"),
        "tip_serviciu": item.get("tip_serviciu"),
        "sursa": item.get("sursa") or "factura",
    }


def colecteaza_locuri_consum(hass) -> list[dict[str, Any]]:
    locuri: dict[str, dict[str, Any]] = {}
    domain_data = hass.data.get(DOMENIU, {}) if hasattr(hass, "data") else {}

    for maybe_coord in domain_data.values():
        if not isinstance(maybe_coord, CoordonatorUtilitatiRomania):
            continue
        if maybe_coord.intrare.data.get("furnizor") == FURNIZOR_ADMIN_GLOBAL:
            continue
        instantaneu = maybe_coord.data
        if not isinstance(instantaneu, InstantaneuFurnizor):
            continue
        for cont in instantaneu.conturi or []:
            item = _loc_consum_din_cont(maybe_coord, instantaneu, cont)
            if item is not None:
                locuri[item["cheie"]] = item

        for factura in instantaneu.facturi or []:
            try:
                item_factura = _build_invoice_item(maybe_coord, instantaneu, factura)
            except Exception:  # noqa: BLE001
                continue
            item_loc = _loc_consum_din_factura_item(maybe_coord, instantaneu, item_factura)
            if item_loc is not None and item_loc["cheie"] not in locuri:
                locuri[item_loc["cheie"]] = item_loc

    # Ca masura de siguranta, sincronizam lista de vizibilitate si cu randurile
    # deja agregate pentru dashboard. Asa apar in Setari inclusiv locatiile
    # construite doar in agregator, cum sunt unele conturi Nova vechi.
    for item_agregat in colecteaza_facturi_agregate(hass):
        if not isinstance(item_agregat, dict):
            continue
        item_loc = _loc_consum_din_factura_item(
            None,
            None,
            {**item_agregat, "sursa": "factura_agregata"},
        )
        if item_loc is not None and item_loc["cheie"] not in locuri:
            locuri[item_loc["cheie"]] = item_loc

    # Păstrăm și locurile ignorate care nu mai sunt returnate temporar de furnizor,
    # ca utilizatorul să le poată reactiva fără să editeze manual storage-ul.
    for cheie, value in obtine_locuri_ignorate(hass).items():
        if cheie in locuri:
            continue
        if not isinstance(value, dict):
            continue
        locuri[cheie] = {
            "cheie": cheie,
            "ignored": True,
            "entry_id": value.get("entry_id"),
            "entry_title": None,
            "furnizor": value.get("furnizor"),
            "furnizor_label": _provider_label(value.get("furnizor")),
            "locatie_cheie": value.get("locatie_cheie"),
            "eticheta_locatie": value.get("eticheta") or value.get("locatie_cheie") or cheie,
            "id_cont": value.get("id_cont"),
            "id_contract": value.get("id_contract"),
            "nume_cont": None,
            "adresa_originala": None,
            "sursa": "ignorat",
        }

    nova_entries_cu_locatii_din_facturi = {
        str(item.get("entry_id") or "")
        for item in locuri.values()
        if normalize_text(item.get("furnizor")).lower() == "nova"
        and item.get("sursa") in {"factura", "factura_agregata"}
    }
    if nova_entries_cu_locatii_din_facturi:
        for cheie, item in list(locuri.items()):
            if (
                normalize_text(item.get("furnizor")).lower() == "nova"
                and item.get("sursa") == "cont"
                and str(item.get("entry_id") or "") in nova_entries_cu_locatii_din_facturi
            ):
                locuri.pop(cheie, None)

    rezultat = list(locuri.values())
    rezultat.sort(key=lambda item: (
        1 if item.get("ignored") else 0,
        normalize_text(item.get("furnizor_label")).lower(),
        normalize_text(item.get("eticheta_locatie")).lower(),
    ))
    return rezultat

def colecteaza_facturi_agregate(hass) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
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
        if instantaneu.furnizor == "hidroelectrica":
            facturi_de_afisat = _ajusteaza_facturi_hidroelectrica(facturi_de_afisat)

        if instantaneu.furnizor in {"engie", "apa_brasov", "hidroelectrica"}:
            # Pentru MyENGIE, Apă Brașov și Hidroelectrica păstrăm în card doar ultima factură
            # pe fiecare loc de consum. La Hidroelectrica păstrăm însă și facturile vechi
            # care au rest de plată explicit, deoarece pot exista mai multe facturi neachitate
            # pe același contract, cu scadențe diferite.
            latest_ids = _latest_invoice_ids_by_group(facturi_de_afisat)
            facturi_de_afisat = [
                factura for factura in facturi_de_afisat
                if str(getattr(factura, "id_factura", "") or "") in latest_ids
                or (instantaneu.furnizor == "hidroelectrica" and _hidroelectrica_are_rest_de_plata(factura))
            ]

        # 1. Facturi reale, dacă există
        for factura in facturi_de_afisat:
            item = _apply_manual_invoice_status(hass, _build_invoice_item(maybe_coord, instantaneu, factura))
            item = _aplica_metadate_loc_consum(hass, item)
            if _item_este_ignorat(hass, item):
                continue

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

        # 2. Fallback e-bloc din consumuri/plati, pentru cazurile in care
        # portalul nu expune lista curenta ca factura reala, dar avem sold,
        # valoare lista sau ultima plata la nivel de apartament.
        if instantaneu.furnizor == "ebloc":
            for cont in instantaneu.conturi or []:
                fallback_item = _apply_manual_invoice_status(
                    hass,
                    _build_ebloc_fallback_item(maybe_coord, instantaneu, cont),
                )
                fallback_item = _aplica_metadate_loc_consum(hass, fallback_item)
                if _item_este_ignorat(hass, fallback_item):
                    continue
                if fallback_item is None:
                    continue

                group_key = _cheie_grupare_factura(fallback_item)
                if group_key not in grouped:
                    grouped[group_key] = _initializeaza_agregare_item(fallback_item)

        # 3. Fallback specific E.ON din consumuri, doar pentru corectarea statusului curent
        if instantaneu.furnizor == "eon":
            for cont in instantaneu.conturi or []:
                fallback_item = _apply_manual_invoice_status(hass, _build_eon_fallback_item(maybe_coord, instantaneu, cont))
                fallback_item = _aplica_metadate_loc_consum(hass, fallback_item)
                if _item_este_ignorat(hass, fallback_item):
                    continue
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

        # 4. Fallback Hidroelectrica din senzorii de cont.
        # Uneori istoricul de facturi nu mai întoarce o factură utilizabilă,
        # dar coordonatorul are în continuare ultima valoare, scadența și statusul.
        if instantaneu.furnizor == "hidroelectrica":
            for cont in instantaneu.conturi or []:
                fallback_item = _apply_manual_invoice_status(
                    hass,
                    _build_hidroelectrica_fallback_item(maybe_coord, instantaneu, cont),
                )
                fallback_item = _aplica_metadate_loc_consum(hass, fallback_item)
                if _item_este_ignorat(hass, fallback_item):
                    continue
                if fallback_item is None:
                    continue

                if fallback_item.get("status") == "unpaid" and _exista_restanta_hidroelectrica_pentru_cont(grouped, fallback_item):
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



def _este_estimare_hidroelectrica_item(item: dict[str, Any]) -> bool:
    """Detectează rândurile de estimare Hidroelectrica afișate separat de factură."""
    text = normalize_text(
        " ".join(
            str(item.get(key) or "")
            for key in ("invoice_title", "invoice_id", "status_raw", "tip_serviciu", "tip_utilitate")
        )
    ).lower()
    return "estimare" in text


def _cheie_restanta_hidroelectrica_item(item: dict[str, Any]) -> tuple[str, str, str]:
    """Grupează rândurile Hidroelectrica care aparțin aceluiași loc de consum."""
    locatie = normalize_text(item.get("locatie_cheie") or item.get("eticheta_locatie") or "").lower()
    contract = normalize_text(item.get("id_contract") or "").lower()
    cont = normalize_text(item.get("id_cont") or "").lower()
    nume = normalize_text(item.get("nume_cont") or item.get("adresa_originala") or "").lower()
    return (locatie, contract, cont or nume)


def _ajusteaza_itemuri_hidroelectrica_sold_cumulat(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Corectează rândurile Hidroelectrica în care soldul total dublează o estimare.

    Portalul Hidroelectrica poate expune simultan o estimare individuală și un rând
    de tip factură/sold curent care conține totalul tuturor restanțelor. În dashboard
    acestea trebuie afișate ca două rânduri distincte reale: estimarea rămâne cu
    valoarea ei, iar rândul de sold total este redus la diferența neacoperită.
    """
    grupuri: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    for item in items:
        if normalize_text(item.get("furnizor")).lower() != "hidroelectrica":
            continue
        if item.get("status") != "unpaid":
            continue
        grupuri.setdefault(_cheie_restanta_hidroelectrica_item(item), []).append(item)

    for grup in grupuri.values():
        if len(grup) < 2 or not any(_este_estimare_hidroelectrica_item(item) for item in grup):
            continue

        valori = [(item, _valoare_neplatita_item(item)) for item in grup]
        valori = [(item, valoare) for item, valoare in valori if valoare > 0]
        if len(valori) < 2:
            continue

        candidat, valoare_candidat = max(valori, key=lambda pair: pair[1])
        total_alte_randuri = round(sum(valoare for item, valoare in valori if item is not candidat), 2)
        diferenta = round(valoare_candidat - total_alte_randuri, 2)

        if total_alte_randuri <= 0 or diferenta <= 0.01 or diferenta >= valoare_candidat:
            continue

        candidat["amount_original"] = candidat.get("amount")
        candidat["unpaid_amount_original"] = candidat.get("unpaid_amount")
        candidat["unpaid_total_original"] = candidat.get("unpaid_total")
        candidat["amount"] = diferenta
        candidat["unpaid_amount"] = diferenta
        candidat["unpaid_total"] = diferenta
        candidat["hidroelectrica_sold_cumulat_ajustat"] = True
        candidat["hidroelectrica_sold_cumulat_acoperit"] = total_alte_randuri

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
