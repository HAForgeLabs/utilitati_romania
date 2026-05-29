from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, quote_plus

import certifi
from curl_cffi import requests

from ..const import (
    CONF_ACCOUNT_ID,
    CONF_CONTRACT_ACCOUNT_ID,
    CONF_CONTRACT_ID,
    CONF_PREMISE_LABEL,
)
from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from ..naming import build_location_alias
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://portal.apacansb.ro"
URL_LOGIN_APLICATIE = f"{URL_BAZA}/sap/bc/ui5_ui5/sap/UMCUI5_MOBILE/index.html"
URL_SERVICIU = f"{URL_BAZA}/sap/opu/odata/sap/ERP_UTILITIES_UMC/"
URL_BATCH = f"{URL_SERVICIU}$batch"
CLIENT_SAP = "001"
LIMBA_LOGIN = "EN"
LIMBA_FORMULAR = "RO"

ANTETE_IMPLICITE = {
    "Accept-Language": "ro-RO,ro;q=0.9,en-RO;q=0.8,en;q=0.7,en-US;q=0.6,de;q=0.5",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}

ACCEPT_LOGIN = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)


class EroareApiApaCanal(Exception):
    pass


class EroareAutentificareApaCanal(EroareApiApaCanal):
    pass


@dataclass(slots=True)
class OptiuneContractApaCanal:
    account_id: str
    contract_account_id: str
    contract_id: str
    eticheta: str


@dataclass(slots=True)
class DateSesiuneApaCanal:
    username: str
    authenticated: bool


def _sap_date_to_datetime(valoare: str | None) -> datetime | None:
    if not valoare:
        return None
    match = re.search(r"/Date\((\d+)\)/", valoare)
    if not match:
        return None
    timestamp_ms = int(match.group(1))
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def _sap_date_to_date(valoare: str | None):
    dt = _sap_date_to_datetime(valoare)
    return dt.date() if dt else None


def _sap_date_to_iso(valoare: str | None) -> str | None:
    data = _sap_date_to_date(valoare)
    return data.isoformat() if data else None


def _float_or_none(valoare: Any) -> float | None:
    if valoare in (None, "", "null"):
        return None
    try:
        return float(valoare)
    except (TypeError, ValueError):
        return None


def _construieste_body_login(utilizator: str, parola: str, xsrf: str) -> str:
    parti: list[tuple[str, str]] = [
        ("sap-system-login-oninputprocessing", ""),
        ("sap-urlscheme", ""),
        ("sap-system-login", "onLogin"),
        ("sap-system-login-basic_auth", ""),
        ("sap-client", CLIENT_SAP),
        ("sap-language", LIMBA_LOGIN),
        ("sap-accessibility", ""),
        ("sap-login-XSRF", xsrf),
        ("sap-system-login-cookie_disabled", ""),
        ("sap-hash", ""),
        ("sap-alias", utilizator),
        ("sap-password", parola),
        ("sap-language", LIMBA_FORMULAR),
    ]
    return "&".join(f"{quote_plus(k)}={quote_plus(v)}" for k, v in parti)


class ApiApaCanal:
    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._csrf_token: str | None = None
        self._fallback_ssl_insecure = False
        self._session = self._create_session()

    def _create_session(self) -> requests.Session:
        sesiune = requests.Session(impersonate="chrome124", timeout=30)
        sesiune.headers.update(ANTETE_IMPLICITE)
        return sesiune

    def _reset_session(self) -> None:
        self._session = self._create_session()
        self._csrf_token = None

    def _has_session_cookie(self) -> bool:
        return bool(self._session.cookies.get("SAP_SESSIONID_APP_001"))

    def _request(self, metoda: str, url: str, **kwargs):
        try:
            return self._session.request(metoda, url, verify=certifi.where(), **kwargs)
        except Exception as err:
            mesaj = str(err).lower()
            problema_ssl = (
                "certificate problem" in mesaj
                or "local issuer certificate" in mesaj
                or "ssl certificate" in mesaj
                or "curl: (60)" in mesaj
            )
            if not problema_ssl:
                raise
            if not self._fallback_ssl_insecure:
                _LOGGER.debug(
                    "Fallback SSL activat pentru Apă Canal Sibiu (verify=False)"
                )
                self._fallback_ssl_insecure = True
            return self._session.request(metoda, url, verify=False, **kwargs)

    def _get_login_xsrf(self) -> str:
        headers = {
            **ANTETE_IMPLICITE,
            "Accept": ACCEPT_LOGIN,
            "Referer": URL_LOGIN_APLICATIE,
            "Upgrade-Insecure-Requests": "1",
        }
        raspuns = self._request("GET", URL_LOGIN_APLICATIE, headers=headers, allow_redirects=True)
        match = re.search(r'name="sap-login-XSRF"\s+value="([^"]+)"', raspuns.text)
        if not match:
            raise EroareAutentificareApaCanal("Nu am putut extrage tokenul de login din portal.")
        return html.unescape(match.group(1))

    def _fetch_csrf_token(self) -> None:
        headers = {
            **ANTETE_IMPLICITE,
            "Accept": "application/json",
            "DataServiceVersion": "2.0",
            "MaxDataServiceVersion": "2.0",
            "Referer": (
                "https://portal.apacansb.ro/sap/bc/ui5_ui5/sap/UMCUI5_MOBILE/"
                "index.html?sap-client=001&sap-language=EN"
            ),
            "X-CSRF-Token": "Fetch",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=0, i",
        }
        raspuns = self._request("GET", URL_SERVICIU, headers=headers)
        token = raspuns.headers.get("x-csrf-token") or raspuns.headers.get("X-CSRF-Token")
        if not token:
            raise EroareAutentificareApaCanal(
                "Portalul nu a returnat x-csrf-token după autentificare."
            )
        self._csrf_token = token

    def login(self, utilizator: str, parola: str) -> None:
        self._reset_session()
        xsrf = self._get_login_xsrf()
        body = _construieste_body_login(utilizator, parola, xsrf)
        headers = {
            **ANTETE_IMPLICITE,
            "Accept": ACCEPT_LOGIN,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": URL_BAZA,
            "Referer": URL_LOGIN_APLICATIE,
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Priority": "u=0, i",
        }
        raspuns = self._request(
            "POST",
            URL_LOGIN_APLICATIE,
            data=body,
            headers=headers,
            allow_redirects=False,
        )
        locatie = raspuns.headers.get("Location")
        if raspuns.status_code not in (302, 303):
            text = raspuns.text
            if 'name="sap-password"' in text or 'name="sap-alias"' in text:
                raise EroareAutentificareApaCanal(
                    "Răspunsul după login încă arată formularul de autentificare."
                )
            raise EroareAutentificareApaCanal(
                f"Autentificare eșuată. HTTP {raspuns.status_code}."
            )
        if not self._session.cookies.get("SAP_SESSIONID_APP_001"):
            raise EroareAutentificareApaCanal(
                "Login respins: portalul nu a emis SAP_SESSIONID_APP_001."
            )
        if not locatie:
            raise EroareAutentificareApaCanal(
                "Login respins: lipsă antet Location după 302."
            )

        headers_redirect = {
            **ANTETE_IMPLICITE,
            "Accept": ACCEPT_LOGIN,
            "Referer": URL_LOGIN_APLICATIE,
            "Upgrade-Insecure-Requests": "1",
        }
        self._request(
            "GET",
            locatie if locatie.startswith("http") else f"{URL_BAZA}{locatie}",
            headers=headers_redirect,
            allow_redirects=True,
        )
        if not self._has_session_cookie():
            raise EroareAutentificareApaCanal(
                "Sesiunea SAP s-a pierdut după redirectul de autentificare."
            )

        self._username = utilizator
        self._password = parola
        self._fetch_csrf_token()

    def _ensure_login(self, utilizator: str, parola: str) -> None:
        aceleasi_credentiale = self._username == utilizator and self._password == parola
        if aceleasi_credentiale and self._has_session_cookie() and self._csrf_token:
            return
        self.login(utilizator, parola)

    def _batch_get(self, cale_relativa: str, *, allow_reauth: bool = True) -> dict[str, Any]:
        if not self._csrf_token:
            self._fetch_csrf_token()

        boundary = f"batch_{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}"
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n"
            "\r\n"
            f"GET {cale_relativa} HTTP/1.1\r\n"
            f"User-Agent: {ANTETE_IMPLICITE['User-Agent']}\r\n"
            "X-REQUESTED-WITH: XMLHttpRequest\r\n"
            "Accept-Language: ro\r\n"
            "Accept: application/json\r\n"
            "MaxDataServiceVersion: 2.0\r\n"
            "DataServiceVersion: 2.0\r\n"
            "\r\n"
            "\r\n"
            f"--{boundary}--\r\n"
        )
        headers = {
            **ANTETE_IMPLICITE,
            "Accept": "application/json",
            "Content-Type": f"multipart/mixed;boundary={boundary}",
            "DataServiceVersion": "2.0",
            "MaxDataServiceVersion": "2.0",
            "Origin": URL_BAZA,
            "Referer": (
                "https://portal.apacansb.ro/sap/bc/ui5_ui5/sap/UMCUI5_MOBILE/"
                "index.html?sap-client=001&sap-language=EN"
            ),
            "X-CSRF-Token": self._csrf_token or "",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=0, i",
        }
        raspuns = self._request(
            "POST",
            URL_BATCH,
            data=body.encode("utf-8"),
            headers=headers,
        )
        text = raspuns.text
        pagina_login = (
            'name="sap-password"' in text
            or 'name="sap-alias"' in text
            or ("<!DOCTYPE HTML>" in text and '{"d":' not in text)
        )
        if raspuns.status_code in (401, 403) or pagina_login:
            if allow_reauth and self._username and self._password:
                self.login(self._username, self._password)
                return self._batch_get(cale_relativa, allow_reauth=False)
            raise EroareAutentificareApaCanal("Sesiunea portalului a expirat.")
        if raspuns.status_code >= 400:
            raise EroareApiApaCanal(f"Eroare HTTP la apelul batch SAP: {raspuns.status_code}")
        match = re.search(r'(\{"d":.*\})', text, re.DOTALL)
        if not match:
            raise EroareApiApaCanal("Răspuns batch invalid sau fără JSON.")
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as err:
            raise EroareApiApaCanal(f"Nu am putut parsa răspunsul JSON din batch: {err}") from err

    def _batch_post(self, cale_relativa: str, payload: dict[str, Any], *, allow_reauth: bool = True) -> dict[str, Any]:
        if not self._csrf_token:
            self._fetch_csrf_token()

        batch_boundary = f"batch_{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}"
        changeset_boundary = f"changeset_{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}"
        json_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("/", r"\/")
        body = (
            f"--{batch_boundary}\r\n"
            f"Content-Type: multipart/mixed; boundary={changeset_boundary}\r\n"
            "\r\n"
            f"--{changeset_boundary}\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n"
            "\r\n"
            f"POST {cale_relativa} HTTP/1.1\r\n"
            f"User-Agent: {ANTETE_IMPLICITE['User-Agent']}\r\n"
            "X-REQUESTED-WITH: XMLHttpRequest\r\n"
            "Accept-Language: en\r\n"
            "Accept: application/json\r\n"
            "MaxDataServiceVersion: 2.0\r\n"
            "DataServiceVersion: 2.0\r\n"
            f"x-csrf-token: {self._csrf_token or ''}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(json_payload.encode('utf-8'))}\r\n"
            "\r\n"
            f"{json_payload}\r\n"
            f"--{changeset_boundary}--\r\n"
            "\r\n"
            f"--{batch_boundary}--\r\n"
        )
        headers = {
            **ANTETE_IMPLICITE,
            "Accept": "application/json",
            "Content-Type": f"multipart/mixed;boundary={batch_boundary}",
            "DataServiceVersion": "2.0",
            "MaxDataServiceVersion": "2.0",
            "Origin": URL_BAZA,
            "Referer": (
                "https://portal.apacansb.ro/sap/bc/ui5_ui5/sap/UMCUI5_MOBILE/"
                "index.html?sap-client=001&sap-language=EN"
            ),
            "X-CSRF-Token": self._csrf_token or "",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=0, i",
        }
        raspuns = self._request("POST", URL_BATCH, data=body.encode("utf-8"), headers=headers)
        text = raspuns.text
        pagina_login = (
            'name="sap-password"' in text
            or 'name="sap-alias"' in text
            or ("<!DOCTYPE HTML>" in text and '{"d":' not in text)
        )
        if raspuns.status_code in (401, 403) or pagina_login:
            if allow_reauth and self._username and self._password:
                self.login(self._username, self._password)
                return self._batch_post(cale_relativa, payload, allow_reauth=False)
            raise EroareAutentificareApaCanal("Sesiunea portalului a expirat.")
        if raspuns.status_code >= 400:
            raise EroareApiApaCanal(f"Eroare HTTP la transmiterea indexului Apă Canal: {raspuns.status_code}")

        match = re.search(r'(\{"d":.*?\})(?:\r?\n--|$)', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError as err:
                raise EroareApiApaCanal(
                    f"Nu am putut parsa răspunsul JSON după transmiterea indexului: {err}"
                ) from err

        statusuri_interne = re.findall(r"HTTP/1\.1\s+(\d{3})\s*([^\r\n]*)", text)
        if statusuri_interne:
            status, descriere = statusuri_interne[-1]
            if status.startswith("2"):
                return {"d": {"success": True, "status": status, "description": descriere.strip()}}

            mesaj_sap = ""
            mesaj_match = re.search(r'"message"\s*:\s*\{[^}]*"value"\s*:\s*"([^"]+)"', text, re.DOTALL)
            if mesaj_match:
                mesaj_sap = mesaj_match.group(1)
            else:
                text_curat = re.sub(r"\s+", " ", text).strip()
                mesaj_sap = text_curat[:500]
            if "does not have status mr order created" in mesaj_sap.lower():
                raise EroareApiApaCanal(
                    "Index deja transmis pentru perioada curentă. Portalul Apă Canal Sibiu nu mai permite o nouă transmitere pe același ordin de citire."
                )

            raise EroareApiApaCanal(
                f"Transmiterea indexului Apă Canal a fost respinsă de SAP: HTTP {status} {descriere.strip()}. {mesaj_sap}".strip()
            )

        fragment = re.sub(r"\s+", " ", text).strip()[:500]
        raise EroareApiApaCanal(
            f"Răspuns batch invalid după transmiterea indexului. Fragment răspuns: {fragment}"
        )

    def login_and_get_contract_choices(self, utilizator: str, parola: str) -> list[OptiuneContractApaCanal]:
        self.login(utilizator, parola)
        return self.get_contract_choices()

    def login_and_get_dashboard_data(
        self,
        utilizator: str,
        parola: str,
        account_id: str,
        contract_id: str,
        contract_account_id: str,
    ) -> dict[str, Any]:
        self._ensure_login(utilizator, parola)
        return self.get_dashboard_data(account_id, contract_id, contract_account_id)

    def login_and_submit_meter_reading(
        self,
        utilizator: str,
        parola: str,
        contract_id: str,
        device_id: str,
        register_id: str,
        reading_result: int | float | str,
    ) -> dict[str, Any]:
        self._ensure_login(utilizator, parola)
        return self.submit_meter_reading(contract_id, device_id, register_id, reading_result)

    def get_contract_choices(self) -> list[OptiuneContractApaCanal]:
        data = self._batch_get(
            "Accounts?$format=json&$expand=ContractAccounts,ContractAccounts/Contracts,ContractAccounts/Contracts/Premise"
        )
        rezultate = data.get("d", {}).get("results", [])
        optiuni: list[OptiuneContractApaCanal] = []
        for account in rezultate:
            account_id = account.get("AccountID")
            contract_accounts = account.get("ContractAccounts", {}).get("results", [])
            for contract_account in contract_accounts:
                contract_account_id = contract_account.get("ContractAccountID")
                contracts = contract_account.get("Contracts", {}).get("results", [])
                for contract in contracts:
                    contract_id = contract.get("ContractID")
                    premise = contract.get("Premise", {}) or {}
                    address_info = premise.get("AddressInfo", {}) or {}
                    eticheta = (
                        address_info.get("ShortForm")
                        or contract_account.get("Description")
                        or contract.get("Description")
                        or contract_id
                    )
                    if not account_id or not contract_account_id or not contract_id:
                        continue
                    optiuni.append(
                        OptiuneContractApaCanal(
                            account_id=account_id,
                            contract_account_id=contract_account_id,
                            contract_id=contract_id,
                            eticheta=build_location_alias(eticheta, contract_id),
                        )
                    )
        return optiuni

    def get_dashboard_data(
        self,
        account_id: str,
        contract_id: str,
        contract_account_id: str,
    ) -> dict[str, Any]:
        balance_data = self._batch_get(
            f"Accounts('{quote(account_id, safe='')}')/ContractAccounts?$format=json&$expand=ContractAccountBalance"
        )
        invoices_data = self._batch_get(
            f"Accounts('{quote(account_id, safe='')}')/Invoices?$format=json"
        )
        payments_data = self._batch_get(
            f"Accounts('{quote(account_id, safe='')}')/PaymentDocuments?$format=json"
        )
        consumption_data = self._batch_get(
            f"Contracts('{quote(contract_id, safe='')}')/ContractConsumptionValues?$expand=MeterReadingCategory&$format=json"
        )
        meter_data = self._batch_get(
            f"Contracts('{quote(contract_id, safe='')}')/MeterReadingResults?$format=json&$expand=MeterReadingStatus,MeterReadingCategory,MeterReadingReason"
        )
        devices_data = self._batch_get(
            f"Contracts('{quote(contract_id, safe='')}')/ContractAllDevices?$format=json"
        )
        try:
            billing_period_data = self._batch_get(
                f"GetCurrentBillingPeriod?ContractID='{quote(contract_id, safe='')}'"
            )
        except EroareApiApaCanal as err:
            _LOGGER.debug(
                "Nu am putut citi perioada curentă de transmitere index pentru Apă Canal Sibiu, contract=%s: %s",
                contract_id,
                err,
            )
            billing_period_data = {}

        balance_results = balance_data.get("d", {}).get("results", [])
        invoices = invoices_data.get("d", {}).get("results", [])
        payments = payments_data.get("d", {}).get("results", [])
        consumptions = consumption_data.get("d", {}).get("results", [])
        meter_results = meter_data.get("d", {}).get("results", [])
        devices = devices_data.get("d", {}).get("results", [])
        billing_period = self._normalize_billing_period(billing_period_data)
        reading_window = self._build_meter_reading_window(contract_id, devices, billing_period)

        sold_curent = None
        for item in balance_results:
            if item.get("ContractAccountID") == contract_account_id:
                sold_curent = item.get("ContractAccountBalance", {})
                break
        if sold_curent is None and balance_results:
            sold_curent = balance_results[0].get("ContractAccountBalance", {})

        ultima_factura = self._pick_latest(invoices, "InvoiceDate")
        ultima_plata = self._pick_latest(payments, "ExecutionDate")
        ultimul_consum = self._pick_latest(consumptions, "StartDate")
        ultimul_index = self._pick_latest(meter_results, "ReadingDateTime")

        return {
            "current_balance": {
                "value": _float_or_none((sold_curent or {}).get("CurrentBalance")),
                "currency": (sold_curent or {}).get("Currency"),
                "open_debits": _float_or_none((sold_curent or {}).get("OpenDebits")),
                "open_credits": _float_or_none((sold_curent or {}).get("OpenCredits")),
                "total_pending": _float_or_none((sold_curent or {}).get("TotalPending")),
            },
            "last_invoice": self._normalize_invoice(ultima_factura),
            "last_payment": self._normalize_payment(ultima_plata),
            "last_consumption": self._normalize_consumption(ultimul_consum),
            "last_meter_reading": self._normalize_meter_reading(ultimul_index),
            "meter_reading_window": reading_window,
        }

    def _build_meter_reading_window(
        self,
        contract_id: str,
        devices: list[dict[str, Any]],
        billing_period: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        registers: list[dict[str, Any]] = []
        installed_devices = [
            device
            for device in devices
            if bool(device.get("CurrentlyInstalledFlag"))
        ]
        devices_to_check = installed_devices or devices

        for device in devices_to_check:
            device_id = str(device.get("DeviceID") or "").strip()
            if not device_id:
                continue
            try:
                data = self._batch_get(
                    f"Devices('{quote(device_id, safe='')}')/RegistersToRead?$format=json&$expand=RegisterType,MeterReadingReason,MeterReadingCategory"
                )
            except EroareApiApaCanal as err:
                _LOGGER.debug(
                    "Nu am putut citi registrele de transmis pentru Apă Canal Sibiu, contract=%s, device=%s: %s",
                    contract_id,
                    device_id,
                    err,
                )
                continue
            for item in data.get("d", {}).get("results", []) or []:
                register_id = str(item.get("RegisterID") or "").strip()
                if register_id:
                    registers.append(self._normalize_register_to_read(item, device))

        is_open = bool(registers)
        today = datetime.now(timezone.utc).date()
        start_date = (billing_period or {}).get("start_date")
        end_date = (billing_period or {}).get("end_date")
        if is_open and not start_date:
            start_date = today.isoformat()
        if is_open and not end_date:
            end_date = _last_day_of_month(today).isoformat()

        period = None
        if is_open:
            period = _format_meter_reading_period(start_date, end_date)
        elif start_date and end_date:
            period = _format_meter_reading_period(start_date, end_date)

        return {
            "available": bool(is_open or (start_date and end_date)),
            "is_open": is_open,
            "start_date": start_date,
            "end_date": end_date,
            "period": period,
            "contract_id": contract_id,
            "registers": registers,
        }

    def _normalize_billing_period(self, data: dict[str, Any]) -> dict[str, Any]:
        item = (data.get("d") or {}).get("GetCurrentBillingPeriod") or {}
        return {
            "start_date": _sap_date_to_iso(item.get("StartDate")),
            "end_date": _sap_date_to_iso(item.get("EndDate")),
        }

    def _normalize_register_to_read(self, item: dict[str, Any], device: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "device_id": item.get("DeviceID") or (device or {}).get("DeviceID"),
            "register_id": item.get("RegisterID"),
            "unit": item.get("ReadingUnit") or "M3",
            "integer_places": item.get("IntegerPlaces"),
            "decimal_places": item.get("DecimalPlaces"),
            "previous_reading": _float_or_none(item.get("PreviousMeterReadingResult")),
            "previous_reading_date": _sap_date_to_iso(item.get("PreviousMeterReadingDate")),
            "previous_reason_id": item.get("PreviousMeterReadingReasonID"),
            "previous_category_id": item.get("PreviousMeterReadingCategoryID"),
            "serial_number": item.get("SerialNumber") or (device or {}).get("SerialNumber"),
            "reason": (item.get("MeterReadingReason") or {}).get("Description"),
            "reason_id": item.get("PreviousMeterReadingReasonID") or (item.get("MeterReadingReason") or {}).get("MeterReadingReasonID"),
            "category": (item.get("MeterReadingCategory") or {}).get("Description"),
            "category_id": item.get("PreviousMeterReadingCategoryID") or (item.get("MeterReadingCategory") or {}).get("MeterReadingCategoryID"),
            "register_type": (item.get("RegisterType") or {}).get("Description"),
        }

    def submit_meter_reading(
        self,
        contract_id: str,
        device_id: str,
        register_id: str,
        reading_result: int | float | str,
    ) -> dict[str, Any]:
        if not device_id or not register_id:
            devices_data = self._batch_get(f"Contracts('{quote(contract_id, safe='')}')/ContractAllDevices?$format=json")
            window = self._build_meter_reading_window(contract_id, devices_data.get("d", {}).get("results", []) or [])
            registers = window.get("registers") or []
            if registers:
                device_id = device_id or str(registers[0].get("device_id") or "")
                register_id = register_id or str(registers[0].get("register_id") or "")

        if not device_id or not register_id:
            raise EroareApiApaCanal("Nu am putut identifica dispozitivul și registrul pentru transmiterea indexului.")

        try:
            valoare = str(int(float(reading_result)))
        except (TypeError, ValueError) as err:
            raise EroareApiApaCanal("Valoarea indexului nu este validă.") from err

        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload = {
            "DependentMeterReadingResults": [],
            "DeviceID": str(device_id),
            "RegisterID": str(register_id),
            "ReadingResult": valoare,
            "ReadingDateTime": f"/Date({timestamp_ms})/",
            "MeterReadingNoteID": "",
        }
        return self._batch_post(
            f"Devices('{quote(str(device_id), safe='')}')/MeterReadingResults",
            payload,
        )

    def _pick_latest(self, items: list[dict[str, Any]], camp_data: str) -> dict[str, Any] | None:
        if not items:
            return None

        def sort_key(item: dict[str, Any]) -> int:
            raw = item.get(camp_data)
            match = re.search(r"/Date\((\d+)\)/", raw or "")
            return int(match.group(1)) if match else 0

        return sorted(items, key=sort_key, reverse=True)[0]

    def _normalize_invoice(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        return {
            "number": item.get("InvoiceID"),
            "issue_date": _sap_date_to_iso(item.get("InvoiceDate")),
            "due_date": _sap_date_to_iso(item.get("DueDate")),
            "amount": _float_or_none(item.get("AmountDue")),
            "currency": item.get("Currency"),
            "amount_paid": _float_or_none(item.get("AmountPaid")),
            "amount_remaining": _float_or_none(item.get("AmountRemaining")),
            "description": item.get("InvoiceDescription"),
        }

    def _normalize_payment(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        return {
            "document_id": item.get("PaymentDocumentID"),
            "date": _sap_date_to_iso(item.get("ExecutionDate")),
            "amount": _float_or_none(item.get("Amount")),
            "currency": item.get("Currency"),
            "method": item.get("PaymentMethodDescription"),
            "payment_type": item.get("PaymentType"),
        }

    def _normalize_consumption(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        return {
            "value": _float_or_none(item.get("ConsumptionValue")),
            "unit": item.get("ConsumptionUnit"),
            "start_date": _sap_date_to_iso(item.get("StartDate")),
            "end_date": _sap_date_to_iso(item.get("EndDate")),
            "billing_period_year": item.get("BillingPeriodYear"),
            "billing_period_month": item.get("BillingPeriodMonth"),
            "reading_category": (item.get("MeterReadingCategory") or {}).get("Description"),
            "billed_amount": _float_or_none(item.get("BilledAmount")),
            "currency": item.get("Currency"),
        }

    def _normalize_meter_reading(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        return {
            "value": _float_or_none(item.get("ReadingResult")),
            "date": _sap_date_to_iso(item.get("ReadingDateTime")),
            "unit": item.get("ReadingUnit"),
            "consumption": _float_or_none(item.get("Consumption")),
            "reason": (item.get("MeterReadingReason") or {}).get("Description"),
            "category": (item.get("MeterReadingCategory") or {}).get("Description"),
            "status": (item.get("MeterReadingStatus") or {}).get("Description"),
            "invoice_status": item.get("InvoiceStatus"),
            "serial_number": item.get("SerialNumber"),
        }


def _last_day_of_month(value: date) -> date:
    if value.month == 12:
        return date(value.year, 12, 31)
    return date(value.year, value.month + 1, 1) - timedelta(days=1)


def _format_date_ro(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return value
    return parsed.strftime("%d.%m.%Y")


def _format_meter_reading_period(start_date: str | None, end_date: str | None) -> str | None:
    start = _format_date_ro(start_date)
    end = _format_date_ro(end_date)
    if start and end:
        return f"{start} - {end}"
    return start or end


class ClientFurnizorApaCanal(ClientFurnizor):
    cheie_furnizor = "apa_canal"
    nume_prietenos = "Apă Canal Sibiu"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ApiApaCanal()

    async def async_obtine_contracte_disponibile(self) -> list[OptiuneContractApaCanal]:
        try:
            return await asyncio.to_thread(
                self.api.login_and_get_contract_choices,
                self.utilizator,
                self.parola,
            )
        except EroareAutentificareApaCanal as err:
            raise EroareAutentificare(str(err)) from err
        except EroareApiApaCanal as err:
            raise EroareConectare(str(err)) from err
        except Exception as err:
            raise EroareParsare(f"Eroare neașteptată la obținerea contractelor Apă Canal: {err}") from err

    async def async_testeaza_conexiunea(self) -> str:
        contracte = await self.async_obtine_contracte_disponibile()
        if contracte:
            prima = contracte[0]
            return f"{prima.account_id}_{prima.contract_id}"
        return self.utilizator.lower()

    async def async_transmite_index(self, contract_id: str, device_id: str, register_id: str, index_value: int | float | str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self.api.login_and_submit_meter_reading,
                self.utilizator,
                self.parola,
                contract_id,
                device_id,
                register_id,
                index_value,
            )
        except EroareAutentificareApaCanal as err:
            raise EroareAutentificare(str(err)) from err
        except EroareApiApaCanal as err:
            raise EroareConectare(str(err)) from err
        except Exception as err:
            raise EroareParsare(f"Eroare neașteptată la transmiterea indexului Apă Canal: {err}") from err

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        account_id = str(self.optiuni.get(CONF_ACCOUNT_ID) or "").strip()
        contract_id = str(self.optiuni.get(CONF_CONTRACT_ID) or "").strip()
        contract_account_id = str(self.optiuni.get(CONF_CONTRACT_ACCOUNT_ID) or "").strip()
        eticheta = str(self.optiuni.get(CONF_PREMISE_LABEL) or contract_id or self.utilizator).strip()

        if not account_id or not contract_id or not contract_account_id:
            raise EroareParsare("Configurația Apă Canal este incompletă: lipsesc identificatorii contractului.")

        try:
            date_brute = await asyncio.to_thread(
                self.api.login_and_get_dashboard_data,
                self.utilizator,
                self.parola,
                account_id,
                contract_id,
                contract_account_id,
            )
        except EroareAutentificareApaCanal as err:
            raise EroareAutentificare(str(err)) from err
        except EroareApiApaCanal as err:
            raise EroareConectare(str(err)) from err
        except Exception as err:
            raise EroareParsare(f"Eroare neașteptată la citirea datelor Apă Canal: {err}") from err

        cont = ContUtilitate(
            id_cont=account_id,
            id_contract=contract_id,
            nume=eticheta,
            tip_cont="contract",
            adresa=eticheta,
            stare="activ",
            tip_utilitate="apa",
            tip_serviciu="apa_canal",
            date_brute=date_brute,
        )

        facturi: list[FacturaUtilitate] = []
        ultima_factura = date_brute.get("last_invoice") or {}
        if ultima_factura:
            facturi.append(
                FacturaUtilitate(
                    id_factura=str(ultima_factura.get("number") or contract_id),
                    titlu="Ultima factură",
                    valoare=_float_or_none(ultima_factura.get("amount")),
                    moneda=ultima_factura.get("currency") or "RON",
                    data_emitere=(
                        datetime.fromisoformat(ultima_factura["issue_date"]).date()
                        if ultima_factura.get("issue_date")
                        else None
                    ),
                    data_scadenta=(
                        datetime.fromisoformat(ultima_factura["due_date"]).date()
                        if ultima_factura.get("due_date")
                        else None
                    ),
                    stare=None,
                    categorie="factura",
                    id_cont=account_id,
                    id_contract=contract_id,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=ultima_factura,
                )
            )

        consumuri: list[ConsumUtilitate] = []

        ultimul_consum = date_brute.get("last_consumption") or {}
        consumuri.append(
            ConsumUtilitate(
                cheie="last_consumption",
                valoare=_float_or_none(ultimul_consum.get("value")),
                unitate=ultimul_consum.get("unit") or "m³",
                perioada=ultimul_consum.get("end_date") or ultimul_consum.get("start_date"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=ultimul_consum,
            )
        )
        consumuri.append(
            ConsumUtilitate(
                cheie="ultim_consum",
                valoare=_float_or_none(ultimul_consum.get("value")),
                unitate=ultimul_consum.get("unit") or "m³",
                perioada=ultimul_consum.get("end_date") or ultimul_consum.get("start_date"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=ultimul_consum,
            )
        )

        ultimul_index = date_brute.get("last_meter_reading") or {}
        consumuri.append(
            ConsumUtilitate(
                cheie="last_meter_reading",
                valoare=_float_or_none(ultimul_index.get("value")),
                unitate=ultimul_index.get("unit") or "m³",
                perioada=ultimul_index.get("date"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=ultimul_index,
            )
        )
        consumuri.append(
            ConsumUtilitate(
                cheie="ultim_index",
                valoare=_float_or_none(ultimul_index.get("value")),
                unitate=ultimul_index.get("unit") or "m³",
                perioada=ultimul_index.get("date"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=ultimul_index,
            )
        )

        fereastra_index = date_brute.get("meter_reading_window") or {}
        registre_index = fereastra_index.get("registers") or []
        primul_registru = registre_index[0] if registre_index else {}
        citire_permisa = bool(fereastra_index.get("is_open"))
        consumuri.append(
            ConsumUtilitate(
                cheie="citire_index_permisa",
                valoare="da" if citire_permisa else "nu",
                unitate=None,
                perioada=fereastra_index.get("period"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=fereastra_index,
            )
        )
        consumuri.append(
            ConsumUtilitate(
                cheie="perioada_citire",
                valoare=fereastra_index.get("period") or "",
                unitate=None,
                perioada=fereastra_index.get("period"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=fereastra_index,
            )
        )
        consumuri.append(
            ConsumUtilitate(
                cheie="zile_pana_citire_index",
                valoare=0 if citire_permisa else None,
                unitate="zile",
                perioada=fereastra_index.get("period"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=fereastra_index,
            )
        )
        consumuri.append(
            ConsumUtilitate(
                cheie="index_de_transmis",
                valoare=_float_or_none(primul_registru.get("previous_reading")),
                unitate=primul_registru.get("unit") or "M3",
                perioada=primul_registru.get("previous_reading_date"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=primul_registru,
            )
        )

        sold = date_brute.get("current_balance") or {}
        consumuri.append(
            ConsumUtilitate(
                cheie="current_balance",
                valoare=_float_or_none(sold.get("value")),
                unitate=sold.get("currency") or "RON",
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=sold,
            )
        )
        consumuri.append(
            ConsumUtilitate(
                cheie="sold_curent",
                valoare=_float_or_none(sold.get("value")),
                unitate=sold.get("currency") or "RON",
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=sold,
            )
        )

        ultima_plata = date_brute.get("last_payment") or {}
        consumuri.append(
            ConsumUtilitate(
                cheie="last_payment",
                valoare=_float_or_none(ultima_plata.get("amount")),
                unitate=ultima_plata.get("currency") or "RON",
                perioada=ultima_plata.get("date"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=ultima_plata,
            )
        )
        consumuri.append(
            ConsumUtilitate(
                cheie="ultima_plata",
                valoare=_float_or_none(ultima_plata.get("amount")),
                unitate=ultima_plata.get("currency") or "RON",
                perioada=ultima_plata.get("date"),
                id_cont=account_id,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=ultima_plata,
            )
        )

        if ultima_factura:
            consumuri.append(
                ConsumUtilitate(
                    cheie="last_invoice",
                    valoare=_float_or_none(ultima_factura.get("amount")),
                    unitate=ultima_factura.get("currency") or "RON",
                    perioada=ultima_factura.get("issue_date"),
                    id_cont=account_id,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=ultima_factura,
                )
            )
            consumuri.append(
                ConsumUtilitate(
                    cheie="ultima_factura",
                    valoare=_float_or_none(ultima_factura.get("amount")),
                    unitate=ultima_factura.get("currency") or "RON",
                    perioada=ultima_factura.get("issue_date"),
                    id_cont=account_id,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=ultima_factura,
                )
            )
            consumuri.append(
                ConsumUtilitate(
                    cheie="valoare_ultima_factura",
                    valoare=_float_or_none(ultima_factura.get("amount")),
                    unitate=ultima_factura.get("currency") or "RON",
                    perioada=ultima_factura.get("issue_date"),
                    id_cont=account_id,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=ultima_factura,
                )
            )
            consumuri.append(
                ConsumUtilitate(
                    cheie="id_ultima_factura",
                    valoare=str(ultima_factura.get("number") or ""),
                    unitate=None,
                    perioada=ultima_factura.get("issue_date"),
                    id_cont=account_id,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=ultima_factura,
                )
            )

        extra = {
            "premise_label": eticheta,
            "account_id": account_id,
            "contract_id": contract_id,
            "contract_account_id": contract_account_id,
            "current_balance": date_brute.get("current_balance"),
            "last_invoice": date_brute.get("last_invoice"),
            "last_payment": date_brute.get("last_payment"),
            "last_consumption": date_brute.get("last_consumption"),
            "last_meter_reading": date_brute.get("last_meter_reading"),
            "meter_reading_window": date_brute.get("meter_reading_window"),
        }

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=[cont],
            facturi=facturi,
            consumuri=consumuri,
            extra=extra,
        )
