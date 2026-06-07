from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://gwss.engie.ro/myservices"
URL_AUTENTIFICARE = f"{URL_BAZA}/v1/login"
URL_REFRESH_TOKEN = "https://auth.engie.ro/oauth/token"
URL_PROFIL = f"{URL_BAZA}/v1/user/me"
URL_LOCURI_CONSUM = f"{URL_BAZA}/v1/placesofconsumption"
URL_SOLD = f"{URL_BAZA}/v1/invoices/ballance-details"
URL_WIDGET_SOLD = f"{URL_BAZA}/v1/widgets/ballance"
URL_CONTRACTE = f"{URL_BAZA}/v1/contracts"
URL_PARTENER = f"{URL_BAZA}/v1/partner/details"
CLIENT_ID_OAUTH = "hMpDTLmC0C8szydob7zqUs231mQoDuyK"
AUDIENTA_OAUTH = "https://myservices.engie.ro"
TOKEN_EXPIRA_IMPLICIT = 7200
TOKEN_REFRESH_INAINTE = 300
TIMEOUT_API = aiohttp.ClientTimeout(total=30)

HEADERS_DISPOZITIV = {
    "Accept": "application/json",
    "source": "android",
    "App-Version": "2.1.11",
    "App-Build": "177",
    "OS-Version": "14",
    "OS-Platform": "android",
    "Device-Type": "phone",
    "Device-Manufacturer": "Samsung",
    "Device-Model": "SM-S926B",
    "Screen-Height": "2340",
    "Screen-Width": "1080",
    "Device-Id": "utilitati-romania",
    "User-Agent": "UtilitatiRomania/1.0 MyENGIE/2.1.11",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return ""
    if isinstance(value, (list, tuple, set)):
        return ""
    return str(value).strip()


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        text = str(value).strip()
        if not text:
            return None

        # ENGIE trimite uneori sumele ca text afișat în portal, de forma
        # "168,80 lei" sau "1.147,78 lei". Normalizăm formatul românesc
        # înainte de conversia în float.
        text = text.replace("\xa0", " ").strip()
        match = re.search(r"-?[0-9][0-9\s.,]*", text)
        if not match:
            return None
        number = match.group(0).replace(" ", "")
        if "," in number and "." in number:
            number = number.replace(".", "").replace(",", ".")
        elif "," in number:
            number = number.replace(",", ".")
        return float(number)
    except (TypeError, ValueError):
        return None


def _round_money(value: Any) -> float | None:
    number = _float_or_none(value)
    return round(number, 2) if number is not None else None


def _parse_date(value: Any) -> date | None:
    if value in (None, "", "None"):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _unwrap(raw: Any) -> Any:
    if isinstance(raw, dict) and "data" in raw:
        return raw.get("data")
    return raw


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _valoare_din_chei(data: dict[str, Any], chei: tuple[str, ...]) -> Any:
    for cheie in chei:
        if cheie in data and data.get(cheie) not in (None, ""):
            return data.get(cheie)
    return None


def _tip_serviciu(value: Any) -> str:
    text = _text(value).lower()
    if text in {"gaz", "gas", "gaze", "gaze naturale"}:
        return "gaz"
    if text in {"elec", "electricitate", "energie electrica", "energie electrică", "curent"}:
        return "curent"
    return text or "energie"


def _tip_utilitate(tip: str) -> str:
    if tip == "gaz":
        return "gaz"
    if tip == "curent":
        return "energie electrică"
    return "energie"


def _stare_factura(factura: dict[str, Any], sold: float | None = None) -> str | None:
    status = _text(_valoare_din_chei(factura, ("status", "state", "payment_status", "invoice_status", "paid"))).lower()
    if status:
        if status in {"paid", "platita", "plătită", "achitata", "achitată", "true"}:
            return "plătită"
        if status in {"unpaid", "restanta", "restantă", "neachitata", "neachitată", "false"}:
            return "neplătită"
        return status
    if sold is not None:
        return "neplătită" if sold > 0 else "plătită"
    return None


def _valoare_factura(factura: dict[str, Any]) -> float | None:
    return _round_money(_valoare_din_chei(factura, (
        "total", "amount", "value", "invoice_value", "invoiceValue", "invoice_amount", "invoiceAmount",
        "suma", "sold", "balance", "payment_amount", "amount_to_pay", "amountToPay",
        "valoare", "valoare_factura", "valoareFactura", "valoare_factură", "valoareFacturaLei",
        "valoare_totala", "valoareTotala", "valoare_totală", "valoareFacturaTotal",
        "rest_de_plata", "restDePlata", "rest_de_plată", "restPlata", "rest_plata",
        "suma_de_plata", "sumaDePlata", "suma_de_plată", "total_de_plata", "totalDePlata",
    )))


def _sold_factura(factura: dict[str, Any]) -> float | None:
    return _round_money(_valoare_din_chei(factura, (
        "rest", "remaining", "remaining_amount", "remainingAmount",
        "unpaid", "unpaid_amount", "unpaidAmount", "unpaid_value", "unpaidValue",
        "sold", "balance", "ballance", "debt",
        "rest_de_plata", "restDePlata", "rest_de_plată", "restDePlată",
        "rest_plata", "restPlata", "restToPay", "rest_to_pay",
        "amount_due", "amountDue", "due_amount", "dueAmount",
        "suma_neachitata", "sumaNeachitata", "suma_neachitată",
        "valoare_de_plata", "valoareDePlata", "valoare_de_plată",
    )))


def _data_emitere_factura(factura: dict[str, Any]) -> date | None:
    return _parse_date(_valoare_din_chei(factura, ("issue_date", "issued_at", "invoice_date", "invoiceDate", "emission_date", "created_at", "data_emitere", "dataEmitere", "emisa_la", "emisaLa", "issued")))


def _data_scadenta_factura(factura: dict[str, Any]) -> date | None:
    return _parse_date(_valoare_din_chei(factura, (
        "due_date", "dueDate", "scadenta", "scadență", "data_scadenta", "dataScadenta",
        "data_scadenței", "data_scadentei", "dataScadentei", "pay_until", "payUntil",
        "termen_plata", "termenPlata", "termen_de_plata", "termenDePlata", "due"
    )))


def _id_factura(factura: dict[str, Any], fallback: str) -> str:
    return _text(_valoare_din_chei(factura, (
        "id", "invoice_id", "invoiceId", "invoice_number", "invoiceNumber", "numar_factura",
        "numarFactura", "număr_factură", "number", "document_number", "documentNumber",
        "nr_factura", "nrFactura", "nr", "invoiceNo", "invoice_no",
    ))) or fallback


def _nume_loc_consum(loc: dict[str, Any]) -> str:
    return _text(_valoare_din_chei(loc, (
        "alias", "nickname", "label", "name", "poc_name", "pocName",
        "loc_consum", "locConsum", "place_name", "placeName", "denumire",
    )))


def _adresa_din_dict(adresa: dict[str, Any]) -> str:
    valori: list[str] = []
    for cheie in (
        "street", "street_name", "streetName", "Street",
        "number", "street_number", "streetNumber", "building", "block",
        "entrance", "floor", "apartment",
        "postal_code", "postalCode", "postcode",
        "city", "City", "locality",
        "county", "County", "district", "district_code",
    ):
        valoare = _text(adresa.get(cheie))
        if valoare and valoare not in valori:
            valori.append(valoare)
    return ", ".join(valori)


def _adresa_loc(loc: dict[str, Any]) -> str:
    valoare_adresa = _valoare_din_chei(loc, (
        "address", "full_address", "fullAddress", "consumption_place_address",
        "address_line", "addressLine", "adresa",
    ))

    if isinstance(valoare_adresa, dict):
        direct = _adresa_din_dict(valoare_adresa)
    else:
        direct = _text(valoare_adresa)

    if direct:
        return direct

    valori: list[str] = []
    for cheie in (
        "street", "street_name", "streetName", "Street",
        "number", "street_number", "streetNumber",
        "city", "City", "locality",
        "county", "County",
    ):
        value = _text(loc.get(cheie))
        if value and value not in valori:
            valori.append(value)
    return ", ".join(valori) if valori else ""


def _extrage_locuri(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for cheie in ("places_of_consumption", "placesOfConsumption", "pocs", "items", "list"):
        value = data.get(cheie)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _extrage_divizii(data: Any) -> list[dict[str, Any]]:
    data = _unwrap(data)
    if isinstance(data, list):
        divizii: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                divizii.extend(_extrage_divizii(item))
        return divizii
    if not isinstance(data, dict):
        return []
    for cheie in ("divisions", "utilities", "services", "installations"):
        value = data.get(cheie)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    if data.get("division") or data.get("installations") or data.get("installation_number"):
        return [data]
    return []


def _contract_account(loc: dict[str, Any], divizie: dict[str, Any] | None = None) -> str:
    divizie = divizie or {}
    chei = (
        "contract_account", "contractAccount", "contract_account_number", "contractAccountNumber",
        "contract_account_code", "contractAccountCode", "contract_number", "contractNumber",
        "ca", "ca_number", "caNumber", "account_number", "accountNumber",
    )

    direct = _text(_valoare_din_chei(divizie, chei)) or _text(_valoare_din_chei(loc, chei))
    if direct:
        return direct

    # Portalul MyENGIE ține conturile contractuale în locul de consum, în lista
    # `cont_contract`, fiecare element având `contract_account_number`. Dacă nu
    # citim această structură, soldul de la `widgets/ballance` nu poate fi mapat
    # corect pe locație, iar conturile cu sold negativ ajung să cadă pe fallback.
    for container in (loc.get("cont_contract"), loc.get("contracts"), loc.get("contracte")):
        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                contract = _text(_valoare_din_chei(item, chei))
                if contract:
                    return contract
        elif isinstance(container, dict):
            contract = _text(_valoare_din_chei(container, chei))
            if contract:
                return contract

    return ""


def _contracte_loc(loc: dict[str, Any]) -> list[str]:
    chei = (
        "contract_account", "contractAccount", "contract_account_number", "contractAccountNumber",
        "contract_account_code", "contractAccountCode", "contract_number", "contractNumber",
        "ca", "ca_number", "caNumber", "account_number", "accountNumber",
    )
    rezultate: list[str] = []

    direct = _text(_valoare_din_chei(loc, chei))
    if direct:
        rezultate.append(direct)

    for container in (loc.get("cont_contract"), loc.get("contracts"), loc.get("contracte")):
        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                contract = _text(_valoare_din_chei(item, chei))
                if contract and contract not in rezultate:
                    rezultate.append(contract)
        elif isinstance(container, dict):
            contract = _text(_valoare_din_chei(container, chei))
            if contract and contract not in rezultate:
                rezultate.append(contract)

    return rezultate


def _partener(loc: dict[str, Any], divizie: dict[str, Any] | None = None) -> str:
    divizie = divizie or {}
    chei = (
        "pa", "partner", "partner_id", "partnerId", "partner_account", "partnerAccount",
        "clientId", "client_id", "customer_id", "customerId",
    )

    direct = _text(_valoare_din_chei(divizie, chei)) or _text(_valoare_din_chei(loc, chei))
    if direct:
        return direct

    for container in (loc.get("cont_contract"), loc.get("contracts"), loc.get("contracte")):
        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                partener = _text(_valoare_din_chei(item, chei))
                if partener:
                    return partener
        elif isinstance(container, dict):
            partener = _text(_valoare_din_chei(container, chei))
            if partener:
                return partener

    return ""


def _poc(loc: dict[str, Any]) -> str:
    return _text(_valoare_din_chei(loc, ("poc_number", "pocNumber", "poc", "id", "number", "cod_loc_consum")))


def _pod(loc: dict[str, Any], divizie: dict[str, Any] | None = None) -> str:
    divizie = divizie or {}
    return _text(_valoare_din_chei(divizie, ("pod", "pod_number", "podNumber", "cod_pod"))) or _text(
        _valoare_din_chei(loc, ("pod", "pod_number", "podNumber", "cod_pod"))
    )


def _instalatie(divizie: dict[str, Any]) -> str:
    instalatii = divizie.get("installations")
    if isinstance(instalatii, list) and instalatii:
        first = instalatii[0]
        if isinstance(first, dict):
            return _text(_valoare_din_chei(first, ("installation_number", "installationNumber", "number", "id")))
        return _text(first)
    return _text(_valoare_din_chei(divizie, ("installation_number", "installationNumber", "installation", "installation_id")))


def _serie_contor(data: Any) -> str | None:
    if isinstance(data, list):
        for item in data:
            gasit = _serie_contor(item)
            if gasit:
                return gasit
        return None
    if isinstance(data, dict):
        direct = _text(_valoare_din_chei(data, (
            "serie_contor", "meter_serial", "meterSerial", "serial",
            "serie", "counter_serial", "counterSerial", "meter",
        )))
        if direct:
            return direct
        for cheie in ("installations", "meters", "counters", "index_readings", "readings", "data"):
            value = data.get(cheie)
            if isinstance(value, (list, dict)):
                gasit = _serie_contor(value)
                if gasit:
                    return gasit
    return None


def _index_curent(data: Any) -> float | None:
    if isinstance(data, list):
        for item in data:
            gasit = _index_curent(item)
            if gasit is not None:
                return gasit
        return None
    if isinstance(data, dict):
        direct = _float_or_none(_valoare_din_chei(data, (
            "index", "reading", "value", "meter_index", "meterIndex",
            "current_index", "currentIndex", "last_index", "lastIndex",
            "last_reading", "lastReading", "Index",
        )))
        if direct is not None:
            return direct
        for cheie in ("installations", "meters", "counters", "index_readings", "readings", "data"):
            value = data.get(cheie)
            if isinstance(value, (list, dict)):
                gasit = _index_curent(value)
                if gasit is not None:
                    return gasit
    return None


def _pod_din_index(data: Any) -> str | None:
    if isinstance(data, list):
        for item in data:
            gasit = _pod_din_index(item)
            if gasit:
                return gasit
        return None
    if isinstance(data, dict):
        direct = _text(_valoare_din_chei(data, ("pod", "pod_number", "podNumber", "cod_pod", "codPod")))
        if direct:
            return direct
        for cheie in ("installations", "meters", "counters", "data"):
            value = data.get(cheie)
            if isinstance(value, (list, dict)):
                gasit = _pod_din_index(value)
                if gasit:
                    return gasit
    return None


def _interval_autocitire(data: Any) -> tuple[date | None, date | None]:
    if isinstance(data, list):
        for item in data:
            start, end = _interval_autocitire(item)
            if start or end:
                return start, end
        return None, None
    if not isinstance(data, dict):
        return None, None

    interval = data.get("next_read_dates") or data.get("read_interval") or data.get("interval")
    if isinstance(interval, dict):
        start = _parse_date(_valoare_din_chei(interval, ("startDate", "start_date", "start", "from", "fromDate")))
        end = _parse_date(_valoare_din_chei(interval, ("endDate", "end_date", "end", "to", "toDate")))
        if start or end:
            return start, end

    start = _parse_date(_valoare_din_chei(data, (
        "next_read_start", "nextReadStart", "reading_start", "readingStart",
        "start_read_date", "startReadDate", "startDate", "start_date",
    )))
    end = _parse_date(_valoare_din_chei(data, (
        "next_read_end", "nextReadEnd", "reading_end", "readingEnd",
        "end_read_date", "endReadDate", "endDate", "end_date",
    )))
    if start or end:
        return start, end

    for cheie in ("installations", "meters", "counters", "data"):
        value = data.get(cheie)
        if isinstance(value, (list, dict)):
            start, end = _interval_autocitire(value)
            if start or end:
                return start, end

    return None, None


def _perioada_autocitire(data: Any) -> str | None:
    start, end = _interval_autocitire(data)
    if start and end:
        return f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}"
    if start:
        return start.strftime("%d.%m.%Y")
    if end:
        return end.strftime("%d.%m.%Y")
    return None


def _zile_pana_autocitire(data: Any) -> int | None:
    start, end = _interval_autocitire(data)
    azi = datetime.now(tz=UTC).date()
    if start and azi < start:
        return (start - azi).days
    if start and end and start <= azi <= end:
        return 0
    return None


def _autocitire_permisa(data: Any) -> str | None:
    if isinstance(data, list):
        for item in data:
            gasit = _autocitire_permisa(item)
            if gasit is not None:
                return gasit
        return None
    if not isinstance(data, dict):
        return None

    # În MyENGIE, câmpul permite_index indică faptul că locul de consum acceptă
    # transmiterea indexului, nu neapărat că perioada este deschisă astăzi.
    # Portalul verifică prioritar intervalul next_read_dates, deci facem la fel.
    start, end = _interval_autocitire(data)
    if start and end:
        azi = datetime.now(tz=UTC).date()
        return "da" if start <= azi <= end else "nu"

    for cheie in ("installations", "meters", "counters", "data"):
        value = data.get(cheie)
        if isinstance(value, (list, dict)):
            gasit = _autocitire_permisa(value)
            if gasit is not None:
                return gasit

    for cheie in (
        "is_allowed", "allowed", "can_submit", "canSubmit",
        "reading_allowed", "readingAllowed", "autocitire_permisa",
        "permite_index", "permiteIndex",
    ):
        if cheie in data:
            return "da" if bool(data.get(cheie)) else "nu"
    return None


def _pare_factura(data: dict[str, Any]) -> bool:
    chei_factura = {
        "invoice_number", "invoiceNumber", "numar_factura", "numarFactura", "nr_factura", "nrFactura",
        "document_number", "documentNumber", "issue_date", "invoice_date", "invoiceDate",
        "data_emitere", "dataEmitere", "data_emiterii", "due_date", "dueDate", "scadenta",
        "data_scadenta", "dataScadenta", "rest_de_plata", "restDePlata", "valoare_factura",
        "valoareFactura", "invoice_amount", "invoiceAmount", "amountToPay", "status",
    }
    return any(cheie in data for cheie in chei_factura)


def _lista_facturi(data: Any) -> list[dict[str, Any]]:
    data = _unwrap(data)
    rezultate: list[dict[str, Any]] = []

    def parcurge(value: Any) -> None:
        value = _unwrap(value)
        if isinstance(value, list):
            for item in value:
                parcurge(item)
            return
        if not isinstance(value, dict):
            return

        if _pare_factura(value):
            rezultate.append(value)
            return

        for cheie in ("invoices", "items", "history", "list", "facturi", "bills", "documents", "rows", "values"):
            if cheie in value:
                parcurge(value.get(cheie))

    parcurge(data)
    return rezultate


def _lista_consum(data: Any) -> list[dict[str, Any]]:
    data = _unwrap(data)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for cheie in ("consumption", "items", "values", "chart", "months", "consum", "history"):
            value = data.get(cheie)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []

def _status_factura_text(factura: dict[str, Any]) -> str:
    return _text(_valoare_din_chei(factura, (
        "status", "state", "payment_status", "paymentStatus", "invoice_status",
        "invoiceStatus", "stare", "paid", "achitat", "achitata", "achitată",
        "status_factura", "statusFactura", "paymentState", "payment_state",
    ))).lower()


def _factura_achitata_explicit(factura: dict[str, Any]) -> bool:
    status = _status_factura_text(factura)
    if not status:
        return False
    # Verificăm întâi formele negative, ca să nu tratăm "neachitată" ca "achitată".
    if any(token in status for token in ("neachit", "neplat", "restant", "unpaid", "not_paid")):
        return False
    return (
        status in {"paid", "platita", "plătită", "achitata", "achitată", "true", "1"}
        or "achit" in status
        or "platit" in status
        or "plătit" in status
    )


def _factura_neplatita(factura: dict[str, Any]) -> bool:
    status = _status_factura_text(factura)
    if status:
        if any(token in status for token in ("neachit", "neplat", "restant", "unpaid", "not_paid")):
            return True
        if _factura_achitata_explicit(factura):
            return False
        if status in {"false", "0"}:
            return True

    rest = _sold_factura(factura)
    if rest is not None:
        return rest > 0

    return False


def _de_plata_din_facturi(facturi: list[dict[str, Any]]) -> float | None:
    total = 0.0
    gasit = False
    for factura in facturi:
        if not _factura_neplatita(factura):
            continue
        rest = _sold_factura(factura)
        valoare = rest if rest is not None else _valoare_factura(factura)
        if valoare is None or valoare <= 0:
            continue
        total += valoare
        gasit = True
    if gasit:
        return round(total, 2)

    if facturi:
        ultima = sorted(
            facturi,
            key=lambda x: _data_emitere_factura(x) or _data_scadenta_factura(x) or date.min,
            reverse=True,
        )[0]

        # Dacă istoricul spune explicit că restul de plată este zero, nu folosim
        # valoarea facturii ca fallback. Acoperă situațiile cu sold negativ/credit,
        # unde ultima factură există, dar în portal apare achitată.
        rest_ultima = _sold_factura(ultima)
        if rest_ultima is not None and rest_ultima <= 0:
            return 0.0

        # Fallback prudent: dacă ultima factură are scadență viitoare și nu este
        # marcată explicit ca achitată, o folosim ca sumă de plată. Acoperă cazul
        # ENGIE în care istoricul conține valoarea și scadența, dar câmpul REST
        # vine ca text sau lipsește din unele răspunsuri.
        if not _factura_achitata_explicit(ultima):
            scadenta = _data_scadenta_factura(ultima)
            valoare = rest_ultima if rest_ultima is not None else _valoare_factura(ultima)
            if valoare is not None and valoare > 0 and scadenta and scadenta >= datetime.now(tz=UTC).date():
                return round(valoare, 2)
    return None


def _sold_din_detaliu_loc(loc: dict[str, Any]) -> float | None:
    return _round_money(_valoare_din_chei(loc, (
        "sold", "balance", "ballance", "total", "total_de_plata", "totalDePlata",
        "suma_de_plata", "sumaDePlata", "rest_de_plata", "restDePlata",
        "de_plata", "dePlata", "amount", "amountToPay", "amount_to_pay",
    )))


def _sold_din_raspuns_sold(data: Any, contract: str | None = None, partener: str | None = None) -> float | None:
    data = _unwrap(data)
    if isinstance(data, list):
        for item in data:
            gasit = _sold_din_raspuns_sold(item, contract, partener)
            if gasit is not None:
                return gasit
        return None
    if not isinstance(data, dict):
        return None

    detalii = data.get("details")
    if isinstance(detalii, list):
        for item in detalii:
            if not isinstance(item, dict):
                continue
            item_contract = _text(item.get("contract_account") or item.get("contractAccount"))
            item_partener = _text(item.get("partner") or item.get("pa") or item.get("clientId"))
            contract_ok = bool(contract and item_contract and item_contract == contract)
            partener_ok = bool(partener and item_partener and item_partener == partener)
            if contract_ok or partener_ok:
                valoare = _round_money(item.get("total") or item.get("gaz") or item.get("electricitate"))
                if valoare is not None:
                    return valoare

    return _round_money(_valoare_din_chei(data, (
        "total", "gaz", "electricitate", "sold", "balance", "ballance",
        "total_de_plata", "totalDePlata", "suma_de_plata", "sumaDePlata",
        "rest_de_plata", "restDePlata", "de_plata", "dePlata",
    )))


class ClientApiEngie:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._expira_in = TOKEN_EXPIRA_IMPLICIT
        self._obtinut_monotonic = 0.0
        self._lock = asyncio.Lock()

    def _token_valid(self) -> bool:
        if not self._token:
            return False
        return (time.monotonic() - self._obtinut_monotonic) < max(self._expira_in - TOKEN_REFRESH_INAINTE, 60)

    def _headers(self, *, autentificat: bool = True, formular: bool = False) -> dict[str, str]:
        headers = dict(HEADERS_DISPOZITIV)
        headers["Content-Type"] = "application/x-www-form-urlencoded" if formular else "application/json"
        if autentificat and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _json_sigur(self, raspuns: aiohttp.ClientResponse) -> Any:
        text = await raspuns.text()
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise EroareParsare(f"Răspuns ENGIE invalid pentru {raspuns.url}: {text[:200]}") from err

    async def async_login(self) -> bool:
        async with self._lock:
            return await self._login_fara_lock()

    async def _login_fara_lock(self) -> bool:
        payload = urlencode({"username": self._utilizator, "password": self._parola})
        try:
            async with self._sesiune.post(
                URL_AUTENTIFICARE,
                data=payload,
                headers=self._headers(autentificat=False, formular=True),
                timeout=TIMEOUT_API,
            ) as raspuns:
                if raspuns.status in (401, 403):
                    return False
                if raspuns.status >= 400:
                    raise EroareConectare(f"ENGIE login a returnat HTTP {raspuns.status}")
                data = await self._json_sigur(raspuns)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectare(f"Eroare conectare ENGIE: {err}") from err

        if not isinstance(data, dict) or data.get("error") is True:
            return False

        body = data.get("data") if isinstance(data.get("data"), dict) else data
        token = _text(body.get("token") or body.get("access_token"))
        if not token:
            return False

        self._token = token
        self._refresh_token = _text(body.get("refresh_token")) or None
        expira = body.get("exp") or body.get("expires_in") or TOKEN_EXPIRA_IMPLICIT
        try:
            self._expira_in = int(expira)
        except (TypeError, ValueError):
            self._expira_in = TOKEN_EXPIRA_IMPLICIT
        self._obtinut_monotonic = time.monotonic()
        return True

    async def _refresh_fara_lock(self) -> bool:
        if not self._refresh_token:
            return False
        payload = urlencode({
            "client_id": CLIENT_ID_OAUTH,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "audience": AUDIENTA_OAUTH,
        })
        try:
            async with self._sesiune.post(
                URL_REFRESH_TOKEN,
                data=payload,
                headers=self._headers(autentificat=False, formular=True),
                timeout=TIMEOUT_API,
            ) as raspuns:
                if raspuns.status >= 400:
                    return False
                data = await self._json_sigur(raspuns)
        except Exception:
            return False

        if not isinstance(data, dict):
            return False
        token = _text(data.get("access_token") or data.get("token"))
        if not token:
            return False
        self._token = token
        self._refresh_token = _text(data.get("refresh_token")) or self._refresh_token
        try:
            self._expira_in = int(data.get("expires_in") or data.get("exp") or TOKEN_EXPIRA_IMPLICIT)
        except (TypeError, ValueError):
            self._expira_in = TOKEN_EXPIRA_IMPLICIT
        self._obtinut_monotonic = time.monotonic()
        return True

    async def _asigura_autentificare(self) -> None:
        if self._token_valid():
            return
        async with self._lock:
            if self._token_valid():
                return
            if await self._refresh_fara_lock():
                return
            if not await self._login_fara_lock():
                raise EroareAutentificare("Autentificare ENGIE eșuată")

    async def _request_json(
        self,
        metoda: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
    ) -> Any:
        await self._asigura_autentificare()
        for incercare in range(2):
            try:
                if form_body is not None:
                    headers = self._headers()
                    headers.pop("Content-Type", None)
                    form_data = aiohttp.FormData(default_to_multipart=True)
                    for cheie, valoare in form_body.items():
                        if isinstance(valoare, list):
                            for item in valoare:
                                form_data.add_field(cheie, str(item))
                        else:
                            form_data.add_field(cheie, str(valoare))
                    async with self._sesiune.request(
                        metoda,
                        url,
                        params=params,
                        data=form_data,
                        headers=headers,
                        timeout=TIMEOUT_API,
                    ) as raspuns:
                        if raspuns.status == 401 and incercare == 0:
                            self._obtinut_monotonic = 0.0
                            await self._asigura_autentificare()
                            continue
                        if raspuns.status >= 400:
                            raise EroareConectare(f"ENGIE a returnat HTTP {raspuns.status} pentru {url}")
                        return await self._json_sigur(raspuns)
                async with self._sesiune.request(
                    metoda,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                    timeout=TIMEOUT_API,
                ) as raspuns:
                    if raspuns.status == 401 and incercare == 0:
                        self._obtinut_monotonic = 0.0
                        await self._asigura_autentificare()
                        continue
                    if raspuns.status >= 400:
                        raise EroareConectare(f"ENGIE a returnat HTTP {raspuns.status} pentru {url}")
                    return await self._json_sigur(raspuns)
            except EroareConectare:
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EroareConectare(f"Eroare conectare ENGIE pentru {url}: {err}") from err
        raise EroareAutentificare("Sesiune ENGIE expirată")

    async def async_profil(self) -> dict[str, Any]:
        data = await self._request_json("GET", URL_PROFIL)
        return _unwrap(data) if isinstance(_unwrap(data), dict) else {}

    async def async_locuri_consum(self) -> dict[str, Any]:
        data = await self._request_json("GET", URL_LOCURI_CONSUM)
        return _unwrap(data) if isinstance(_unwrap(data), dict) else {}

    async def async_divizii(self, poc: str, pa: str) -> Any:
        return await self._request_json("GET", f"{URL_LOCURI_CONSUM}/divisions/{poc}", params={"pa": pa})

    async def async_index(self, poc: str, pa: str, division: str, installation: str | None = None, serie: str | None = None) -> Any:
        params: dict[str, Any] = {"poc_number": poc, "division": division, "pa": pa}
        if installation:
            params["installation_number"] = installation
        if serie:
            params["serie_contor"] = serie
        return await self._request_json("GET", f"{URL_BAZA}/v1/index/{poc}", params=params)

    async def async_istoric_facturi(self, poc: str, pa: str, start: str, end: str) -> Any:
        params = {"startDate": start, "endDate": end, "pa": pa}
        try:
            return await self._request_json("GET", f"{URL_BAZA}/v1/invoices/history-only/{poc}", params=params)
        except EroareConectare:
            return await self._request_json("GET", f"{URL_BAZA}/v1/invoices/history/{poc}", params=params)

    async def async_consum_lunar(self, poc: str, pa: str, start: str, end: str) -> Any:
        return await self._request_json("GET", f"{URL_BAZA}/v1/index/consumption/{poc}", params={"startDate": start, "endDate": end, "pa": pa})

    async def async_revizie(self, poc: str, pod: str, pa: str) -> Any:
        return await self._request_json("GET", f"{URL_BAZA}/v1/widgets/newrv/{poc}/{pod}", params={"pa": pa})

    async def async_sold(self, contracte: list[str]) -> Any:
        contracte_curate = [c for c in contracte if c]
        if not contracte_curate:
            return {}
        form = {"contract_account[]": contracte_curate}
        try:
            return await self._request_json("POST", URL_WIDGET_SOLD, form_body=form)
        except EroareConectare:
            try:
                return await self._request_json("POST", URL_SOLD, form_body=form)
            except EroareConectare:
                return await self._request_json("POST", URL_SOLD, json_body={"contract_account": contracte_curate})

    async def async_contracte(self) -> Any:
        return await self._request_json("GET", URL_CONTRACTE)

    async def async_partener(self) -> Any:
        return await self._request_json("GET", URL_PARTENER)


class ClientFurnizorEngie(ClientFurnizor):
    cheie_furnizor = "engie"
    nume_prietenos = "ENGIE România"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiEngie(sesiune, utilizator, parola)

    async def async_testeaza_conexiunea(self) -> str:
        ok = await self.api.async_login()
        if not ok:
            raise EroareAutentificare("Autentificare ENGIE eșuată")
        locuri = _extrage_locuri(await self.api.async_locuri_consum())
        if locuri:
            return _poc(locuri[0]) or self.utilizator.lower()
        return self.utilizator.lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            profil = await self.api.async_profil()
            locuri_raw = await self.api.async_locuri_consum()
        except EroareAutentificare:
            raise
        except EroareConectare:
            raise
        except Exception as err:
            raise EroareParsare(str(err)) from err

        locuri = _extrage_locuri(locuri_raw)
        if not locuri:
            raise EroareParsare("ENGIE nu a returnat locuri de consum")

        azi = datetime.now(tz=UTC).date()
        start = (azi - timedelta(days=365)).isoformat()
        end = azi.isoformat()

        divizii_pe_poc: dict[str, list[dict[str, Any]]] = {}
        contracte_sold: list[str] = []
        for loc in locuri:
            poc = _poc(loc)
            pa = _partener(loc)
            if not poc or not pa:
                continue
            try:
                divizii = _extrage_divizii(await self.api.async_divizii(poc, pa))
            except Exception as err:
                _LOGGER.debug("Nu s-au putut citi diviziile ENGIE pentru %s: %s", poc, err)
                divizii = []
            if not divizii:
                divizii = [{"division": loc.get("division") or loc.get("utility") or "gaz"}]
            divizii_pe_poc[poc] = divizii
            for contract in _contracte_loc(loc):
                if contract and contract not in contracte_sold:
                    contracte_sold.append(contract)
            for divizie in divizii:
                contract = _contract_account(loc, divizie)
                if contract and contract not in contracte_sold:
                    contracte_sold.append(contract)

        try:
            sold_raw = _unwrap(await self.api.async_sold(contracte_sold)) or {}
        except Exception as err:
            _LOGGER.debug("Nu s-a putut citi soldul ENGIE: %s", err)
            sold_raw = {}

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []
        total_de_plata = _round_money(sold_raw.get("total") if isinstance(sold_raw, dict) else None) or 0.0
        total_pozitiv = max(total_de_plata, 0.0)

        detalii_sold = sold_raw.get("details") if isinstance(sold_raw, dict) else []
        sold_pe_contract: dict[str, float] = {}
        sold_pe_partener: dict[str, float] = {}
        if isinstance(detalii_sold, list):
            for item in detalii_sold:
                if not isinstance(item, dict):
                    continue
                contract = _text(item.get("contract_account") or item.get("contractAccount"))
                partener = _text(item.get("partner") or item.get("pa") or item.get("clientId"))
                valoare = _round_money(item.get("total") or item.get("gaz") or item.get("electricitate"))
                if contract and valoare is not None:
                    sold_pe_contract[contract] = valoare
                if partener and valoare is not None:
                    sold_pe_partener[partener] = valoare

        for loc in locuri:
            poc = _poc(loc)
            pa = _partener(loc)
            if not poc or not pa:
                continue
            divizii = divizii_pe_poc.get(poc) or [{"division": loc.get("division") or "gaz"}]
            adresa = _adresa_loc(loc)
            nume_loc = _nume_loc_consum(loc)
            nume_client = _text(
                nume_loc
                or _valoare_din_chei(loc, ("partner_name", "client_name"))
                or _valoare_din_chei(profil, ("name", "full_name", "fullname"))
                or " ".join(x for x in (_text(profil.get("firstName")), _text(profil.get("lastName"))) if x)
                or f"Loc consum {poc[-4:]}"
            )

            for divizie in divizii:
                division = _text(divizie.get("division") or divizie.get("utility") or divizie.get("type") or loc.get("division") or "gaz").lower()
                tip = _tip_serviciu(division)
                tip_util = _tip_utilitate(tip)
                contract = _contract_account(loc, divizie)
                pod = _pod(loc, divizie)
                installation = _instalatie(divizie)
                id_cont = f"{poc}_{tip}"

                index_raw: Any = {}
                invoices_raw: Any = {}
                consumption_raw: Any = {}
                inspection_raw: Any = {}
                try:
                    index_raw = await self.api.async_index(poc, pa, division or tip, installation or None)
                except Exception as err:
                    _LOGGER.debug("Nu s-a putut citi indexul ENGIE pentru %s/%s: %s", poc, tip, err)
                try:
                    invoices_raw = await self.api.async_istoric_facturi(poc, pa, start, end)
                except Exception as err:
                    _LOGGER.debug("Nu s-au putut citi facturile ENGIE pentru %s: %s", poc, err)
                try:
                    consumption_raw = await self.api.async_consum_lunar(poc, pa, start, end)
                except Exception as err:
                    _LOGGER.debug("Nu s-a putut citi consumul ENGIE pentru %s/%s: %s", poc, tip, err)

                facturi_loc = _lista_facturi(invoices_raw)
                consum_lunar = _lista_consum(consumption_raw)
                index_data = _unwrap(index_raw)
                pod_index = _pod_din_index(index_data)
                if not pod and pod_index:
                    pod = pod_index
                if tip == "gaz" and pod:
                    try:
                        inspection_raw = await self.api.async_revizie(poc, pod, pa)
                    except Exception as err:
                        _LOGGER.debug("Nu s-a putut citi revizia ENGIE pentru %s: %s", poc, err)
                revizie_data = _unwrap(inspection_raw)
                sold_contract = sold_pe_contract.get(contract)
                if sold_contract is None:
                    for contract_loc in _contracte_loc(loc):
                        sold_contract = sold_pe_contract.get(contract_loc)
                        if sold_contract is not None:
                            if not contract:
                                contract = contract_loc
                            break
                if sold_contract is None and pa:
                    sold_contract = sold_pe_partener.get(pa)
                if sold_contract is None:
                    sold_contract = _sold_din_detaliu_loc(loc)

                de_plata_facturi = _de_plata_din_facturi(facturi_loc)

                # Pentru conturile cu sold negativ/credit, răspunsul agregat poate fi
                # insuficient sau poate să nu se potrivească perfect cu locul de consum.
                # Cerem și soldul individual pe contract, astfel încât să păstrăm corect
                # valori precum -22,24 RON în loc să cădem pe valoarea ultimei facturi.
                contracte_individuale = [contract] if contract else _contracte_loc(loc)
                for contract_individual in contracte_individuale:
                    if not contract_individual:
                        continue
                    try:
                        sold_individual_raw = await self.api.async_sold([contract_individual])
                        sold_individual = _sold_din_raspuns_sold(sold_individual_raw, contract_individual, pa)
                        if sold_individual is not None:
                            sold_contract = sold_individual
                            if not contract:
                                contract = contract_individual
                            break
                    except Exception as err:
                        _LOGGER.debug("Nu s-a putut citi soldul individual ENGIE pentru %s: %s", contract_individual, err)

                if sold_contract is None:
                    if de_plata_facturi is not None:
                        sold_contract = de_plata_facturi
                    else:
                        sold_contract = total_de_plata if len(locuri) <= 1 else 0.0

                de_plata_contract = max(sold_contract, 0.0)
                if (
                    de_plata_facturi is not None
                    and sold_contract is not None
                    and sold_contract >= 0
                    and (de_plata_contract == 0 or abs(de_plata_facturi - de_plata_contract) > 0.01)
                ):
                    de_plata_contract = de_plata_facturi
                    if sold_contract == 0.0:
                        sold_contract = de_plata_facturi
                ultima_factura = None
                if facturi_loc:
                    ultima_factura = sorted(
                        facturi_loc,
                        key=lambda x: _data_emitere_factura(x) or _data_scadenta_factura(x) or date.min,
                        reverse=True,
                    )[0]

                index_val = _index_curent(index_data)
                serie = _serie_contor(index_data)
                citire_permisa = _autocitire_permisa(index_data)
                perioada_citire = _perioada_autocitire(index_data)
                zile_pana_citire = _zile_pana_autocitire(index_data)

                consumuri.extend([
                    ConsumUtilitate("sold_curent", round(sold_contract, 2), "RON", id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip),
                    ConsumUtilitate("de_plata", round(de_plata_contract, 2), "RON", id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip),
                    ConsumUtilitate("numar_facturi", len(facturi_loc), None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip, date_brute={"facturi": facturi_loc}),
                    ConsumUtilitate("factura_restanta", "da" if de_plata_contract > 0 else "nu", None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip),
                    ConsumUtilitate("consum_lunar", len(consum_lunar), None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip, date_brute={"consum": consum_lunar}),
                ])
                if citire_permisa is not None:
                    consumuri.append(ConsumUtilitate("citire_permisa", citire_permisa, None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip, date_brute={"index": index_data}))
                if perioada_citire:
                    consumuri.append(ConsumUtilitate("perioada_citire", perioada_citire, None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip, date_brute={"index": index_data}))
                if zile_pana_citire is not None:
                    consumuri.append(ConsumUtilitate("zile_pana_citire_index", zile_pana_citire, None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip, date_brute={"index": index_data}))
                if index_val is not None:
                    consumuri.append(ConsumUtilitate("index_contor", round(index_val, 3), "m³" if tip == "gaz" else "kWh", id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip, date_brute={"serie_contor": serie, "index": index_data}))
                if ultima_factura:
                    valoare_ultima = _valoare_factura(ultima_factura)
                    if valoare_ultima is not None:
                        consumuri.append(ConsumUtilitate("valoare_ultima_factura", valoare_ultima, "RON", id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip))
                    scadenta = _data_scadenta_factura(ultima_factura)
                    if scadenta:
                        consumuri.append(ConsumUtilitate("urmatoarea_scadenta", scadenta.isoformat(), None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip))
                if isinstance(revizie_data, dict) and revizie_data:
                    next_date = _parse_date(revizie_data.get("next_inspection_date") or revizie_data.get("next_icu_inspection_date"))
                    overdue = bool(revizie_data.get("next_inspection_is_overdue") or revizie_data.get("next_icu_inspection_is_overdue"))
                    status_revizie = "expirată" if overdue else "validă" if next_date else "necunoscută"
                    consumuri.append(ConsumUtilitate("revizie_tehnica", status_revizie, None, id_cont=id_cont, tip_utilitate=tip_util, tip_serviciu=tip, date_brute=revizie_data))

                conturi.append(ContUtilitate(
                    id_cont=id_cont,
                    nume=nume_client,
                    tip_cont="loc_consum",
                    id_contract=contract or None,
                    adresa=adresa,
                    stare="activ",
                    tip_utilitate=tip_util,
                    tip_serviciu=tip,
                    date_brute={
                        "poc": poc,
                        "pa": pa,
                        "pod": pod,
                        "division": division,
                        "installation_number": installation,
                        "contract_account": contract,
                        "loc": loc,
                        "divizie": divizie,
                        "profil": profil,
                        "index": index_data,
                        "serie_contor": serie,
                        "facturi": facturi_loc,
                        "consum_lunar": consum_lunar,
                        "revizie": revizie_data,
                        "sold": sold_contract,
                    },
                ))

                for idx, factura in enumerate(facturi_loc, start=1):
                    sold_f = _sold_factura(factura)
                    factura_id = _id_factura(factura, f"{poc}-{idx}")
                    facturi.append(FacturaUtilitate(
                        id_factura=factura_id,
                        titlu=f"Factură {factura_id}",
                        valoare=_valoare_factura(factura),
                        moneda="RON",
                        data_emitere=_data_emitere_factura(factura),
                        data_scadenta=_data_scadenta_factura(factura),
                        stare=_stare_factura(factura, sold_f),
                        categorie="factura",
                        id_cont=id_cont,
                        id_contract=contract or None,
                        tip_utilitate=tip_util,
                        tip_serviciu=tip,
                        date_brute=factura,
                    ))

        total_sold_conturi = round(sum(
            float(c.date_brute.get("sold") or 0)
            for c in conturi
            if isinstance(c.date_brute, dict)
        ), 2)
        total_de_plata_conturi = round(sum(
            max(float(c.date_brute.get("sold") or 0), 0.0)
            for c in conturi
            if isinstance(c.date_brute, dict)
        ), 2)
        if total_de_plata == 0.0 and total_de_plata_conturi != 0.0:
            total_de_plata = total_sold_conturi
            total_pozitiv = total_de_plata_conturi

        consumuri.append(ConsumUtilitate("sold_curent", round(total_de_plata, 2), "RON", tip_utilitate="energie"))
        consumuri.append(ConsumUtilitate("de_plata", round(total_pozitiv, 2), "RON", tip_utilitate="energie"))

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={
                "profil": profil,
                "locuri_consum": locuri_raw,
                "sold": sold_raw,
                "contracte_sold": contracte_sold,
            },
        )
