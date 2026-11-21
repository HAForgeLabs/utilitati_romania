"""Acces unificat la portalul Premier Energy pentru Utilități România."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
from datetime import date, datetime
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from curl_cffi import requests as curl_requests

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

PORTAL_URL = "https://app.premierenergy.ro"
API_BASE = f"{PORTAL_URL}/api"
AUTH_BASE = "https://login.premierenergy.ro"
AUTH_TENANT = "premierenergyrob2c.onmicrosoft.com"
AUTH_POLICY = "B2C_1A_signup_signin"
AUTH_PREFIX = f"/{AUTH_TENANT}/{AUTH_POLICY.lower()}"
AUTH_AUTHORIZE = f"{AUTH_BASE}{AUTH_PREFIX}/oauth2/v2.0/authorize"
AUTH_TOKEN = f"{AUTH_BASE}{AUTH_PREFIX}/oauth2/v2.0/token"
AUTH_CLIENT_ID = "5dc97de8-713e-431b-8d05-4319c180377a"
AUTH_RESOURCE = "4fea07ee-cea1-441e-a2e3-382276f2f19e"
AUTH_SCOPE = f"{AUTH_BASE}/{AUTH_RESOURCE}/access openid profile offline_access"
TOKEN_MARGIN_SECONDS = 120
MAX_PDF_BYTES = 16 * 1024 * 1024


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    if not (text := _text(value)):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _iso_date(value: Any) -> str | None:
    parsed = _date(value)
    return parsed.isoformat() if parsed else None


def _tip_serviciu(loc: dict[str, Any]) -> str:
    pod = _text(loc.get("PoD")).upper()
    distribuitor = _text(loc.get("Distribuitor")).upper()
    if pod.startswith("DGS") or any(s in distribuitor for s in ("GAZ", "DGSR")):
        return "gaz"
    if pod.startswith("RO") or any(s in distribuitor for s in ("ELECT", "DEO", "RETELE")):
        return "curent"
    return "energie"


def _tip_utilitate(serviciu: str) -> str:
    if serviciu == "gaz":
        return "gaz"
    if serviciu == "curent":
        return "energie electrică"
    return "energie"


def _invoice_amount(invoice: dict[str, Any]) -> float | None:
    return _number(invoice.get("INV_AMT_TOTAL"))


def _invoice_rest(invoice: dict[str, Any]) -> float | None:
    return _number(invoice.get("restPlata"))


class ClientApiPremierEnergy:
    """Sesiune OAuth PKCE și operații API fără efecte asupra contului."""

    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_exp = 0.0
        self._login_lock = asyncio.Lock()

    def _token_valid(self) -> bool:
        return bool(self._access_token) and time.time() + TOKEN_MARGIN_SECONDS < self._token_exp

    @staticmethod
    def _settings_from_html(html: str) -> dict[str, Any]:
        match = re.search(r"var\s+SETTINGS\s*=\s*(\{.*?\});", html, re.DOTALL)
        if not match:
            raise EroareParsare("Premier Energy: parametrii formularului de autentificare lipsesc")
        try:
            result = json.loads(match.group(1))
        except ValueError as err:
            raise EroareParsare("Premier Energy: formular de autentificare invalid") from err
        if not isinstance(result, dict) or not (result.get("csrf") and result.get("transId")):
            raise EroareParsare("Premier Energy: formularul de autentificare este incomplet")
        hosts = result.get("hosts")
        if not isinstance(hosts, dict) or _text(hosts.get("tenant")).lower() != f"/{AUTH_TENANT}/{AUTH_POLICY}".lower():
            raise EroareParsare("Premier Energy: configurația furnizorului de identitate s-a schimbat")
        return result

    @staticmethod
    def _extract_tokens(response: Any) -> tuple[str, str | None, float]:
        if response.status_code != 200:
            if response.status_code in (400, 401, 403):
                raise EroareAutentificare("Premier Energy: sesiunea OAuth nu a fost acceptată")
            raise EroareConectare(f"Premier Energy: emitere token HTTP {response.status_code}")
        try:
            data = response.json()
        except (TypeError, ValueError) as err:
            raise EroareParsare("Premier Energy: răspuns OAuth invalid") from err
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise EroareAutentificare("Premier Energy: tokenul de acces lipsește")
        try:
            lifetime = int(data.get("expires_in") or 3600)
        except (ValueError, TypeError):
            lifetime = 3600
        return data["access_token"], _text(data.get("refresh_token")) or None, time.time() + max(60, lifetime)

    def _autentificare_noua(self, session: Any) -> tuple[str, str | None, float]:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": AUTH_CLIENT_ID,
            "response_type": "code",
            "response_mode": "fragment",
            "redirect_uri": f"{PORTAL_URL}/",
            "scope": AUTH_SCOPE,
            "state": state,
            "nonce": secrets.token_urlsafe(24),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "client_info": "1",
        }
        start = session.get(AUTH_AUTHORIZE, params=params, timeout=40)
        if start.status_code != 200:
            raise EroareConectare(f"Premier Energy: inițializare OAuth HTTP {start.status_code}")
        settings = self._settings_from_html(start.text)
        flow = settings["hosts"]["tenant"]
        csrf = _text(settings["csrf"])
        transaction = _text(settings["transId"])
        policy = _text(settings["hosts"].get("policy")) or AUTH_POLICY
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": AUTH_BASE,
            "Referer": start.url,
            "X-CSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest",
        }
        login = session.post(
            f"{AUTH_BASE}{flow}/SelfAsserted",
            params={"tx": transaction, "p": policy},
            data={"request_type": "RESPONSE", "signInName": self._utilizator, "password": self._parola},
            headers=headers,
            timeout=40,
        )
        if login.status_code in (400, 401, 403):
            raise EroareAutentificare("Premier Energy: utilizator sau parolă respinsă")
        if login.status_code != 200:
            raise EroareConectare(f"Premier Energy: autentificare HTTP {login.status_code}")
        try:
            login_data = login.json()
        except (TypeError, ValueError) as err:
            raise EroareParsare("Premier Energy: răspuns de autentificare invalid") from err
        if not isinstance(login_data, dict) or str(login_data.get("status")) != "200":
            raise EroareAutentificare("Premier Energy: autentificarea nu a fost confirmată")
        confirmation = session.get(
            f"{AUTH_BASE}{flow}/api/CombinedSigninAndSignup/confirmed",
            params={"rememberMe": "false", "csrf_token": csrf, "tx": transaction, "p": policy},
            headers={"Referer": start.url}, allow_redirects=False, timeout=40,
        )
        if confirmation.status_code not in (301, 302, 303, 307, 308):
            raise EroareAutentificare("Premier Energy: codul OAuth nu a fost emis")
        redirect = urlsplit(confirmation.headers.get("location", ""))
        if redirect.scheme != "https" or redirect.netloc != "app.premierenergy.ro":
            raise EroareAutentificare("Premier Energy: destinație de autentificare neașteptată")
        values = parse_qs(redirect.fragment)
        code = (values.get("code") or [""])[0]
        if not code or (values.get("state") or [""])[0] != state:
            raise EroareAutentificare("Premier Energy: verificarea codului OAuth a eșuat")
        token_response = session.post(
            AUTH_TOKEN,
            data={
                "client_id": AUTH_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": f"{PORTAL_URL}/",
                "scope": AUTH_SCOPE,
                "client_info": "1",
            },
            headers={"Accept": "application/json", "Origin": PORTAL_URL, "Referer": f"{PORTAL_URL}/"},
            timeout=40,
        )
        return self._extract_tokens(token_response)

    @staticmethod
    def _reimprospatare_token(session: Any, refresh_token: str) -> tuple[str, str | None, float]:
        response = session.post(
            AUTH_TOKEN,
            data={
                "client_id": AUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": AUTH_SCOPE,
                "client_info": "1",
            },
            headers={"Accept": "application/json", "Origin": PORTAL_URL, "Referer": f"{PORTAL_URL}/"},
            timeout=40,
        )
        return ClientApiPremierEnergy._extract_tokens(response)

    def _obtine_token(self, refresh: str | None) -> tuple[str, str | None, float]:
        try:
            with curl_requests.Session(impersonate="chrome") as session:
                if refresh:
                    try:
                        return self._reimprospatare_token(session, refresh)
                    except EroareAutentificare:
                        _LOGGER.debug("Premier Energy: refresh OAuth respins; reautentificare completă")
                return self._autentificare_noua(session)
        except (EroareAutentificare, EroareConectare, EroareParsare):
            raise
        except Exception as err:
            _LOGGER.warning("Premier Energy: conexiune OAuth indisponibilă (%s)", type(err).__name__)
            raise EroareConectare("Premier Energy: conexiunea de autentificare a eșuat") from err

    async def async_login(self, *, force: bool = False) -> None:
        if not force and self._token_valid():
            return
        async with self._login_lock:
            if not force and self._token_valid():
                return
            access, refresh, expiration = await asyncio.to_thread(self._obtine_token, self._refresh_token)
            self._access_token = access
            if refresh:
                self._refresh_token = refresh
            self._token_exp = expiration

    async def _api_get(self, endpoint: str, *, params: dict[str, str] | None = None, pdf: bool = False) -> Any:
        if endpoint.startswith("/") or "?" in endpoint or ".." in endpoint:
            raise EroareParsare("Premier Energy: rută API invalidă")
        await self.async_login()
        for attempt in range(2):
            headers = {
                "Accept": "application/pdf, text/plain, */*" if pdf else "application/json, text/plain, */*",
                "Authorization": f"Bearer {self._access_token}",
                "Origin": PORTAL_URL,
                "Referer": f"{PORTAL_URL}/",
            }
            try:
                async with self._sesiune.get(
                    f"{API_BASE}/{endpoint}", params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=45 if pdf else 30),
                ) as response:
                    status = response.status
                    if status in (401, 403):
                        body = None
                    elif status >= 400:
                        raise EroareConectare(f"Premier Energy: API {endpoint} HTTP {status}")
                    else:
                        body = await response.read()
            except (EroareAutentificare, EroareConectare):
                raise
            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
                raise EroareConectare("Premier Energy: API indisponibil") from err
            if status in (401, 403):
                if attempt:
                    raise EroareAutentificare("Premier Energy: accesul la API a fost respins")
                await self.async_login(force=True)
                continue
            if pdf:
                return body
            try:
                return json.loads(body)
            except (ValueError, TypeError) as err:
                raise EroareParsare(f"Premier Energy: răspuns JSON invalid pentru {endpoint}") from err
        raise EroareAutentificare("Premier Energy: sesiunea nu a putut fi recuperată")

    async def async_locuri_consum(self) -> list[dict[str, Any]]:
        result = await self._api_get("locConsum", params={"cuAdresa": "true", "platform": "web"})
        if not isinstance(result, list):
            raise EroareParsare("Premier Energy: structură locuri de consum neașteptată")
        return result

    async def async_facturi(self) -> list[dict[str, Any]]:
        result = await self._api_get("facturi")
        if not isinstance(result, list):
            raise EroareParsare("Premier Energy: structură facturi neașteptată")
        return result

    async def async_consum(self) -> list[dict[str, Any]]:
        result = await self._api_get("consum")
        if not isinstance(result, list):
            raise EroareParsare("Premier Energy: structură consum neașteptată")
        return result

    async def async_plati_recente(self, pod: str) -> dict[str, Any]:
        result = await self._api_get("recentPayments", params={"pod": pod})
        if not isinstance(result, dict):
            raise EroareParsare("Premier Energy: structură plăți neașteptată")
        return result

    async def async_indexuri(self, contract: str) -> list[dict[str, Any]]:
        result = await self._api_get("readings", params={"contract": contract})
        if not isinstance(result, list):
            raise EroareParsare("Premier Energy: structură indexuri neașteptată")
        return result

    async def async_descarca_factura(self, identificator: str) -> bytes:
        invoice_id = _text(identificator)
        if not re.fullmatch(r"[0-9]{1,32}", invoice_id):
            raise EroareParsare("Premier Energy: identificator de factură invalid")
        blob = await self._api_get(f"facturi/{invoice_id}/pdf", pdf=True)
        if not isinstance(blob, bytes) or len(blob) > MAX_PDF_BYTES:
            raise EroareParsare("Premier Energy: PDF prea mare sau invalid")
        if blob.startswith(b"%PDF-"):
            pdf_data = blob
        else:
            try:
                pdf_data = base64.b64decode(blob.strip(), validate=True)
            except (ValueError, base64.binascii.Error) as err:
                raise EroareParsare("Premier Energy: conținut PDF invalid") from err
        if len(pdf_data) > MAX_PDF_BYTES or not pdf_data.startswith(b"%PDF-"):
            raise EroareParsare("Premier Energy: documentul returnat nu este PDF")
        return pdf_data

    async def async_transmite_index(self, pod: str, contract: str, index: int) -> str:
        raise EroareConectare(
            "Premier Energy: transmiterea indexului este indisponibilă temporar; "
            "noul endpoint nu a fost verificat"
        )


class ClientFurnizorPremierEnergy(ClientFurnizor):
    cheie_furnizor = "premier_energy"
    nume_prietenos = "Premier Energy"

    def __init__(self, *, sesiune: aiohttp.ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiPremierEnergy(sesiune, utilizator, parola)

    async def async_testeaza_conexiunea(self) -> str:
        locuri = await self.api.async_locuri_consum()
        if not locuri:
            raise EroareConectare("Premier Energy: contul nu conține locuri de consum")
        return _text(locuri[0].get("Partener")) or self.utilizator.strip().lower()

    async def async_descarca_factura(self, identificator: str) -> bytes:
        return await self.api.async_descarca_factura(identificator)

    async def async_transmite_index(self, id_cont: str, index: int) -> str:
        return await self.api.async_transmite_index(id_cont, "", index)

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        locuri = await self.api.async_locuri_consum()
        facturi_raw = await self.api.async_facturi()
        consum_raw = await self.api.async_consum()

        rows_consum_by_pod: dict[str, list[dict[str, Any]]] = {}
        for grupa in consum_raw:
            if not isinstance(grupa, dict):
                continue
            pod = _text(grupa.get("codlocConsum")).upper()
            rows: list[dict[str, Any]] = []
            for contract in grupa.get("contracte") or []:
                if not isinstance(contract, dict):
                    continue
                for item in contract.get("consum") or []:
                    if isinstance(item, dict):
                        rows.append(item)
            rows.sort(key=lambda item: _text(item.get("dataCitire")), reverse=True)
            if pod:
                rows_consum_by_pod[pod] = rows

        identifiers: dict[str, dict[str, set[str]]] = {
            "pod": defaultdict(set), "contract": defaultdict(set), "partner": defaultdict(set)
        }
        for place in locuri:
            if not isinstance(place, dict):
                continue
            pod = _text(place.get("PoD")).upper()
            if not pod:
                continue
            identifiers["pod"][pod].add(pod)
            for field in ("CONTR_FURN", "cont_contract"):
                if (value := _text(place.get(field)).upper()):
                    identifiers["contract"][value].add(pod)
            if (partner := _text(place.get("Partener")).upper()):
                identifiers["partner"][partner].add(pod)

        invoices_by_pod: dict[str, list[dict[str, Any]]] = defaultdict(list)
        unmatched = 0
        for invoice in facturi_raw:
            if not isinstance(invoice, dict):
                continue
            found: set[str] = set()
            for kind, fields in (
                ("pod", ("POD", "PoD", "pod")),
                ("contract", ("vertrag", "VERTRAG", "CONTR_FURN", "VKONT")),
                ("partner", ("PARTNER", "Partener", "partner")),
            ):
                possible: set[str] = set()
                for field in fields:
                    if (key := _text(invoice.get(field)).upper()):
                        possible.update(identifiers[kind].get(key, set()))
                if possible:
                    found = possible
                    break
            if len(found) == 1:
                invoices_by_pod[next(iter(found))].append(invoice)
            else:
                unmatched += 1
        if unmatched:
            _LOGGER.warning("Premier Energy: %d facturi nu pot fi asociate fără ambiguitate", unmatched)
        for invoices in invoices_by_pod.values():
            invoices.sort(key=lambda i: _text(i.get("BLDAT")), reverse=True)

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []
        today = datetime.now(ZoneInfo("Europe/Bucharest")).date()

        for loc in locuri:
            if not isinstance(loc, dict) or not (pod := _text(loc.get("PoD"))):
                continue
            pod_key = pod.upper()
            tip_serviciu = _tip_serviciu(loc)
            tip_utilitate = _tip_utilitate(tip_serviciu)
            contract = _text(loc.get("CONTR_FURN")) or _text(loc.get("cont_contract"))
            adresa = _text(loc.get("Adresa")) or None
            nume = adresa or _text(loc.get("Denumire")) or pod
            invoices = invoices_by_pod.get(pod_key, [])
            consumption = rows_consum_by_pod.get(pod_key, [])
            start = _date(loc.get("perioada_citire_start"))
            stop = _date(loc.get("perioada_citire_sfarsit"))
            window_open = bool(start and stop and start <= today <= stop)
            interval = f"{start:%d.%m.%Y} - {stop:%d.%m.%Y}" if start and stop else None
            outstanding_values = [_invoice_rest(inv) for inv in invoices]
            outstanding_known = bool(invoices) and all(value is not None for value in outstanding_values)
            sold_curent = (
                round(sum(value for value in outstanding_values if value is not None), 2)
                if outstanding_known else None
            )
            de_plata = max(0.0, sold_curent) if sold_curent is not None else None
            summary = [
                {
                    "id": _text(inv.get("OPBEL")),
                    "valoare": _invoice_amount(inv),
                    "rest_plata": _invoice_rest(inv),
                    "emitere": _iso_date(inv.get("BLDAT")),
                    "scadenta": _iso_date(inv.get("FAEDN")),
                }
                for inv in invoices[:12]
            ]
            conturi.append(ContUtilitate(
                id_cont=pod,
                nume=nume,
                tip_cont="loc_consum",
                id_contract=contract or None,
                adresa=adresa,
                stare="activ" if contract else None,
                tip_utilitate=tip_utilitate,
                tip_serviciu=tip_serviciu,
                date_brute={
                    **loc,
                    "sold_curent": sold_curent,
                    "sursa_sold_curent": "suma_restPlata_facturi" if sold_curent is not None else None,
                    "de_plata_din_facturi": de_plata,
                    "numar_facturi": len(invoices),
                    "facturi_rezumat": summary,
                    "numar_inregistrari_consum": len(consumption),
                    "citire_permisa": False,
                    "fereastra_autocitire_deschisa": window_open,
                    "perioada_citire": interval,
                },
            ))
            consumuri.extend([
                ConsumUtilitate("sold_curent", sold_curent, "RON", id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                ConsumUtilitate("de_plata", de_plata, "RON", id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                ConsumUtilitate("citire_permisa", False, None, id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                ConsumUtilitate("perioada_citire", interval, None, id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                ConsumUtilitate("cod_client", _text(loc.get("Partener")) or None, None, id_cont=pod),
                ConsumUtilitate("cont_contract", _text(loc.get("cont_contract")) or None, None, id_cont=pod),
                ConsumUtilitate("pod", pod, None, id_cont=pod),
                ConsumUtilitate("distribuitor", _text(loc.get("Distribuitor")) or None, None, id_cont=pod),
            ])

            monthly_kwh: dict[str, float] = defaultdict(float)
            measurement_dates: list[date] = []
            for row in consumption:
                when = _date(row.get("dataCitire"))
                mwh = _number(row.get("consumMWH"))
                if when and mwh is not None:
                    monthly_kwh[when.strftime("%Y-%m")] += mwh * 1000
                    measurement_dates.append(when)
            if measurement_dates:
                latest_month_date = max(measurement_dates)
                year, month = latest_month_date.year, latest_month_date.month
                month_names: list[str] = []
                for _ in range(12):
                    month_names.append(f"{year:04d}-{month:02d}")
                    month -= 1
                    if month == 0:
                        year, month = year - 1, 12
                history = [{"luna": month_name, "valoare": round(monthly_kwh[month_name], 3)} for month_name in month_names]
                consumuri.append(ConsumUtilitate(
                    "consum_lunar", round(monthly_kwh[month_names[0]], 3), "kWh",
                    latest_month_date.isoformat(), pod, tip_utilitate, tip_serviciu,
                    {"istoric": history},
                ))
                consumuri.append(ConsumUtilitate(
                    "consum_ultimele_12_luni", round(sum(monthly_kwh[m] for m in month_names), 3),
                    "kWh", id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu,
                    date_brute={"istoric": history},
                ))

            if invoices:
                last = invoices[0]
                consumuri.extend([
                    ConsumUtilitate("valoare_ultima_factura", _invoice_amount(last), "RON", id_cont=pod),
                    ConsumUtilitate("id_ultima_factura", _text(last.get("OPBEL")) or None, None, id_cont=pod),
                    ConsumUtilitate("data_ultima_factura", _iso_date(last.get("BLDAT")), None, id_cont=pod),
                    ConsumUtilitate("urmatoarea_scadenta", _iso_date(last.get("FAEDN")), None, id_cont=pod),
                    ConsumUtilitate("numar_facturi", len(invoices), None, id_cont=pod),
                ])

            for invoice in invoices:
                if not (invoice_id := _text(invoice.get("OPBEL"))):
                    continue
                amount = _invoice_amount(invoice)
                rest = _invoice_rest(invoice)
                status = "neplatita" if rest is not None and rest > 0 else (
                    "platita" if rest == 0 and amount is not None and amount > 0 else None
                )
                facturi.append(FacturaUtilitate(
                    id_factura=invoice_id,
                    titlu=f"Factura {invoice_id}",
                    valoare=amount,
                    moneda=_text(invoice.get("WAERS")) or "RON",
                    data_emitere=_date(invoice.get("BLDAT")),
                    data_scadenta=_date(invoice.get("FAEDN")),
                    stare=status,
                    id_cont=pod,
                    id_contract=_text(invoice.get("vertrag")) or contract or None,
                    tip_utilitate=tip_utilitate,
                    tip_serviciu=tip_serviciu,
                    date_brute={
                        "exista_pdf": bool(invoice.get("existaPDF")),
                        "sursa": "premier_energy",
                        "rest_plata": rest,
                    },
                ))

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={"portal_url": PORTAL_URL},
        )
