from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import json
import logging
from typing import Any

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://backend.nova-energy.ro/api"
ENDPOINT_LOGIN = "/accounts/login/client"
ENDPOINT_PUNCTE_CONSUM = "/metering-points"
ENDPOINT_FACTURI = "/invoices"
ENDPOINT_PLATI = "/payments"
ENDPOINT_AUTOCITIRI = "/self-readings"
ENDPOINT_PUNCTE_AUTOCITIRE = "/metering-points/self-readings"
ENDPOINT_NOTIFICARI = "/legal-notifications"
ENDPOINT_INCIDENTE = "/incidents"


class EroareApiNova(Exception):
    pass


class EroareAutentificareNova(EroareApiNova):
    pass


class EroareConectareNova(EroareApiNova):
    pass


class EroareRaspunsNova(EroareApiNova):
    pass


@dataclass(slots=True)
class DateSesiuneNova:
    token: str
    expira_la: int


class ClientApiNova:
    def __init__(self, sesiune: aiohttp.ClientSession, email: str, parola: str) -> None:
        self._sesiune = sesiune
        self._email = email
        self._parola = parola
        self._token: str | None = None
        self._token_expira_la: int | None = None
        self.cont: dict[str, Any] = {}
        self.cont_vizualizat: dict[str, Any] = {}

    def _url(self, endpoint: str) -> str:
        return f"{URL_BAZA}{endpoint}"

    def _token_valid(self) -> bool:
        if not self._token or not self._token_expira_la:
            return False
        acum = int(datetime.now(tz=UTC).timestamp())
        return acum < (self._token_expira_la - 60)

    async def _request(self, metoda: str, endpoint: str, *, autentificat: bool = True, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        if autentificat and not self._token_valid():
            await self.async_login()

        antete: dict[str, str] = {"Accept": "application/json"}
        if autentificat:
            if not self._token:
                raise EroareAutentificareNova("Lipsește tokenul de autentificare")
            antete["Authorization"] = f"Bearer {self._token}"
            id_cont = self.cont_vizualizat.get("accountId") or self.cont_vizualizat.get("_id") or self.cont_vizualizat.get("id")
            if id_cont:
                antete["x-account-id"] = str(id_cont)
        if json_data is not None:
            antete["Content-Type"] = "application/json"

        try:
            async with self._sesiune.request(
                metoda,
                self._url(endpoint),
                headers=antete,
                json=json_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    raise EroareAutentificareNova(f"Autentificare eșuată pentru {endpoint}: HTTP {raspuns.status}")
                if raspuns.status >= 400:
                    raise EroareApiNova(f"Nova API a returnat HTTP {raspuns.status} pentru {endpoint}: {text}")
                try:
                    data = await raspuns.json()
                except aiohttp.ContentTypeError as err:
                    raise EroareRaspunsNova(f"Răspuns JSON invalid pentru {endpoint}: {text}") from err
        except EroareApiNova:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareNova(f"Eroare de conectare la {endpoint}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareNova(f"Timeout la {endpoint}") from err

        if not isinstance(data, dict):
            raise EroareRaspunsNova(f"Tip de răspuns neașteptat pentru {endpoint}: {type(data)}")
        return data

    async def async_login(self) -> DateSesiuneNova:
        raspuns = await self._request("POST", ENDPOINT_LOGIN, autentificat=False, json_data={"email": self._email, "password": self._parola})
        data = raspuns.get("data", {})
        sesiune = data.get("session")
        if not raspuns.get("success") or not isinstance(sesiune, dict):
            raise EroareAutentificareNova("Login Nova eșuat: răspuns invalid")
        token = sesiune.get("token")
        expira_la = sesiune.get("expireAt")
        if not token or not expira_la:
            raise EroareAutentificareNova("Login Nova eșuat: lipsă token sau expirare")
        self._token = str(token)
        self._token_expira_la = int(expira_la)
        self.cont = data.get("loggedInAccount", {}) or {}
        self.cont_vizualizat = data.get("viewedAccount", {}) or {}
        return DateSesiuneNova(token=self._token, expira_la=self._token_expira_la)

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        return {
            "account": self.cont,
            "viewed_account": self.cont_vizualizat,
            "metering_points": await self.async_get_metering_points(),
        }

    async def async_get_metering_points(self) -> list[dict[str, Any]]:
        raspuns = await self._request("GET", ENDPOINT_PUNCTE_CONSUM)
        data = raspuns.get("data")
        if isinstance(data, dict) and isinstance(data.get("docs"), list):
            return data["docs"]
        if isinstance(data, list):
            return data
        if isinstance(raspuns.get("docs"), list):
            return raspuns["docs"]
        return []

    async def async_get_invoices(self) -> dict[str, Any]:
        raspuns = await self._request("GET", ENDPOINT_FACTURI)
        docs = raspuns.get("docs", [])
        if not isinstance(docs, list):
            return {"invoices": [], "balance": {}}
        facturi: list[dict[str, Any]] = []
        balanta: dict[str, Any] = {}
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if not balanta and isinstance(doc.get("balance"), dict):
                balanta = doc["balance"]
            nested = doc.get("invoices")
            if isinstance(nested, list):
                facturi.extend([f for f in nested if isinstance(f, dict)])
            else:
                facturi.append(doc)
        return {"invoices": facturi, "balance": balanta}

    async def async_get_docs(self, endpoint: str) -> list[dict[str, Any]]:
        raspuns = await self._request("GET", endpoint)
        docs = raspuns.get("docs", [])
        return docs if isinstance(docs, list) else []

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._token_valid():
            await self.async_login()
        facturi = await self.async_get_invoices()
        return {
            "account": self.cont,
            "viewed_account": self.cont_vizualizat,
            "metering_points": await self.async_get_metering_points(),
            "invoices": facturi.get("invoices", []),
            "invoice_balance": facturi.get("balance", {}),
            "payments": await self.async_get_docs(ENDPOINT_PLATI),
            "self_readings": await self.async_get_docs(ENDPOINT_AUTOCITIRI),
            "metering_points_self_readings": await self.async_get_docs(ENDPOINT_PUNCTE_AUTOCITIRE),
            "legal_notifications": await self.async_get_docs(ENDPOINT_NOTIFICARI),
            "incidents": await self.async_get_docs(ENDPOINT_INCIDENTE),
        }


class ClientFurnizorNova(ClientFurnizor):
    cheie_furnizor = "nova"
    nume_prietenos = "Nova Power & Gas"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiNova(sesiune=sesiune, email=utilizator, parola=parola)

    async def async_testeaza_conexiunea(self) -> str:
        try:
            rezultat = await self.api.async_validate_credentials()
        except EroareAutentificareNova as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareNova as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsNova as err:
            raise EroareParsare(str(err)) from err
        cont = rezultat.get("viewed_account", {}) or {}
        return str(cont.get("accountNumber") or cont.get("_id") or self.utilizator)

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            date_brute = await self.api.async_get_all_data()
        except EroareAutentificareNova as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareNova as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsNova as err:
            raise EroareParsare(str(err)) from err

        _logheaza_diagnostic_nova(date_brute)

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute)
        consumuri = self._mapeaza_consumuri(date_brute, conturi)
        extra = self._construieste_extra(date_brute, facturi)

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra=extra,
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        rezultate: list[ContUtilitate] = []
        for punct in date_brute.get("metering_points", []) or []:
            id_cont = str(punct.get("meteringPointId") or punct.get("_id") or punct.get("id") or "").strip()
            if not id_cont:
                continue
            adresa = punct.get("address")
            if isinstance(adresa, dict):
                adresa = ", ".join(str(x) for x in [adresa.get("city"), adresa.get("street"), adresa.get("number"), adresa.get("postalCode")] if x)
            tip_serviciu = _normalizeaza_tip_serviciu(
                punct.get("utilityType")
                or punct.get("utility")
                or punct.get("serviceType")
                or punct.get("commodity")
                or punct.get("type")
                or ""
            )
            rezultate.append(
                ContUtilitate(
                    id_cont=id_cont,
                    nume=str(punct.get("specificIdForUtilityType") or punct.get("number") or id_cont),
                    tip_cont=str(punct.get("utilityType") or "").lower() or None,
                    id_contract=str(punct.get("contractType") or "") or None,
                    adresa=adresa if isinstance(adresa, str) else None,
                    stare=str(punct.get("status") or "active") or None,
                    tip_utilitate=tip_serviciu,
                    tip_serviciu=tip_serviciu,
                    date_brute=punct,
                )
            )
        return rezultate

    def _mapeaza_facturi(self, date_brute: dict[str, Any]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        for factura in date_brute.get("invoices", []) or []:
            id_factura = str(factura.get("invoiceId") or factura.get("series") or factura.get("invoiceSeries") or factura.get("number") or factura.get("invoiceNumber") or "").strip()
            if not id_factura:
                continue
            valoare = _float_sigur(factura.get("amountTotal") or factura.get("value") or factura.get("invoiceValue") or factura.get("total") or factura.get("amount") or factura.get("totalAmount"))
            rest_plata = _float_sigur(factura.get("amountToPay") or factura.get("restToPay") or factura.get("rest") or factura.get("remainingValue") or factura.get("remaining") or factura.get("amountRemaining"))
            tip_serviciu = _normalizeaza_tip_serviciu(
                factura.get("utilityType")
                or factura.get("utility")
                or factura.get("serviceType")
                or factura.get("commodity")
                or factura.get("type")
                or ""
            )
            facturi.append(
                FacturaUtilitate(
                    id_factura=id_factura,
                    titlu=str(factura.get("type") or factura.get("title") or f"Factura {id_factura}"),
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=_data_sigura(factura.get("issueDate") or factura.get("issuedAt") or factura.get("date")),
                    data_scadenta=_data_sigura(factura.get("dueDate") or factura.get("dueAt")),
                    stare=_deduce_stare_factura(factura, rest_plata),
                    categorie=_deduce_categorie_factura(factura),
                    id_cont=self._gaseste_id_cont_pentru_factura(date_brute.get("metering_points", []), factura),
                    id_contract=str(factura.get("contractId") or "") or None,
                    tip_utilitate=tip_serviciu,
                    tip_serviciu=tip_serviciu,
                    este_prosumator=_deduce_categorie_factura(factura) == "injectie",
                    date_brute={**factura, "rest_plata": rest_plata},
                )
            )
        facturi.sort(key=lambda x: x.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont_pentru_factura(self, puncte: list[dict[str, Any]], factura: dict[str, Any]) -> str | None:
        numar_punct = str(factura.get("meteringPointNumber") or "").strip()
        cod_specific = str(factura.get("meteringPointCode") or "").strip()
        for punct in puncte or []:
            if numar_punct and str(punct.get("number") or "").strip() == numar_punct:
                return str(punct.get("meteringPointId") or punct.get("_id") or punct.get("id") or "") or None
            if cod_specific and str(punct.get("specificIdForUtilityType") or "").strip() == cod_specific:
                return str(punct.get("meteringPointId") or punct.get("_id") or punct.get("id") or "") or None
        return None

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        balanta = date_brute.get("invoice_balance", {}) or {}
        tipuri_servicii = sorted({_normalizeaza_tip_serviciu(c.tip_serviciu or c.tip_cont or "") for c in conturi if (c.tip_serviciu or c.tip_cont)})
        este_prosumator = bool(_float_sigur(balanta.get("prosumer")) not in (None, 0.0))
        consumuri.extend([
            ConsumUtilitate(cheie="sold_curent", valoare=_float_sigur(balanta.get("total")), unitate="RON"),
            ConsumUtilitate(cheie="sold_prosumator", valoare=_float_sigur(balanta.get("prosumer")), unitate="RON"),
            ConsumUtilitate(cheie="este_prosumator", valoare="da" if este_prosumator else "nu", unitate=None),
            ConsumUtilitate(cheie="tipuri_servicii", valoare=", ".join([t for t in tipuri_servicii if t]), unitate=None),
            ConsumUtilitate(cheie="numar_puncte_consum", valoare=float(len(conturi)), unitate="buc"),
            ConsumUtilitate(cheie="numar_conturi_curent", valoare=float(sum(1 for c in conturi if c.tip_serviciu == "curent")), unitate="buc"),
            ConsumUtilitate(cheie="numar_conturi_gaz", valoare=float(sum(1 for c in conturi if c.tip_serviciu == "gaz")), unitate="buc"),
            ConsumUtilitate(cheie="numar_facturi", valoare=float(len(date_brute.get("invoices", []) or [])), unitate="buc"),
            ConsumUtilitate(cheie="numar_plati", valoare=float(len(date_brute.get("payments", []) or [])), unitate="buc"),
        ])
        return consumuri

    def _construieste_extra(self, date_brute: dict[str, Any], facturi: list[FacturaUtilitate]) -> dict[str, Any]:
        balanta = date_brute.get("invoice_balance", {}) or {}
        return {
            "cont": date_brute.get("account", {}),
            "cont_vizualizat": date_brute.get("viewed_account", {}),
            "sumar": {
                "total_rest_de_plata": _float_sigur(balanta.get("total")),
                "sold_prosumator": _float_sigur(balanta.get("prosumer")),
                "numar_facturi": len(facturi),
                "numar_facturi_neachitate": sum(1 for f in facturi if f.stare in {"neplatita", "scadenta"}),
                "ultima_factura_id": facturi[0].id_factura if facturi else None,
                "ultima_factura_scadenta": facturi[0].data_scadenta.isoformat() if facturi and facturi[0].data_scadenta else None,
                "ultima_factura_valoare": facturi[0].valoare if facturi else None,
            },
            "date_brute": {
                "invoice_balance": balanta,
                "payments_count": len(date_brute.get("payments", []) or []),
                "self_readings_count": len(date_brute.get("self_readings", []) or []),
                "metering_points_count": len(date_brute.get("metering_points", []) or []),
            },
        }



def _logheaza_diagnostic_nova(date_brute: dict[str, Any]) -> None:
    """Scrie in log un diagnostic anonimizat pentru investigarea conturilor Nova multi-locatie."""

    try:
        puncte = date_brute.get("metering_points", []) or []
        facturi = date_brute.get("invoices", []) or []
        balanta = date_brute.get("invoice_balance", {}) or {}

        _LOGGER.warning(
            "Diagnostic Nova: conturi si puncte consum: %s",
            json.dumps(
                {
                    "logged_account": _nova_safe_account_debug(date_brute.get("account", {}) or {}),
                    "viewed_account": _nova_safe_account_debug(date_brute.get("viewed_account", {}) or {}),
                    "metering_points_count": len(puncte),
                    "metering_points": [_nova_safe_metering_point_debug(punct) for punct in puncte[:12]],
                    "raw_counts": {
                        "invoices": len(facturi),
                        "payments": len(date_brute.get("payments", []) or []),
                        "self_readings": len(date_brute.get("self_readings", []) or []),
                        "metering_points_self_readings": len(date_brute.get("metering_points_self_readings", []) or []),
                        "legal_notifications": len(date_brute.get("legal_notifications", []) or []),
                        "incidents": len(date_brute.get("incidents", []) or []),
                    },
                },
                ensure_ascii=False,
                default=str,
            ),
        )

        _LOGGER.warning(
            "Diagnostic Nova: facturi si solduri: %s",
            json.dumps(
                {
                    "balance": _nova_safe_balance_debug(balanta),
                    "invoices_count": len(facturi),
                    "invoices": [_nova_safe_invoice_debug(factura) for factura in facturi[:12]],
                },
                ensure_ascii=False,
                default=str,
            ),
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Diagnostic Nova: nu s-a putut genera diagnosticul anonimizat: %s", err)


def _nova_safe_account_debug(cont: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cont, dict):
        return {}

    return {
        "id": _nova_mask_identifier(
            cont.get("accountId") or cont.get("_id") or cont.get("id"),
            "cont_anonimizat",
        ),
        "account_number": _nova_mask_identifier(
            cont.get("accountNumber") or cont.get("number") or cont.get("clientCode"),
            "numar_cont_anonimizat",
        ),
        "type": _nova_safe_label(cont.get("type") or cont.get("accountType") or cont.get("role")),
        "status": _nova_safe_label(cont.get("status")),
        "keys_present": sorted(str(key) for key in cont.keys())[:40],
    }


def _nova_safe_metering_point_debug(punct: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(punct, dict):
        return {}

    adresa = punct.get("address")
    return {
        "id": _nova_mask_identifier(
            punct.get("meteringPointId") or punct.get("_id") or punct.get("id"),
            "punct_consum_anonimizat",
        ),
        "number": _nova_mask_identifier(punct.get("number"), "numar_punct_anonimizat"),
        "specific_id": _nova_mask_identifier(
            punct.get("specificIdForUtilityType") or punct.get("specificId") or punct.get("code"),
            "cod_punct_anonimizat",
        ),
        "utility_type": _nova_safe_label(
            punct.get("utilityType")
            or punct.get("utility")
            or punct.get("serviceType")
            or punct.get("commodity")
            or punct.get("type")
        ),
        "normalized_utility_type": _normalizeaza_tip_serviciu(
            punct.get("utilityType")
            or punct.get("utility")
            or punct.get("serviceType")
            or punct.get("commodity")
            or punct.get("type")
            or ""
        ),
        "contract_type": _nova_safe_label(punct.get("contractType")),
        "contract_id": _nova_mask_identifier(punct.get("contractId"), "contract_anonimizat"),
        "status": _nova_safe_label(punct.get("status")),
        "has_address": bool(adresa),
        "address": _nova_mask_identifier(adresa, "adresa_anonimizata") if adresa else None,
        "is_prosumer_flag": _nova_boolish(
            punct.get("isProsumer") or punct.get("prosumer") or punct.get("hasInjection") or punct.get("injection")
        ),
        "keys_present": sorted(str(key) for key in punct.keys())[:50],
    }


def _nova_safe_invoice_debug(factura: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(factura, dict):
        return {}

    return {
        "id": _nova_mask_identifier(
            factura.get("invoiceId")
            or factura.get("id")
            or factura.get("_id")
            or factura.get("number")
            or factura.get("invoiceNumber"),
            "factura_anonimizata",
        ),
        "invoice_number": _nova_mask_identifier(
            factura.get("invoiceNumber") or factura.get("number") or factura.get("series") or factura.get("invoiceSeries"),
            "numar_factura_anonimizat",
        ),
        "issue_date": _nova_safe_label(factura.get("issueDate") or factura.get("issuedAt") or factura.get("date")),
        "due_date": _nova_safe_label(factura.get("dueDate") or factura.get("dueAt")),
        "amount": _float_sigur(
            factura.get("amountTotal")
            or factura.get("value")
            or factura.get("invoiceValue")
            or factura.get("total")
            or factura.get("amount")
            or factura.get("totalAmount")
        ),
        "remaining": _float_sigur(
            factura.get("amountToPay")
            or factura.get("restToPay")
            or factura.get("rest")
            or factura.get("remainingValue")
            or factura.get("remaining")
            or factura.get("amountRemaining")
        ),
        "status": _nova_safe_label(factura.get("status") or factura.get("paymentStatus")),
        "type": _nova_safe_label(factura.get("type")),
        "title": _nova_safe_label(factura.get("title")),
        "category": _nova_safe_label(factura.get("category") or factura.get("description") or factura.get("invoiceType")),
        "utility_type": _nova_safe_label(
            factura.get("utilityType")
            or factura.get("utility")
            or factura.get("serviceType")
            or factura.get("commodity")
        ),
        "normalized_utility_type": _normalizeaza_tip_serviciu(
            factura.get("utilityType")
            or factura.get("utility")
            or factura.get("serviceType")
            or factura.get("commodity")
            or factura.get("type")
            or ""
        ),
        "metering_point_number": _nova_mask_identifier(
            factura.get("meteringPointNumber"),
            "numar_punct_anonimizat",
        ),
        "metering_point_code": _nova_mask_identifier(
            factura.get("meteringPointCode"),
            "cod_punct_anonimizat",
        ),
        "contract_id": _nova_mask_identifier(factura.get("contractId"), "contract_anonimizat"),
        "keys_present": sorted(str(key) for key in factura.keys())[:60],
    }


def _nova_safe_balance_debug(balanta: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(balanta, dict):
        return {}

    return {
        "total": _float_sigur(balanta.get("total")),
        "prosumer": _float_sigur(balanta.get("prosumer")),
        "keys_present": sorted(str(key) for key in balanta.keys())[:40],
    }


def _nova_mask_identifier(valoare: Any, prefix: str) -> str | None:
    if valoare in (None, "", [], {}):
        return None
    text = json.dumps(valoare, ensure_ascii=False, sort_keys=True, default=str) if isinstance(valoare, (dict, list)) else str(valoare)
    digest = hashlib.sha256(f"utilitati_romania_nova::{text}".encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def _nova_safe_label(valoare: Any, *, limita: int = 80) -> str | None:
    if valoare in (None, ""):
        return None
    text = str(valoare).strip()
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) > limita:
        text = f"{text[:limita]}…"
    return text


def _nova_boolish(valoare: Any) -> bool | None:
    if valoare in (None, ""):
        return None
    if isinstance(valoare, bool):
        return valoare
    if isinstance(valoare, (int, float)):
        return bool(valoare)
    text = str(valoare).strip().lower()
    if text in {"true", "1", "da", "yes", "y"}:
        return True
    if text in {"false", "0", "nu", "no", "n"}:
        return False
    return None

def _normalizeaza_tip_serviciu(valoare: Any) -> str | None:
    if valoare in (None, ""):
        return None
    text = str(valoare).strip().lower()
    if not text:
        return None

    if any(cuvant in text for cuvant in ("gaz", "gaze", "natural gas", "gas")):
        return "gaz"
    if any(cuvant in text for cuvant in ("energie electric", "electricitate", "electric", "curent", "power", "energy", "electricity")):
        return "curent"
    return text


def _float_sigur(valoare: Any) -> float | None:
    if valoare in (None, "", "null"):
        return None
    try:
        return float(valoare)
    except (TypeError, ValueError):
        return None


def _data_sigura(valoare: Any) -> date | None:
    if not valoare:
        return None
    text = str(valoare)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _deduce_stare_factura(factura: dict[str, Any], rest_plata: float | None) -> str:
    status_brut = str(factura.get("status") or factura.get("paymentStatus") or "").lower()
    if "paid" in status_brut or "plat" in status_brut:
        return "platita"
    if rest_plata and rest_plata > 0:
        data_scadenta = _data_sigura(factura.get("dueDate") or factura.get("dueAt"))
        if data_scadenta and data_scadenta < date.today():
            return "scadenta"
        return "neplatita"
    return status_brut or "necunoscuta"


def _deduce_categorie_factura(factura: dict[str, Any]) -> str:
    text = " ".join(str(factura.get(camp) or "") for camp in ["type", "title", "category", "description", "invoiceType"]).lower()
    valoare = _float_sigur(
        factura.get("amountTotal")
        or factura.get("value")
        or factura.get("invoiceValue")
        or factura.get("total")
        or factura.get("amount")
        or factura.get("totalAmount")
    )
    if "inject" in text or "prosum" in text or "compens" in text or "sold" in text:
        return "injectie"
    if valoare is not None and valoare < 0:
        return "injectie"
    return "consum"
