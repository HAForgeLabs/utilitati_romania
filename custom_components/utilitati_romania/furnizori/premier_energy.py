from __future__ import annotations

import asyncio
import base64
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import aiohttp
from curl_cffi import requests as curl_requests

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

AUTH_BASE = "https://login.premierenergy.ro"
AUTH_URL = f"{AUTH_BASE}/login.premierenergy.ro/oauth2/v2.0/authorize"
API_BASE = "https://peremierenergy-portalclient.azurewebsites.net/api"
PORTAL_URL = "https://my.premierenergy.ro"
CLIENT_ID = "304aaac1-c88a-4775-be6b-ac23ce30cab5"
POLICY = "B2C_1A_SIGNUP_SIGNIN"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
TOKEN_MARGIN_SECONDS = 120


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _iso_date(value: Any) -> str | None:
    parsed = _date(value)
    return parsed.isoformat() if parsed else None


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return float(data.get("exp") or 0)
    except (ValueError, KeyError, IndexError, json.JSONDecodeError):
        return 0.0


def _tip_serviciu(loc: dict[str, Any]) -> str:
    distributor = _text(loc.get("Distribuitor")).upper()
    pod = _text(loc.get("PoD")).upper()
    if any(token in distributor for token in ("GAZ", "DGSR", "DIST GAZ")) or pod.startswith("DGS"):
        return "gaz"
    if any(token in distributor for token in ("ELECT", "DEO", "DISTRIBUTIE OLTENIA", "RETELE")):
        return "curent"
    return "energie"


def _tip_utilitate(tip_serviciu: str) -> str:
    if tip_serviciu == "gaz":
        return "gaz"
    if tip_serviciu == "curent":
        return "energie electrică"
    return "energie"


class ClientApiPremierEnergy:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._id_token: str | None = None
        self._token_exp = 0.0
        self._login_lock = asyncio.Lock()

    def _token_valid(self) -> bool:
        return bool(self._id_token) and time.time() < self._token_exp - TOKEN_MARGIN_SECONDS

    async def _text_response(self, response: aiohttp.ClientResponse) -> str:
        return await response.text(errors="replace")

    @staticmethod
    def _settings_from_html(html: str) -> dict[str, Any]:
        match = re.search(r"var\s+SETTINGS\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
        if not match:
            raise EroareParsare("Premier Energy: configurația paginii de autentificare nu a fost găsită")
        try:
            settings = json.loads(match.group(1))
        except json.JSONDecodeError as err:
            raise EroareParsare("Premier Energy: configurație de autentificare invalidă") from err
        if not settings.get("csrf") or not settings.get("transId"):
            raise EroareParsare("Premier Energy: lipsesc parametrii de autentificare")
        return settings

    def _login_curl(self) -> tuple[str, float]:
        """Autentifică pe transportul Chromium, într-un fir separat de event loop."""
        params = {
            "p": POLICY,
            "redirect_uri": PORTAL_URL,
            "client_id": CLIENT_ID,
            "nonce": "defaultNonce",
            "scope": "openid",
            "response_type": "id_token",
            "prompt": "login",
        }
        try:
            with curl_requests.Session(impersonate="chrome") as session:
                auth = session.get(AUTH_URL, params=params, timeout=40)
                _LOGGER.debug("Premier Energy: authorize HTTP %s", auth.status_code)
                if auth.status_code != 200:
                    raise EroareConectare(
                        f"Premier Energy: inițializarea autentificării a eșuat (HTTP {auth.status_code})"
                    )
                settings = self._settings_from_html(auth.text)
                csrf = _text(settings.get("csrf"))
                transaction = _text(settings.get("transId"))
                hosts = settings.get("hosts") if isinstance(settings.get("hosts"), dict) else {}
                tenant = _text(hosts.get("tenant")) or "/login.premierenergy.ro/B2C_1A_signup_signin"
                policy = _text(hosts.get("policy")) or "B2C_1A_signup_signin"
                if not tenant.startswith("/login.premierenergy.ro/"):
                    raise EroareParsare("Premier Energy: cale de autentificare neașteptată")
                headers = {
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": AUTH_BASE,
                    "Referer": auth.url,
                    "X-CSRF-TOKEN": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }
                login = session.post(
                    f"{AUTH_BASE}{tenant}/SelfAsserted",
                    params={"tx": transaction, "p": policy},
                    data={"request_type": "RESPONSE", "signInName": self._utilizator, "password": self._parola},
                    headers=headers,
                    timeout=40,
                )
                _LOGGER.debug("Premier Energy: SelfAsserted HTTP %s", login.status_code)
                if login.status_code in (401, 403):
                    raise EroareAutentificare("Premier Energy: date de autentificare respinse")
                if login.status_code != 200:
                    raise EroareConectare(f"Premier Energy: autentificare refuzată (HTTP {login.status_code})")
                try:
                    parsed = login.json()
                except (ValueError, TypeError) as err:
                    raise EroareParsare("Premier Energy: răspuns de autentificare invalid") from err
                if not isinstance(parsed, dict) or str(parsed.get("status")) != "200":
                    raise EroareAutentificare("Premier Energy: autentificarea nu a fost acceptată")
                confirm = session.get(
                    f"{AUTH_BASE}{tenant}/api/CombinedSigninAndSignup/confirmed",
                    params={
                        "rememberMe": "false",
                        "csrf_token": csrf,
                        "tx": transaction,
                        "p": policy,
                    },
                    headers={"Referer": auth.url},
                    allow_redirects=False,
                    timeout=40,
                )
                location = confirm.headers.get("location", "")
                token = _text((parse_qs(urlsplit(location).fragment).get("id_token") or [""])[0])
                if confirm.status_code not in (301, 302, 303, 307, 308) or not token:
                    raise EroareAutentificare("Premier Energy: tokenul de sesiune nu a fost emis")
                _LOGGER.debug("Premier Energy: autentificare finalizată")
                return token, _jwt_exp(token) or (time.time() + 3000)
        except (EroareAutentificare, EroareConectare, EroareParsare):
            raise
        except Exception as err:
            _LOGGER.warning("Premier Energy: eroare de transport la autentificare (%s)", type(err).__name__)
            raise EroareConectare("Premier Energy: conexiunea de autentificare a eșuat") from err

    async def async_login(self, *, force: bool = False) -> None:
        if not force and self._token_valid():
            return
        async with self._login_lock:
            if not force and self._token_valid():
                return
            token, expiry = await asyncio.to_thread(self._login_curl)
            self._id_token = token
            self._token_exp = expiry

    async def _api_get(self, endpoint: str, *, params: dict[str, str] | None = None, retry_auth: bool = True) -> Any:
        await self.async_login()
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": PORTAL_URL,
            "Referer": f"{PORTAL_URL}/",
            "premier-auth": f"Bearer {self._id_token}",
            "x-pe-request-id": str(uuid4()),
        }
        try:
            async with self._sesiune.get(
                f"{API_BASE}/{endpoint.lstrip('/')}",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                body = await self._text_response(response)
                _LOGGER.debug("Premier Energy API %s HTTP %s", endpoint, response.status)
                if response.status in (401, 403):
                    if retry_auth:
                        self._id_token = None
                        self._token_exp = 0
                        await self.async_login(force=True)
                        return await self._api_get(endpoint, params=params, retry_auth=False)
                    raise EroareAutentificare("Sesiunea Premier Energy a expirat")
                if response.status >= 400:
                    raise EroareConectare(f"Premier Energy API a returnat HTTP {response.status}")
        except (EroareAutentificare, EroareConectare):
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Premier Energy diagnostic: etapa 5/5 - API %s eroare transport (%s)", endpoint.split("?", 1)[0], type(err).__name__)
            raise EroareConectare("Premier Energy API nu răspunde") from err

        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            raise EroareParsare("Premier Energy API a returnat date invalide") from err

    async def async_transmite_index(self, pod: str, contract: str, index: int) -> str:
        """Transmite o singură citire; nu presupune succesul doar din HTTP 200."""
        pod = _text(pod)
        contract = _text(contract)
        if not re.fullmatch(r"[A-Za-z0-9]{5,40}", pod):
            raise EroareParsare("Premier Energy: loc de consum invalid")
        if not re.fullmatch(r"[0-9]{1,24}", contract):
            raise EroareParsare("Premier Energy: contract de furnizare invalid")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index > 99999999:
            raise EroareParsare("Premier Energy: indexul trebuie să fie întreg și pozitiv sau zero")
        await self.async_login()
        payload = {"index": str(index), "locConsum": pod, "sursa": "portalClienti", "contract": contract}
        for attempt in range(2):
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Origin": PORTAL_URL,
                "Referer": f"{PORTAL_URL}/transmitere-index",
                "premier-auth": f"Bearer {self._id_token}",
                "x-pe-request-id": str(uuid4()),
            }
            try:
                async with self._sesiune.post(
                    f"{API_BASE}/index", json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    body = (await response.text(errors="replace")).strip()
                    if response.status in (401, 403) and attempt == 0:
                        self._id_token = None
                        self._token_exp = 0
                        await self.async_login(force=True)
                        continue
                    if response.status in (401, 403):
                        raise EroareAutentificare("Premier Energy: autentificarea a expirat")
                    if response.status != 200:
                        raise EroareConectare(f"Premier Energy: trimitere index HTTP {response.status}")
            except (EroareAutentificare, EroareConectare):
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EroareConectare(
                    "Premier Energy: rezultatul transmiterii este incert; verificați portalul înainte de a reîncerca"
                ) from err
            match = re.search(r"indexul a fost înregistrat la poziția\s+(\d+)", body, re.IGNORECASE)
            if not match:
                raise EroareParsare(
                    "Premier Energy: serverul nu a confirmat înregistrarea indexului; verificați portalul"
                )
            return match.group(1)
        raise EroareAutentificare("Premier Energy: nu s-a putut reînnoi sesiunea")

    async def async_locuri_consum(self) -> list[dict[str, Any]]:
        data = await self._api_get("locConsum", params={"cuAdresa": "true"})
        return data if isinstance(data, list) else []

    async def async_facturi(self) -> list[dict[str, Any]]:
        data = await self._api_get("facturi", params={"Versiunea": "1"})
        return data if isinstance(data, list) else []

    async def async_consum(self) -> list[dict[str, Any]]:
        data = await self._api_get("consum")
        return data if isinstance(data, list) else []

    async def async_descarca_factura(self, identificator: str) -> bytes:
        """Returnează PDF-ul autentic al unei facturi, fără persistența documentului."""
        identificator = _text(identificator)
        if not re.fullmatch(r"[0-9]{1,32}", identificator):
            raise EroareParsare("Premier Energy: identificator de factură invalid")
        await self.async_login()
        for retry in range(2):
            headers = {
                "Accept": "text/plain, application/pdf, */*",
                "Origin": PORTAL_URL,
                "Referer": f"{PORTAL_URL}/facturi-plati",
                "premier-auth": f"Bearer {self._id_token}",
                "x-pe-request-id": str(uuid4()),
                "User-Agent": USER_AGENT,
            }
            try:
                async with self._sesiune.get(
                    f"{API_BASE}/facturi/{identificator}/pdf",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as response:
                    if response.status in (401, 403) and retry == 0:
                        self._id_token = None
                        self._token_exp = 0
                        await self.async_login(force=True)
                        continue
                    if response.status in (401, 403):
                        raise EroareAutentificare("Premier Energy: sesiune expirată la descărcarea facturii")
                    if response.status != 200:
                        raise EroareConectare(f"Premier Energy: descărcare PDF HTTP {response.status}")
                    encoded = await response.read()
            except (EroareAutentificare, EroareConectare):
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EroareConectare("Premier Energy: conexiunea pentru PDF a eșuat") from err
            if len(encoded) > 16 * 1024 * 1024:
                raise EroareParsare("Premier Energy: documentul depășește limita de dimensiune")
            if encoded.startswith(b"%PDF-"):
                pdf = encoded
            else:
                try:
                    pdf = base64.b64decode(encoded.strip(), validate=True)
                except (ValueError, base64.binascii.Error) as err:
                    raise EroareParsare("Premier Energy: codificare PDF invalidă") from err
            if not pdf.startswith(b"%PDF-"):
                raise EroareParsare("Premier Energy: răspunsul nu conține un PDF valid")
            return pdf
        raise EroareAutentificare("Premier Energy: autentificarea pentru PDF a eșuat")

    async def async_sold(self) -> list[dict[str, Any]]:
        data = await self._api_get("sold", params={"Versiunea": "1"})
        return data if isinstance(data, list) else []


class ClientFurnizorPremierEnergy(ClientFurnizor):
    cheie_furnizor = "premier_energy"
    nume_prietenos = "Premier Energy"

    def __init__(self, *, sesiune: aiohttp.ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiPremierEnergy(sesiune, utilizator, parola)

    async def async_descarca_factura(self, identificator: str) -> bytes:
        return await self.api.async_descarca_factura(identificator)

    async def async_transmite_index(self, id_cont: str, index: int) -> str:
        """Validează locul, contractul și fereastra direct din datele curente."""
        locuri = await self.api.async_locuri_consum()
        loc = next((item for item in locuri if _text(item.get("PoD")) == _text(id_cont)), None)
        if not loc:
            raise EroareParsare("Premier Energy: locul de consum nu a fost găsit")
        contract = _text(loc.get("CONTR_FURN"))
        start = _date(loc.get("perioada_citire_start"))
        end = _date(loc.get("perioada_citire_sfarsit"))
        azi = datetime.now(ZoneInfo("Europe/Bucharest")).date()
        if not start or not end or not start <= azi <= end:
            raise EroareConectare("Premier Energy: suntem în afara perioadei de autocitire")
        if not contract:
            raise EroareParsare("Premier Energy: lipsește contractul de furnizare")
        return await self.api.async_transmite_index(_text(id_cont), contract, index)

    async def async_testeaza_conexiunea(self) -> str:
        locuri = await self.api.async_locuri_consum()
        if not locuri:
            raise EroareConectare("Premier Energy: contul nu conține locuri de consum")
        partner = _text(locuri[0].get("Partener"))
        return partner or self.utilizator.strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        locuri = await self.api.async_locuri_consum()
        facturi_raw = await self.api.async_facturi()
        consum_raw = await self.api.async_consum()
        sold_raw = await self.api.async_sold()

        sold_by_pod: dict[str, float] = {}
        for partner in sold_raw:
            for loc in partner.get("locuriConsum") or []:
                pod = _text(loc.get("pod"))
                valori = loc.get("sold") or []
                total = sum(v for item in valori if (v := _float(item.get("sold"))) is not None)
                if pod:
                    sold_by_pod[pod] = round(total, 2)

        consum_by_pod: dict[str, list[dict[str, Any]]] = {}
        for grup in consum_raw:
            pod = _text(grup.get("codlocConsum"))
            rows: list[dict[str, Any]] = []
            for contract in grup.get("contracte") or []:
                for item in contract.get("consum") or []:
                    if isinstance(item, dict):
                        rows.append({**item, "nrContract": contract.get("nrContract")})
            rows.sort(key=lambda x: _text(x.get("dataCitire")), reverse=True)
            if pod:
                consum_by_pod[pod] = rows

        # Asociază facturile după POD, contract sau partener, fără asocieri ambigue.
        indexes: dict[str, dict[str, set[str]]] = {
            "pod": defaultdict(set), "contract": defaultdict(set), "partner": defaultdict(set)
        }
        for location in locuri:
            location_pod = _text(location.get("PoD")).upper()
            if not location_pod:
                continue
            indexes["pod"][location_pod].add(location_pod)
            for key in ("CONTR_FURN", "cont_contract"):
                value = _text(location.get(key)).upper()
                if value:
                    indexes["contract"][value].add(location_pod)
            partner = _text(location.get("Partener")).upper()
            if partner:
                indexes["partner"][partner].add(location_pod)

        invoices_by_pod: dict[str, list[dict[str, Any]]] = defaultdict(list)
        unmatched_invoices = 0
        for invoice in facturi_raw:
            if not isinstance(invoice, dict):
                continue
            matched = set()
            # Prioritizează identificatori cu granularitate mai mare.
            for kind, keys in (
                ("pod", ("POD", "PoD", "pod")),
                ("contract", ("vertrag", "VERTRAG", "CONTR_FURN", "VKONT")),
                ("partner", ("PARTNER", "Partener", "partner")),
            ):
                matches = set()
                for key in keys:
                    value = _text(invoice.get(key)).upper()
                    if value:
                        matches.update(indexes[kind].get(value, set()))
                if matches:
                    matched = matches
                    break
            if len(matched) == 1:
                invoices_by_pod[next(iter(matched))].append(invoice)
            else:
                unmatched_invoices += 1
        if unmatched_invoices:
            _LOGGER.warning(
                "Premier Energy: %s facturi neasociate sau ambigue; verificați identificatorii din API",
                unmatched_invoices,
            )
        for rows in invoices_by_pod.values():
            rows.sort(key=lambda x: _text(x.get("BLDAT")), reverse=True)

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []

        for loc in locuri:
            pod = _text(loc.get("PoD"))
            if not pod:
                continue
            tip_serviciu = _tip_serviciu(loc)
            tip_utilitate = _tip_utilitate(tip_serviciu)
            contract = _text(loc.get("CONTR_FURN")) or _text(loc.get("cont_contract"))
            adresa = _text(loc.get("Adresa")) or None
            nume = adresa or _text(loc.get("Denumire")) or pod
            sold = sold_by_pod.get(pod)
            rows_consum = consum_by_pod.get(pod, [])
            rows_facturi = invoices_by_pod.get(pod.upper(), [])

            start_citire = _date(loc.get("perioada_citire_start"))
            sfarsit_citire = _date(loc.get("perioada_citire_sfarsit"))
            azi = datetime.now(ZoneInfo("Europe/Bucharest")).date()
            citire_permisa = bool(start_citire and sfarsit_citire and start_citire <= azi <= sfarsit_citire)
            perioada_citire = (
                f"{start_citire.strftime('%d.%m.%Y')} - {sfarsit_citire.strftime('%d.%m.%Y')}"
                if start_citire and sfarsit_citire
                else None
            )

            raw_cont = {
                **loc,
                "sold_curent": sold,
                "numar_facturi": len(rows_facturi),
                "facturi_rezumat": [{"id": _text(f.get("OPBEL")), "valoare": _float(f.get("TOTAL_AMNT")), "emitere": _iso_date(f.get("BLDAT")), "scadenta": _iso_date(f.get("FAEDN"))} for f in rows_facturi[:12]],
                "numar_inregistrari_consum": len(rows_consum),
                "citire_permisa": citire_permisa,
                "perioada_citire": perioada_citire,
            }
            conturi.append(
                ContUtilitate(
                    id_cont=pod,
                    nume=nume,
                    tip_cont="loc_consum",
                    id_contract=contract or None,
                    adresa=adresa,
                    stare="activ" if _text(loc.get("CONTR_FURN")) else None,
                    tip_utilitate=tip_utilitate,
                    tip_serviciu=tip_serviciu,
                    date_brute=raw_cont,
                )
            )

            de_plata = max(sold or 0.0, 0.0) if sold is not None else None
            consumuri.extend(
                [
                    ConsumUtilitate("sold_curent", sold, "RON", id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                    ConsumUtilitate("de_plata", de_plata, "RON", id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                    ConsumUtilitate("citire_permisa", citire_permisa, None, id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                    ConsumUtilitate("perioada_citire", perioada_citire, None, id_cont=pod, tip_utilitate=tip_utilitate, tip_serviciu=tip_serviciu),
                    ConsumUtilitate("cod_client", _text(loc.get("Partener")) or None, None, id_cont=pod),
                    ConsumUtilitate("cont_contract", _text(loc.get("cont_contract")) or None, None, id_cont=pod),
                    ConsumUtilitate("pod", pod, None, id_cont=pod),
                    ConsumUtilitate("distribuitor", _text(loc.get("Distribuitor")) or None, None, id_cont=pod),
                ]
            )

            valori_kwh: list[tuple[date, float]] = []
            for row in rows_consum:
                data_row = _date(row.get("dataCitire"))
                mwh = _float(row.get("consumMWH"))
                if data_row and mwh is not None:
                    valori_kwh.append((data_row, mwh * 1000.0))
            if valori_kwh:
                # Fiecare lună poate avea mai multe înregistrări (inclusiv regularizări).
                # Totalul este calculat pe 12 luni calendaristice, nu 12 evenimente.
                monthly_kwh: dict[str, float] = defaultdict(float)
                for measurement_date, measurement_value in valori_kwh:
                    monthly_kwh[measurement_date.strftime("%Y-%m")] += measurement_value
                latest_observed = max(d for d, _ in valori_kwh)
                months = []
                year, month = latest_observed.year, latest_observed.month
                for _ in range(12):
                    months.append(f"{year:04d}-{month:02d}")
                    month -= 1
                    if month == 0:
                        year -= 1
                        month = 12
                history = [
                    {"luna": month, "valoare": round(monthly_kwh[month], 3)}
                    for month in months
                ]
                latest_month = months[0]
                latest_month_date = max(d for d, _ in valori_kwh if d.strftime("%Y-%m") == latest_month)
                consumuri.append(ConsumUtilitate(
                    "consum_lunar", round(monthly_kwh[latest_month], 3), "kWh",
                    latest_month_date.isoformat(), pod, tip_utilitate, tip_serviciu,
                    {"istoric": history},
                ))
                consumuri.append(ConsumUtilitate(
                    "consum_ultimele_12_luni", round(sum(monthly_kwh[m] for m in months), 3),
                    "kWh", id_cont=pod, tip_utilitate=tip_utilitate,
                    tip_serviciu=tip_serviciu, date_brute={"istoric": history},
                ))

            if rows_facturi:
                latest_invoice = rows_facturi[0]
                consumuri.extend(
                    [
                        ConsumUtilitate("valoare_ultima_factura", _float(latest_invoice.get("TOTAL_AMNT")), "RON", id_cont=pod),
                        ConsumUtilitate("id_ultima_factura", _text(latest_invoice.get("OPBEL")) or None, None, id_cont=pod),
                        ConsumUtilitate("data_ultima_factura", _iso_date(latest_invoice.get("BLDAT")), None, id_cont=pod),
                        ConsumUtilitate("urmatoarea_scadenta", _iso_date(latest_invoice.get("FAEDN")), None, id_cont=pod),
                        ConsumUtilitate("numar_facturi", len(rows_facturi), None, id_cont=pod),
                    ]
                )

            for inv in rows_facturi:
                factura_id = _text(inv.get("OPBEL"))
                if not factura_id:
                    continue
                amount = _float(inv.get("TOTAL_AMNT"))
                facturi.append(
                    FacturaUtilitate(
                        id_factura=factura_id,
                        titlu=f"Factura {factura_id}",
                        valoare=amount,
                        moneda="RON",
                        data_emitere=_date(inv.get("BLDAT")),
                        data_scadenta=_date(inv.get("FAEDN")),
                        stare=None,
                        id_cont=pod,
                        id_contract=_text(inv.get("vertrag")) or contract or None,
                        tip_utilitate=tip_utilitate,
                        tip_serviciu=tip_serviciu,
                        date_brute={"exista_pdf": str(inv.get("existaPDF")).strip().lower() in ("true", "1", "x", "yes"), "sursa": "premier_energy"},
                    )
                )

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={"portal_url": PORTAL_URL},
        )
