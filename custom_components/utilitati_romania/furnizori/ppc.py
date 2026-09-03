from __future__ import annotations

import base64
from datetime import date, datetime
import hashlib
import logging
import re
import time
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_API = "https://myppc.ppcenergy.ro/me/rfc"
APPLICATION_UUID = "19782da0-4f66-4c28-b40e-60d8ab39f243"
SOURCE_CHANNEL = "PPC_APP_ROM"
TOUCHPOINT = "app"
USER_AGENT = "Apache-HttpClient/4.5.5 (Java/21.0.0)"
SOAP_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"
URN_NAMESPACE = "urn:cros:document:types:myenel"
TOKEN_RENEW_MARGIN = 60


class EroareApiPpc(Exception):
    pass


class EroareAutentificarePpc(EroareApiPpc):
    pass


class EroareConectarePpc(EroareApiPpc):
    pass


class EroareRaspunsPpc(EroareApiPpc):
    pass


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None




def _interval_citire(mesaj: Any) -> str | None:
    text = _text(mesaj)
    if not text:
        return None
    match = re.search(
        r"(\d{2}\.\d{2}\.\d{4})\s*[-–—]\s*(\d{2}\.\d{2}\.\d{4})",
        text,
    )
    if not match:
        return None
    return f"{match.group(1)} - {match.group(2)}"

def _local_name(tag: str) -> str:
    return tag.split("}")[-1]


def _element_to_value(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        nil = element.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil")
        if _text(nil).lower() == "true":
            return None
        return _text(element.text)

    result: dict[str, Any] = {}
    for child in children:
        key = _local_name(child.tag)
        value = _element_to_value(child)
        if key in result:
            current = result[key]
            if not isinstance(current, list):
                result[key] = [current]
            result[key].append(value)
        else:
            result[key] = value
    return result


def _parse_soap(raw: str, metoda: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as err:
        raise EroareRaspunsPpc(f"Răspuns XML invalid pentru {metoda}") from err

    body = next((node for node in root.iter() if _local_name(node.tag) == "Body"), None)
    if body is None:
        raise EroareRaspunsPpc(f"Răspuns SOAP fără Body pentru {metoda}")

    response = next(iter(body), None)
    if response is None:
        raise EroareRaspunsPpc(f"Răspuns SOAP gol pentru {metoda}")

    if _local_name(response.tag) == "Fault":
        mesaj = ""
        for node in response.iter():
            if _local_name(node.tag) in {"faultstring", "Text", "message"} and _text(node.text):
                mesaj = _text(node.text)
                break
        raise EroareRaspunsPpc(mesaj or f"PPC a returnat SOAP Fault pentru {metoda}")

    value = _element_to_value(response)
    return value if isinstance(value, dict) else {"value": value}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("Rows")
    if not isinstance(rows, dict):
        return []
    row = rows.get("Row")
    if isinstance(row, list):
        return [item for item in row if isinstance(item, dict)]
    if isinstance(row, dict):
        return [row]
    return []


def _tip_serviciu(value: Any) -> str:
    text = _text(value).upper()
    if text in {"GAZ", "GAS"}:
        return "gaz"
    if text in {"EE", "ELEC", "ELECTRICITY", "ENERGIE ELECTRICA", "ENERGIE ELECTRICĂ"}:
        return "curent"
    return text.lower() or "energie"


def _tip_utilitate(tip: str) -> str:
    if tip == "gaz":
        return "gaz"
    if tip == "curent":
        return "energie electrică"
    return "energie"


def _token_fp(token: str | None) -> str:
    if not token:
        return "none"
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _xml_fields(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    parts: list[str] = []
    for key, value in data.items():
        if value is None:
            continue
        parts.append(f"<{key}>{escape(str(value))}</{key}>")
    return "".join(parts)


def _soap_envelope(metoda: str, data: dict[str, Any] | None, token: str | None) -> bytes:
    auth = ""
    if token:
        auth = f"<urn:userAuthentication>{escape(token)}</urn:userAuthentication>"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_NAMESPACE}" xmlns:urn="{URN_NAMESPACE}">'
        "<SOAP-ENV:Header>"
        f"<urn:applicationUuid>{APPLICATION_UUID}</urn:applicationUuid>"
        f"{auth}"
        "</SOAP-ENV:Header>"
        "<SOAP-ENV:Body>"
        f"<urn:{metoda}Request>{_xml_fields(data)}</urn:{metoda}Request>"
        "</SOAP-ENV:Body>"
        "</SOAP-ENV:Envelope>"
    )
    return xml.encode("utf-8")


class ClientApiPpc:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._sid = str(uuid4())
        self._identitate: dict[str, Any] = {}
        self._access_token: str | None = None
        self._expires_at = 0.0

    @property
    def enel_id(self) -> str:
        return _text(self._identitate.get("enelid"))

    def _headers(self, metoda: str) -> dict[str, str]:
        headers = {
            "Accept": "text/xml",
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": f'"{metoda}"',
            "SOURCE_CHANNEL": SOURCE_CHANNEL,
            "TOUCHPOINT": TOUCHPOINT,
            "SID": self._sid,
            "TID": str(uuid4()),
            "CLIENT_IP": "127.0.0.1",
            "User-Agent": USER_AGENT,
        }
        if self.enel_id:
            headers["ENEL_ID"] = self.enel_id
        return headers

    def _token_valid(self) -> bool:
        return bool(self._access_token) and time.monotonic() < (self._expires_at - TOKEN_RENEW_MARGIN)

    async def _apel(
        self,
        metoda: str,
        data: dict[str, Any] | None = None,
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        body = _soap_envelope(metoda, data, token)
        headers = self._headers(metoda)
        start = time.monotonic()

        _LOGGER.debug(
            "[PPC DIAG] request method=%s token=%s enel_id=%s body_bytes=%s",
            metoda,
            _token_fp(token),
            bool(self.enel_id),
            len(body),
        )

        try:
            async with self._sesiune.post(
                URL_API,
                headers=headers,
                data=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                raw = await raspuns.text(errors="replace")
                durata = round(time.monotonic() - start, 3)
                _LOGGER.debug(
                    "[PPC DIAG] response method=%s http=%s elapsed=%s bytes=%s",
                    metoda,
                    raspuns.status,
                    durata,
                    len(raw),
                )
                if raspuns.status in (401, 403):
                    raise EroareAutentificarePpc(f"PPC a respins sesiunea pentru {metoda} (HTTP {raspuns.status})")
                if raspuns.status >= 400:
                    raise EroareConectarePpc(f"PPC a returnat HTTP {raspuns.status} pentru {metoda}")
        except EroareApiPpc:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectarePpc(f"Eroare de conectare PPC la {metoda}: {err}") from err

        payload = _parse_soap(raw, metoda)
        _LOGGER.debug("[PPC DIAG] parsed method=%s rows=%s", metoda, len(_rows(payload)))
        return payload

    async def async_login(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._token_valid():
            return self._identitate

        credentiale = base64.b64encode(f"{self._utilizator}:{self._parola}".encode()).decode()
        payload = await self._apel("GetToken", {"userAuthentication": credentiale}, token=None)
        randuri = _rows(payload)
        if not randuri:
            raise EroareAutentificarePpc("PPC nu a returnat date de sesiune")

        identitate = randuri[0]
        access_token = _text(identitate.get("access_token"))
        enel_id = _text(identitate.get("enelid"))
        if not access_token or not enel_id:
            raise EroareAutentificarePpc("PPC nu a returnat o sesiune validă")

        try:
            expires_in = max(int(float(_text(identitate.get("expires_in")) or "3600")), 60)
        except (TypeError, ValueError):
            expires_in = 3600

        self._identitate = identitate
        self._access_token = access_token
        self._expires_at = time.monotonic() + expires_in
        _LOGGER.debug(
            "[PPC DIAG] login_ok token_type=%s expires_in=%s token_fp=%s enel_id=%s",
            _text(identitate.get("token_type")),
            expires_in,
            _token_fp(access_token),
            enel_id[:6] + "…",
        )
        return identitate

    async def async_apel(self, metoda: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.async_login()
        try:
            return await self._apel(metoda, data, token=self._access_token)
        except EroareAutentificarePpc:
            _LOGGER.debug("[PPC DIAG] session_reauth method=%s", metoda)
            await self.async_login(force=True)
            return await self._apel(metoda, data, token=self._access_token)


class ClientFurnizorPpc(ClientFurnizor):
    cheie_furnizor = "ppc"
    nume_prietenos = "PPC Energie"

    def __init__(self, *, sesiune: aiohttp.ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiPpc(sesiune, utilizator, parola)

    @staticmethod
    def _converteste_eroare(err: Exception) -> Exception:
        if isinstance(err, EroareAutentificarePpc):
            return EroareAutentificare(str(err))
        if isinstance(err, EroareConectarePpc):
            return EroareConectare(str(err))
        return EroareParsare(str(err))

    async def async_testeaza_conexiunea(self) -> str:
        try:
            identitate = await self.api.async_login(force=True)
            locuri = await self.api.async_apel("ConsumptionLocationsAssociatedV2")
        except EroareApiPpc as err:
            raise self._converteste_eroare(err) from err

        unic = _text(identitate.get("enelid") or identitate.get("cros_user"))
        if unic:
            return unic.lower()
        randuri = _rows(locuri)
        if randuri:
            cod = _text(randuri[0].get("clientCode") or randuri[0].get("paymentCode"))
            if cod:
                return cod.lower()
        return self.utilizator.lower().strip()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            await self.api.async_login()
            locuri_payload = await self.api.async_apel("ConsumptionLocationsAssociatedV2")
            profil_payload = await self.api.async_apel(
                "GetProfileAccountV1",
                {"country": "RO", "enelId": self.api.enel_id},
            )
            restante_payload = await self.api.async_apel("OnlinePaymentsListV2")
        except EroareApiPpc as err:
            raise self._converteste_eroare(err) from err

        profil_rows = _rows(profil_payload)
        profil = profil_rows[0] if profil_rows else {}
        nume_client = " ".join(
            filter(None, [_text(profil.get("firstName")), _text(profil.get("lastName"))])
        ).strip()

        restante_dupa_document: dict[str, dict[str, Any]] = {}
        restante_dupa_payment: dict[str, list[dict[str, Any]]] = {}
        for client in _rows(restante_payload):
            documente = client.get("documentList")
            if isinstance(documente, dict):
                documente = documente.get("Row", [])
            if isinstance(documente, dict):
                documente = [documente]
            for document in documente if isinstance(documente, list) else []:
                if not isinstance(document, dict):
                    continue
                id_doc = _text(document.get("documentId"))
                if id_doc:
                    restante_dupa_document[id_doc] = document
                cod_plata = _text(document.get("paymentCode"))
                if cod_plata:
                    restante_dupa_payment.setdefault(cod_plata, []).append(document)

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []

        for grup in _rows(locuri_payload):
            cod_client = _text(grup.get("clientCode"))
            cod_plata = _text(grup.get("paymentCode"))
            tip = _tip_serviciu(grup.get("supplyType"))
            tip_util = _tip_utilitate(tip)
            locuri = grup.get("consumptionLocations")
            if isinstance(locuri, dict):
                locuri = locuri.get("Row", [])
            if isinstance(locuri, dict):
                locuri = [locuri]

            for loc in locuri if isinstance(locuri, list) else []:
                if not isinstance(loc, dict):
                    continue
                id_loc = _text(
                    loc.get("idConsumptionLocation")
                    or loc.get("noConsumptionLocation")
                    or loc.get("pod")
                )
                if not id_loc:
                    continue

                adresa = _text(loc.get("address") or grup.get("addressPaymentCode"))
                alias = _text(loc.get("aliasConsumptionPlace") or grup.get("aliasPaymentCode"))
                pod = _text(loc.get("pod"))

                try:
                    facturi_payload = await self.api.async_apel(
                        "InvoicesListV4", {"idConsumptionLocation": id_loc}
                    )
                    index_payload = await self.api.async_apel(
                        "MeterReadingsV3", {"idConsumptionLocation": id_loc}
                    )
                    istoric_payload = await self.api.async_apel(
                        "IndexHistoryV2", {"idConsumptionLocation": id_loc}
                    )
                    comparatie_payload = await self.api.async_apel(
                        "CompareConsumptionV2", {"idConsumptionLocation": id_loc}
                    )
                    plati_payload = await self.api.async_apel(
                        "PaymentsList", {"idConsumptionLocation": id_loc}
                    )
                except EroareApiPpc as err:
                    raise self._converteste_eroare(err) from err

                facturi_raw = _rows(facturi_payload)
                citiri_raw = _rows(index_payload)
                istoric_raw = _rows(istoric_payload)
                comparatie_raw = _rows(comparatie_payload)
                plati_raw = _rows(plati_payload)
                restante_loc = restante_dupa_payment.get(cod_plata, [])
                sold = round(sum(_float(x.get("balance")) or 0.0 for x in restante_loc), 2)

                cont = ContUtilitate(
                    id_cont=id_loc,
                    nume=alias or adresa or f"Loc consum {id_loc}",
                    tip_cont="loc_consum",
                    id_contract=cod_plata or None,
                    adresa=adresa or None,
                    stare="activ",
                    tip_utilitate=tip_util,
                    tip_serviciu=tip,
                    date_brute={
                        "client_code": cod_client,
                        "payment_code": cod_plata,
                        "pod": pod,
                        "nlc": _text(loc.get("noConsumptionLocation")),
                        "distribuitor": _text(loc.get("distribuitor")),
                        "eneltel": _text(loc.get("eneltel")),
                        "profil": profil,
                        "loc": loc,
                        "grup": grup,
                        "facturi": facturi_raw,
                        "plati": plati_raw,
                        "citiri": citiri_raw,
                        "istoric_index": istoric_raw,
                        "istoric_consum": comparatie_raw,
                        "sold": sold,
                        "mesaj_citire": _text(index_payload.get("textReturn")),
                    },
                )
                conturi.append(cont)

                for factura_raw in facturi_raw:
                    id_factura_api = _text(factura_raw.get("idInvoiceBill"))
                    numar_factura = _text(factura_raw.get("bill")) or id_factura_api
                    restanta = restante_dupa_document.get(id_factura_api)
                    stare = _text(restanta.get("status")) if restanta else "plătită"
                    if stare.lower() in {"neachitata", "neachitată", "unpaid"}:
                        stare = "neplătită"
                    facturi.append(
                        FacturaUtilitate(
                            id_factura=numar_factura or id_factura_api,
                            titlu=f"Factură {numar_factura or id_factura_api}",
                            valoare=_float(factura_raw.get("amount") or factura_raw.get("currentAmount")),
                            moneda="RON",
                            data_emitere=_parse_date(factura_raw.get("date")),
                            data_scadenta=_parse_date(factura_raw.get("maturity")),
                            stare=stare,
                            categorie="factura",
                            id_cont=id_loc,
                            id_contract=cod_plata or None,
                            tip_utilitate=tip_util,
                            tip_serviciu=tip,
                            date_brute={**factura_raw, "restanta": restanta},
                        )
                    )

                consumuri.extend(
                    [
                        ConsumUtilitate("sold_curent", sold, "RON", id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("de_plata", max(sold, 0.0), "RON", id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("factura_restanta", "da" if sold > 0 else "nu", None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("numar_facturi", len(facturi_raw), None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("numar_facturi_neachitate", len(restante_loc), None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("numar_plati", len(plati_raw), None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("cod_client", cod_client, None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("cod_plata", cod_plata, None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("pod", pod, None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ConsumUtilitate("distribuitor", _text(loc.get("distribuitor")), None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                    ]
                )

                if facturi_raw:
                    ultima = max(facturi_raw, key=lambda x: _parse_date(x.get("date")) or date.min)
                    emitere = _parse_date(ultima.get("date"))
                    scadenta = _parse_date(ultima.get("maturity"))
                    consumuri.extend(
                        [
                            ConsumUtilitate("valoare_ultima_factura", _float(ultima.get("amount") or ultima.get("currentAmount")), "RON", id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                            ConsumUtilitate("id_ultima_factura", _text(ultima.get("bill") or ultima.get("idInvoiceBill")), None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                            ConsumUtilitate("data_ultima_factura", emitere.isoformat() if emitere else None, None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                            ConsumUtilitate("urmatoarea_scadenta", scadenta.isoformat() if scadenta else None, None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ]
                    )

                if citiri_raw:
                    citire = citiri_raw[0]
                    unitate = _text(citire.get("measuredSize") or citire.get("codeDial")) or ("M3" if tip == "gaz" else "kWh")
                    unitate = "m³" if unitate.upper() == "M3" else unitate
                    data_citire = _parse_date(citire.get("oldDate"))
                    consumuri.extend(
                        [
                            ConsumUtilitate("index_contor", _float(citire.get("oldIndex")), unitate, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                            ConsumUtilitate("serie_contor", _text(citire.get("series")), None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                            ConsumUtilitate("data_ultimei_citiri", data_citire.isoformat() if data_citire else None, None, id_cont=id_loc, tip_utilitate=tip_util, tip_serviciu=tip),
                        ]
                    )

                citire_permisa = _text(index_payload.get("isSelfReadingAllowed")).lower() == "true"
                consumuri.append(
                    ConsumUtilitate(
                        "citire_permisa",
                        "da" if citire_permisa else "nu",
                        None,
                        id_cont=id_loc,
                        tip_utilitate=tip_util,
                        tip_serviciu=tip,
                    )
                )
                mesaj_citire = _text(index_payload.get("textReturn"))
                perioada_citire = _interval_citire(mesaj_citire)
                if perioada_citire:
                    consumuri.append(
                        ConsumUtilitate(
                            "perioada_citire",
                            perioada_citire,
                            None,
                            id_cont=id_loc,
                            tip_utilitate=tip_util,
                            tip_serviciu=tip,
                        )
                    )

                if plati_raw:
                    ultima_plata = max(
                        plati_raw,
                        key=lambda x: _parse_date(x.get("date")) or date.min,
                    )
                    data_plata = _parse_date(ultima_plata.get("date"))
                    consumuri.extend(
                        [
                            ConsumUtilitate(
                                "valoare_ultima_plata",
                                _float(ultima_plata.get("total")),
                                "RON",
                                id_cont=id_loc,
                                tip_utilitate=tip_util,
                                tip_serviciu=tip,
                            ),
                            ConsumUtilitate(
                                "data_ultima_plata",
                                data_plata.isoformat() if data_plata else None,
                                None,
                                id_cont=id_loc,
                                tip_utilitate=tip_util,
                                tip_serviciu=tip,
                            ),
                        ]
                    )

                if comparatie_raw:
                    def ordine(item: dict[str, Any]) -> tuple[int, int]:
                        try:
                            return int(item.get("year") or 0), int(item.get("month") or 0)
                        except (TypeError, ValueError):
                            return 0, 0

                    comparatie_sortata = sorted(comparatie_raw, key=ordine, reverse=True)
                    ultima_luna = comparatie_sortata[0]
                    unitate = _text(ultima_luna.get("um")) or ("M3" if tip == "gaz" else "kWh")
                    unitate = "m³" if unitate.upper() == "M3" else unitate
                    consumuri.extend(
                        [
                            ConsumUtilitate(
                                "consum_lunar",
                                _float(ultima_luna.get("quantity")),
                                unitate,
                                perioada=f"{ultima_luna.get('month')}/{ultima_luna.get('year')}",
                                id_cont=id_loc,
                                tip_utilitate=tip_util,
                                tip_serviciu=tip,
                                date_brute={"istoric": comparatie_sortata[:24]},
                            ),
                            ConsumUtilitate(
                                "consum_ultimele_12_luni",
                                round(sum(_float(x.get("quantity")) or 0.0 for x in comparatie_sortata[:12]), 3),
                                unitate,
                                id_cont=id_loc,
                                tip_utilitate=tip_util,
                                tip_serviciu=tip,
                                date_brute={"istoric": comparatie_sortata[:12]},
                            ),
                        ]
                    )

        if not conturi:
            raise EroareParsare("PPC nu a returnat niciun loc de consum asociat contului")

        _LOGGER.debug(
            "[PPC DIAG] snapshot_ok accounts=%s invoices=%s consumptions=%s",
            len(conturi),
            len(facturi),
            len(consumuri),
        )
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={"profil": profil, "nume_client": nume_client},
        )
