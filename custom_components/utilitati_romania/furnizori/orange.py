from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import base64
import logging
from typing import Any
from uuid import NAMESPACE_DNS, uuid4, uuid5

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://www.orange.ro"
ENDPOINT_TOKEN = "/accounts/token"
ENDPOINT_USER_INFO = "/accounts/v3/userInfo"
ENDPOINT_SUBSCRIBERS = "/myaccount/api/v5/subscribers"
ENDPOINT_INVOICE_INFO = "/myaccount/api/v5/invoice/{profile_id}/{msisdn}/invoiceInfo"
ENDPOINT_INVOICE_HISTORY = "/myaccount/api/v5/invoice/history"

CLIENT_ID_MOBIL = "07f501ee-3d7f-4eed-848c-658be314219c"
CLIENT_SECRET_MOBIL = "cDlicFa9aaRETjgU9tDk6azeyUaBMAheQTfS"
SCOP_ORANGE = "oauth.userinfo.extended myaccountb2c.access asyncchat.read eshopb2c.place_order eshopb2c.read_offers openid"
USER_AGENT_ORANGE = "myorange_android okhttp/4.12.0"
VERSIUNE_APLICATIE_ORANGE = "10.10.11"


class EroareApiOrange(Exception):
    pass


class EroareAutentificareOrange(EroareApiOrange):
    pass


class EroareConectareOrange(EroareApiOrange):
    pass


class EroareRaspunsOrange(EroareApiOrange):
    pass


@dataclass(slots=True)
class DateSesiuneOrange:
    access_token: str
    refresh_token: str | None
    expira_la: int


class ClientApiOrange:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expira_la: int | None = None
        self._device_id = str(uuid5(NAMESPACE_DNS, f"utilitati-romania-orange-{utilizator.lower().strip()}"))
        self._profile_session_id = uuid4().hex.upper()

    def _url(self, endpoint: str) -> str:
        return f"{URL_BAZA}{endpoint}"

    def _basic_auth_header(self) -> str:
        raw = f"{CLIENT_ID_MOBIL}:{CLIENT_SECRET_MOBIL}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_valid(self) -> bool:
        if not self._access_token or not self._token_expira_la:
            return False
        acum = int(datetime.now(tz=UTC).timestamp())
        return acum < (self._token_expira_la - 90)

    def _headers_standard(self, *, autentificat: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT_ORANGE,
            "X-App-Version": VERSIUNE_APLICATIE_ORANGE,
            "X-Profile-Session-Id": self._profile_session_id,
            "X-Tracking-Id": str(uuid4()),
        }
        if autentificat:
            if not self._access_token:
                raise EroareAutentificareOrange("Lipsește tokenul de autentificare Orange")
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _request_json(
        self,
        metoda: str,
        endpoint: str,
        *,
        autentificat: bool = True,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        if autentificat and not self._token_valid():
            await self.async_login()

        headers = self._headers_standard(autentificat=autentificat)
        if json_data is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with self._sesiune.request(
                metoda,
                self._url(endpoint),
                headers=headers,
                json=json_data,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    if autentificat and retry_auth:
                        self._access_token = None
                        await self.async_login()
                        return await self._request_json(
                            metoda,
                            endpoint,
                            autentificat=autentificat,
                            json_data=json_data,
                            params=params,
                            retry_auth=False,
                        )
                    raise EroareAutentificareOrange(f"Autentificare Orange eșuată pentru {endpoint}: HTTP {raspuns.status}")
                if raspuns.status >= 400:
                    raise EroareRaspunsOrange(f"Orange API a returnat HTTP {raspuns.status} pentru {endpoint}: {text[:500]}")
                try:
                    data = await raspuns.json()
                except aiohttp.ContentTypeError as err:
                    raise EroareRaspunsOrange(f"Răspuns Orange invalid pentru {endpoint}: {text[:500]}") from err
        except EroareApiOrange:
            raise
        except TimeoutError as err:
            raise EroareConectareOrange(f"Timeout la Orange API pentru {endpoint}") from err
        except aiohttp.ClientError as err:
            raise EroareConectareOrange(f"Eroare de conectare la Orange API pentru {endpoint}: {err}") from err

        if not isinstance(data, dict):
            raise EroareRaspunsOrange(f"Tip de răspuns Orange neașteptat pentru {endpoint}: {type(data)}")
        return data

    async def _request_token(self, payload: dict[str, Any]) -> DateSesiuneOrange:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT_ORANGE,
            "X-App-Version": VERSIUNE_APLICATIE_ORANGE,
            "X-Device-Id": self._device_id,
            "X-Device-Model": "Home Assistant",
            "X-Device-Os": "Android: 25 (7.1.2)",
        }
        try:
            async with self._sesiune.post(
                self._url(ENDPOINT_TOKEN),
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (400, 401, 403):
                    raise EroareAutentificareOrange(f"Autentificare Orange eșuată: HTTP {raspuns.status}")
                if raspuns.status >= 400:
                    raise EroareRaspunsOrange(f"Orange token a returnat HTTP {raspuns.status}: {text[:500]}")
                try:
                    data = await raspuns.json()
                except aiohttp.ContentTypeError as err:
                    raise EroareRaspunsOrange(f"Răspuns token Orange invalid: {text[:500]}") from err
        except EroareApiOrange:
            raise
        except TimeoutError as err:
            raise EroareConectareOrange("Timeout la autentificarea Orange") from err
        except aiohttp.ClientError as err:
            raise EroareConectareOrange(f"Eroare de conectare la autentificarea Orange: {err}") from err

        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise EroareAutentificareOrange("Autentificare Orange eșuată: lipsește access_token")

        refresh_token = str(data.get("refresh_token") or "").strip() or None
        expires_in = _int_sigur(data.get("expires_in"), 3599)
        expira_la = int(datetime.now(tz=UTC).timestamp()) + max(expires_in, 300)

        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expira_la = expira_la
        return DateSesiuneOrange(access_token=access_token, refresh_token=refresh_token, expira_la=expira_la)

    async def async_login(self) -> DateSesiuneOrange:
        if self._refresh_token:
            try:
                return await self._request_token(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "scope": SCOP_ORANGE,
                    }
                )
            except EroareAutentificareOrange:
                self._refresh_token = None

        return await self._request_token(
            {
                "access_type": "offline",
                "grant_type": "password",
                "username": self._utilizator,
                "password": self._parola,
                "scope": SCOP_ORANGE,
            }
        )

    async def async_user_info(self) -> dict[str, Any]:
        return await self._request_json("GET", ENDPOINT_USER_INFO)

    async def async_subscribers(self) -> list[dict[str, Any]]:
        data = await self._request_json("GET", ENDPOINT_SUBSCRIBERS)
        lista = data.get("msisdnList")
        return [item for item in lista if isinstance(item, dict)] if isinstance(lista, list) else []

    async def async_invoice_info(self, profile_id: str, msisdn: str) -> dict[str, Any]:
        endpoint = ENDPOINT_INVOICE_INFO.format(profile_id=profile_id, msisdn=msisdn)
        return await self._request_json("GET", endpoint)

    async def async_invoice_history(self, customer_number: str, subscriber_id: str) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            ENDPOINT_INVOICE_HISTORY,
            params={"customerNumber": customer_number, "subscriberId": subscriber_id},
        )

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        user_info = await self.async_user_info()
        subscribers = await self.async_subscribers()
        return {"user_info": user_info, "subscribers": subscribers}

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._token_valid():
            await self.async_login()

        user_info = await self.async_user_info()
        subscribers = await self.async_subscribers()
        invoice_infos: dict[str, dict[str, Any]] = {}
        history_by_customer: dict[str, dict[str, Any]] = {}

        for subscriber in subscribers:
            if not _subscriber_facturabil(subscriber):
                continue
            msisdn = _text(subscriber.get("msisdn"))
            profile_id = _text(subscriber.get("profileId"))
            if not msisdn or not profile_id:
                continue
            try:
                invoice_infos[msisdn] = await self.async_invoice_info(profile_id, msisdn)
            except EroareApiOrange as err:
                _LOGGER.warning("Nu am putut citi factura Orange pentru %s: %s", msisdn, err)

        for subscriber in subscribers:
            if not _subscriber_facturabil(subscriber):
                continue
            subscriber_id = _text(subscriber.get("subscriberId"))
            msisdn = _text(subscriber.get("msisdn"))
            customer_number = _extrage_customer_number(invoice_infos.get(msisdn))
            if not customer_number or not subscriber_id:
                continue
            if customer_number in history_by_customer:
                continue
            try:
                history_by_customer[customer_number] = await self.async_invoice_history(customer_number, subscriber_id)
            except EroareApiOrange as err:
                _LOGGER.debug("Nu am putut citi istoricul facturilor Orange pentru %s: %s", customer_number, err)

        return {
            "user_info": user_info,
            "subscribers": subscribers,
            "invoice_infos": invoice_infos,
            "invoice_history": history_by_customer,
        }


class ClientFurnizorOrange(ClientFurnizor):
    cheie_furnizor = "orange"
    nume_prietenos = "Orange"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiOrange(sesiune=sesiune, utilizator=utilizator, parola=parola)

    async def async_testeaza_conexiunea(self) -> str:
        try:
            rezultat = await self.api.async_validate_credentials()
        except EroareAutentificareOrange as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareOrange as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsOrange as err:
            raise EroareParsare(str(err)) from err

        user_info = rezultat.get("user_info") or {}
        identificator = _text(user_info.get("sub") or user_info.get("email") or user_info.get("username") or self.utilizator)
        return identificator or self.utilizator

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            date_brute = await self.api.async_get_all_data()
        except EroareAutentificareOrange as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareOrange as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsOrange as err:
            raise EroareParsare(str(err)) from err

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute, conturi)
        consumuri = self._mapeaza_consumuri(date_brute, conturi, facturi)
        extra = self._construieste_extra(date_brute, conturi, facturi)

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra=extra,
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        conturi: list[ContUtilitate] = []
        for subscriber in date_brute.get("subscribers", []) or []:
            msisdn = _text(subscriber.get("msisdn"))
            if not msisdn:
                continue
            nume_abonament = _text(subscriber.get("subscriptionName"))
            tip_afisat = _text(subscriber.get("subscriberTypeDisplayName"))
            nume = f"{msisdn}"
            if nume_abonament:
                nume = f"{msisdn} - {nume_abonament}"
            conturi.append(
                ContUtilitate(
                    id_cont=msisdn,
                    nume=nume,
                    tip_cont=tip_afisat or _text(subscriber.get("subscriberType")) or None,
                    id_contract=_text(subscriber.get("profileId")) or None,
                    adresa=_text(subscriber.get("address")) or None,
                    stare=_text(subscriber.get("status")) or None,
                    tip_utilitate="telecom",
                    tip_serviciu="abonament" if _subscriber_facturabil(subscriber) else "prepay",
                    date_brute=subscriber,
                )
            )
        return conturi

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        conturi_dupa_id = {cont.id_cont: cont for cont in conturi}
        invoice_infos = date_brute.get("invoice_infos", {}) or {}
        istoric_pe_customer = _istoric_facturi_dupa_msisdn(date_brute.get("invoice_history", {}) or {})

        for msisdn, raspuns in invoice_infos.items():
            if not isinstance(raspuns, dict):
                continue
            data = raspuns.get("data") if isinstance(raspuns.get("data"), dict) else {}
            invoice_info = data.get("invoiceInfo") if isinstance(data.get("invoiceInfo"), dict) else {}
            last_bill = data.get("lastBill") if isinstance(data.get("lastBill"), dict) else {}
            balance = data.get("balanceData") if isinstance(data.get("balanceData"), dict) else {}

            reference = _text(last_bill.get("reference"))
            istoric_curent = _alege_factura_istoric(istoric_pe_customer.get(msisdn, []), reference)
            if not reference and istoric_curent:
                reference = _text(istoric_curent.get("reference"))
            if not reference:
                reference = f"orange_{msisdn}_ultima"

            valoare = _float_sigur(invoice_info.get("lastBillIssuedAmount"))
            if valoare is None and istoric_curent:
                valoare = _float_sigur(istoric_curent.get("issuedAmount"))

            data_emitere = _data_sigura(invoice_info.get("lastBillIssueDate"))
            if data_emitere is None and istoric_curent:
                data_emitere = _data_sigura(istoric_curent.get("issueDate"))

            data_scadenta = _data_sigura(last_bill.get("dueDate"))
            rest_plata = _float_sigur(balance.get("serviceBalanceAmount"))
            if rest_plata is None:
                rest_plata = _float_sigur(balance.get("totalBalanceAmount"))
            if rest_plata is None and istoric_curent:
                rest_plata = _float_sigur(istoric_curent.get("serviceBalanceAmount"))

            stare = _stare_factura(rest_plata, data_scadenta, istoric_curent)
            cont = conturi_dupa_id.get(msisdn)
            raw = {
                "invoice_info": invoice_info,
                "last_bill": last_bill,
                "balance_data": balance,
                "invoice_response": raspuns,
                "history_item": istoric_curent,
                "rest_plata": rest_plata,
                "amount_remaining": rest_plata,
                "subscriber": getattr(cont, "date_brute", {}) if cont else {},
            }

            facturi.append(
                FacturaUtilitate(
                    id_factura=reference,
                    titlu=f"Factura Orange {msisdn}",
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=data_emitere,
                    data_scadenta=data_scadenta,
                    stare=stare,
                    categorie="consum",
                    id_cont=msisdn,
                    id_contract=getattr(cont, "id_contract", None) if cont else None,
                    tip_utilitate="telecom",
                    tip_serviciu="abonament",
                    date_brute=raw,
                )
            )

        facturi.sort(key=lambda item: item.data_emitere or date.min, reverse=True)
        return facturi

    def _mapeaza_consumuri(
        self,
        date_brute: dict[str, Any],
        conturi: list[ContUtilitate],
        facturi: list[FacturaUtilitate],
    ) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        total_sold = 0.0
        total_de_plata = 0.0
        are_sold = False

        for cont in conturi:
            raw = cont.date_brute if isinstance(cont.date_brute, dict) else {}

            consumuri.extend(
                [
                    ConsumUtilitate("subscriber_id", _text(raw.get("subscriberId")), None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("profile_id", _text(raw.get("profileId")), None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("nume_abonament", _text(raw.get("subscriptionName")), None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("status_serviciu", _text(raw.get("status")), None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                ]
            )

            if cont.tip_serviciu != "abonament":
                continue

            factura = next((item for item in facturi if item.id_cont == cont.id_cont), None)
            factura_raw = factura.date_brute if factura and isinstance(factura.date_brute, dict) else {}
            balance = factura_raw.get("balance_data") if isinstance(factura_raw.get("balance_data"), dict) else {}
            last_bill = factura_raw.get("last_bill") if isinstance(factura_raw.get("last_bill"), dict) else {}
            invoice_info = factura_raw.get("invoice_info") if isinstance(factura_raw.get("invoice_info"), dict) else {}

            sold = _float_sigur(balance.get("serviceBalanceAmount"))
            if sold is None:
                sold = _float_sigur(balance.get("totalBalanceAmount"))
            if sold is not None:
                are_sold = True
                total_sold += sold
                total_de_plata += max(sold, 0.0)

            consumuri.extend(
                [
                    ConsumUtilitate("sold_curent", sold, "RON", id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("de_plata", max(sold or 0.0, 0.0), "RON", id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("sold_factura", sold, "RON", id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("factura_restanta", "da" if (sold or 0.0) > 0 else "nu", None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("valoare_ultima_factura", factura.valoare if factura else None, "RON", id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("id_ultima_factura", factura.id_factura if factura else None, None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("urmatoarea_scadenta", factura.data_scadenta.isoformat() if factura and factura.data_scadenta else None, None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("data_ultima_factura", factura.data_emitere.isoformat() if factura and factura.data_emitere else None, None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("cod_client", _text((factura_raw.get("invoice_response") or {}).get("data", {}).get("customerNumber")), None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("data_urmatoarei_facturi", _data_iso(invoice_info.get("nextBillDate")), None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                    ConsumUtilitate("zi_facturare", last_bill.get("billDay"), None, id_cont=cont.id_cont, tip_utilitate="telecom", tip_serviciu=cont.tip_serviciu),
                ]
            )

        user_info = date_brute.get("user_info") if isinstance(date_brute.get("user_info"), dict) else {}
        consumuri.extend(
            [
                ConsumUtilitate("sold_curent", round(total_sold, 2) if are_sold else None, "RON"),
                ConsumUtilitate("de_plata", round(total_de_plata, 2) if are_sold else None, "RON"),
                ConsumUtilitate("total_neachitat", round(total_de_plata, 2) if are_sold else None, "RON"),
                ConsumUtilitate("numar_servicii", len(conturi), "buc"),
                ConsumUtilitate("numar_abonamente_active", sum(1 for cont in conturi if cont.tip_serviciu == "abonament" and str(cont.stare).upper() == "ACTIVE"), "buc"),
                ConsumUtilitate("numar_cartele_prepay", sum(1 for cont in conturi if cont.tip_serviciu == "prepay"), "buc"),
                ConsumUtilitate("numar_facturi", len(facturi), "buc"),
                ConsumUtilitate("client", _text(user_info.get("name")), None),
                ConsumUtilitate("email", _text(user_info.get("email")), None),
            ]
        )
        return consumuri

    def _construieste_extra(
        self,
        date_brute: dict[str, Any],
        conturi: list[ContUtilitate],
        facturi: list[FacturaUtilitate],
    ) -> dict[str, Any]:
        user_info = date_brute.get("user_info") if isinstance(date_brute.get("user_info"), dict) else {}
        return {
            "user_info": user_info,
            "sumar": {
                "client": _text(user_info.get("name")),
                "email": _text(user_info.get("email")),
                "numar_servicii": len(conturi),
                "numar_facturi_curente": len(facturi),
                "total_de_plata": sum(max(_float_sigur(f.date_brute.get("rest_plata")) or 0.0, 0.0) for f in facturi),
                "ultima_factura_id": facturi[0].id_factura if facturi else None,
                "ultima_factura_valoare": facturi[0].valoare if facturi else None,
            },
            "date_brute": {
                "subscribers_count": len(date_brute.get("subscribers", []) or []),
                "invoice_infos_count": len(date_brute.get("invoice_infos", {}) or {}),
                "invoice_history_customers": list((date_brute.get("invoice_history", {}) or {}).keys()),
            },
        }


def _text(valoare: Any) -> str:
    if valoare in (None, "", "null"):
        return ""
    return str(valoare).strip()


def _int_sigur(valoare: Any, default: int = 0) -> int:
    try:
        if valoare in (None, "", "null"):
            return default
        return int(float(valoare))
    except (TypeError, ValueError):
        return default


def _float_sigur(valoare: Any) -> float | None:
    if valoare in (None, "", "null"):
        return None
    try:
        if isinstance(valoare, str):
            valoare = valoare.replace(" ", "").replace(",", ".")
        return round(float(valoare), 2)
    except (TypeError, ValueError):
        return None


def _data_sigura(valoare: Any) -> date | None:
    if not valoare:
        return None
    text = str(valoare).strip().replace("Z", "+00:00")
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _data_iso(valoare: Any) -> str | None:
    parsed = _data_sigura(valoare)
    return parsed.isoformat() if parsed else None


def _subscriber_facturabil(subscriber: dict[str, Any]) -> bool:
    status = _text(subscriber.get("status")).upper()
    tip = _text(subscriber.get("subscriberType")).upper()
    return status == "ACTIVE" and tip == "POSTPAY"


def _extrage_customer_number(raspuns_invoice: dict[str, Any] | None) -> str:
    if not isinstance(raspuns_invoice, dict):
        return ""
    data = raspuns_invoice.get("data")
    if not isinstance(data, dict):
        return ""
    return _text(data.get("customerNumber"))


def _istoric_facturi_dupa_msisdn(history_by_customer: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rezultat: dict[str, list[dict[str, Any]]] = {}
    for history in history_by_customer.values():
        if not isinstance(history, dict):
            continue
        items = history.get("data")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            msisdn = _text(item.get("msisdn"))
            if not msisdn:
                continue
            rezultat.setdefault(msisdn, []).append(item)
    for items in rezultat.values():
        items.sort(key=lambda item: _data_sigura(item.get("issueDate")) or date.min, reverse=True)
    return rezultat


def _alege_factura_istoric(items: list[dict[str, Any]], reference: str) -> dict[str, Any] | None:
    if reference:
        for item in items:
            if _text(item.get("reference")) == reference:
                return item
    return items[0] if items else None


def _stare_factura(rest_plata: float | None, data_scadenta: date | None, istoric: dict[str, Any] | None) -> str:
    if rest_plata is not None:
        if rest_plata > 0:
            if data_scadenta and data_scadenta < date.today():
                return "scadenta"
            return "neplatita"
        return "platita"
    status = _text((istoric or {}).get("status")).lower()
    if status:
        if "achit" in status or "plat" in status or "paid" in status:
            return "platita"
        return status
    return "necunoscuta"
