"""Client asincron pentru datele YellowGrid, fara operatii asupra echipamentelor."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import re
import time
from typing import Any
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare

API_ROOT = "https://prod-ro-api.yellowgrid.ro"
LOGIN_PATH = "/platform-authentication/v1/api/mobile/public/user/login"
REFRESH_PATH = "/platform-authentication/v1/api/mobile/public/refresh-token"
PLANTS_PATH = "/customers/api/plants/plants"

# Configurarea clientului este distribuita separat de sursele publice.
CONFIG_URL = "https://utilitati-yellowgrid-config.marius-onitiu.workers.dev/v1/yellowgrid/config"
CONFIG_CACHE_SECONDS = 6 * 60 * 60
CONFIG_RESPONSE_BYTES = 4096
CONFIG_STAGE = "/_configurare_client"


def _diagnostic_stage(path: str) -> str:
    if path == CONFIG_STAGE:
        return "configurare_client"
    if path == LOGIN_PATH:
        return "login"
    if path == REFRESH_PATH:
        return "refresh"
    if path.startswith(PLANTS_PATH + "/"):
        return "instalatii"
    if "/telemetry-v2" in path:
        return "telemetrie"
    if "/deviceenergyflow/" in path:
        return "fluxuri"
    if "/earnings-v2/" in path:
        return "castiguri"
    if path.startswith("/billing/api/wallet/"):
        return "portofel"
    return "api"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOKEN_ADVANCE_SECONDS = 90


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None


def _claims(jwt: str) -> dict[str, Any]:
    try:
        data = jwt.split(".")
        if len(data) != 3:
            return {}
        part = data[1]
        payload = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        decoded = json.loads(payload)
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError, UnicodeError):
        return {}


def normalizare_telefon_yellowgrid(value: Any) -> str | None:
    """Pastreaza zero-ul national cerut de API: mobileNumber = 07xxxxxxxx."""
    if not isinstance(value, str):
        return None
    number = re.sub(r"[\s().-]", "", value.strip())
    if number.startswith("+40"):
        number = "0" + number[3:]
    elif number.startswith("0040"):
        number = "0" + number[4:]
    elif number.startswith("40"):
        number = "0" + number[2:]
    elif re.fullmatch(r"7[0-9]{8}", number):
        number = "0" + number
    return number if re.fullmatch(r"07[0-9]{8}", number) else None


def numar_api(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def interval_zi_romania(acum: datetime | None = None) -> tuple[str, str]:
    tz = ZoneInfo("Europe/Bucharest")
    local = (acum or datetime.now(tz)).astimezone(tz)
    inceput = local.replace(hour=0, minute=0, second=0, microsecond=0)
    sfarsit = (inceput + timedelta(days=1)).astimezone(timezone.utc) - timedelta(milliseconds=1)

    def serializare(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return serializare(inceput), serializare(sfarsit)


def normalizare_telemetrie(date: dict[str, Any]) -> dict[str, Any]:
    """Pastreaza numai masuratori confirmate; nicio valoare lipsa nu devine zero."""
    campuri = {
        "solar_w": "solarPower",
        "production_w": "productionPower",
        "load_w": "loadPower",
        "grid_signed_w": "meterPower",
        "battery_signed_w": "batteryPower",
        "battery_soc_pct": "batterySoc",
        "ac_coupling_w": "producedAcCouplingPower",
    }
    rezultat = {key: numar_api(date.get(field)) for key, field in campuri.items()}
    import_brut = numar_api(date.get("meterImportPower"))
    export_brut = numar_api(date.get("meterExportPower"))
    baterie = rezultat["battery_signed_w"]
    rezultat["grid_import_w"] = abs(import_brut) if import_brut is not None else None
    rezultat["grid_export_w"] = abs(export_brut) if export_brut is not None else None
    rezultat["battery_charging_w"] = max(-baterie, 0.0) if baterie is not None else None
    rezultat["battery_discharging_w"] = max(baterie, 0.0) if baterie is not None else None
    rezultat["state"] = str(date.get("deviceState")) if date.get("deviceState") is not None else None
    rezultat["updated_at"] = str(date.get("timeStamp")) if date.get("timeStamp") is not None else None
    return rezultat


FLUXURI = {
    "solar_to_load_kwh": "productionToConsumption",
    "solar_to_battery_kwh": "productionToBattery",
    "solar_to_grid_kwh": "productionToGrid",
    "solar_conversion_loss_kwh": "productionToConversionLoss",
    "load_from_battery_kwh": "consumptionFromBattery",
    "load_from_grid_kwh": "consumptionFromGrid",
    "grid_to_battery_kwh": "gridToBattery",
    "battery_to_grid_kwh": "batteryToGrid",
}


def normalizare_fluxuri(date: dict[str, Any]) -> dict[str, float | None]:
    flows = date.get("flows")
    if date.get("unit") != "kWh" or not isinstance(flows, dict):
        raise EroareParsare("YellowGrid: formatul fluxurilor energetice s-a schimbat")
    return {key: numar_api(flows.get(api_field)) for key, api_field in FLUXURI.items()}


class SesiuneYellowGrid:
    """Sesiune JWT cu refresh rotativ, tinuta numai in memorie."""

    def __init__(self, sesiune: aiohttp.ClientSession, *, telefon: str, parola: str,
                 cod_tara: str) -> None:
        self._sesiune = sesiune
        self._telefon = normalizare_telefon_yellowgrid(telefon)
        if self._telefon is None:
            raise EroareAutentificare("YellowGrid: numarul trebuie sa fie 07xxxxxxxx")
        self._parola = parola
        self._cod_tara = cod_tara.strip()
        if not all((self._telefon, self._parola, self._cod_tara)):
            raise EroareAutentificare("YellowGrid: date de conectare incomplete")
        self._access: str | None = None
        self._refresh: str | None = None
        self._valid_until = 0.0
        self._customer_id: str | None = None
        self._auth_lock = asyncio.Lock()
        self._client_key_lock = asyncio.Lock()
        self._client_key: str | None = None
        self._client_key_expires = 0.0
        self._diagnostic_events: list[dict[str, Any]] = []

    @property
    def istoric_diagnostic(self) -> list[dict[str, Any]]:
        """Metadate sigure; nu contin URL-uri, identificatori sau credentiale."""
        return [dict(item) for item in self._diagnostic_events]

    def _note_diagnostic(self, *, path: str, method: str, result: str,
                         started: float, http: int | None = None) -> None:
        event: dict[str, Any] = {
            "etapa": _diagnostic_stage(path),
            "metoda": method,
            "rezultat": result,
            "durata_ms": max(0, int((time.perf_counter() - started) * 1000)),
        }
        if http is not None:
            event["http"] = http
        self._diagnostic_events.append(event)
        if len(self._diagnostic_events) > 16:
            del self._diagnostic_events[:-16]

    @property
    def customer_id(self) -> str | None:
        return self._customer_id

    async def _obtine_cheie_client(self, *, reinnoieste: bool = False) -> str:
        """Obtine identificatorul clientului din configurarea HTTPS, cu cache local."""
        if self._client_key and not reinnoieste and time.monotonic() < self._client_key_expires:
            return self._client_key

        async with self._client_key_lock:
            if self._client_key and not reinnoieste and time.monotonic() < self._client_key_expires:
                return self._client_key
            started = time.perf_counter()
            try:
                async with self._sesiune.get(
                    CONFIG_URL,
                    headers={"Accept": "application/json", "Cache-Control": "no-store"},
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    if status != 200:
                        self._note_diagnostic(
                            path=CONFIG_STAGE, method="GET", result="http_error",
                            started=started, http=status,
                        )
                        raise EroareConectare(f"YellowGrid: configurarea clientului nu este disponibila (HTTP {status})")
                    if response.content_length is not None and response.content_length > CONFIG_RESPONSE_BYTES:
                        self._note_diagnostic(
                            path=CONFIG_STAGE, method="GET", result="response_too_large",
                            started=started, http=200,
                        )
                        raise EroareParsare("YellowGrid: configurare client prea mare")
                    raw = await response.content.read(CONFIG_RESPONSE_BYTES + 1)
                    if len(raw) > CONFIG_RESPONSE_BYTES:
                        self._note_diagnostic(
                            path=CONFIG_STAGE, method="GET", result="response_too_large",
                            started=started, http=200,
                        )
                        raise EroareParsare("YellowGrid: configurare client prea mare")
            except asyncio.TimeoutError as err:
                self._note_diagnostic(
                    path=CONFIG_STAGE, method="GET", result="timeout", started=started,
                )
                if self._client_key and not reinnoieste:
                    return self._client_key
                raise EroareConectare("YellowGrid: timpul de asteptare pentru configurare a expirat") from err
            except aiohttp.ClientError as err:
                self._note_diagnostic(
                    path=CONFIG_STAGE, method="GET", result="network_error", started=started,
                )
                if self._client_key and not reinnoieste:
                    return self._client_key
                raise EroareConectare("YellowGrid: serviciul de configurare este inaccesibil") from err
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, ValueError) as err:
                self._note_diagnostic(
                    path=CONFIG_STAGE, method="GET", result="json_invalid", started=started, http=200,
                )
                raise EroareParsare("YellowGrid: raspuns invalid al configurarii") from err

            key = payload.get("api_key") if isinstance(payload, dict) and payload.get("provider") == "yellowgrid" else None
            if (not isinstance(key, str) or not (12 <= len(key) <= 256)
                    or not key.isascii() or not key.isprintable()):
                self._note_diagnostic(
                    path=CONFIG_STAGE, method="GET", result="config_invalid", started=started, http=200,
                )
                raise EroareParsare("YellowGrid: identificator client indisponibil in configurare")
            self._client_key = key
            self._client_key_expires = time.monotonic() + CONFIG_CACHE_SECONDS
            self._note_diagnostic(
                path=CONFIG_STAGE, method="GET", result="ok", started=started, http=200,
            )
            return key

    async def _request(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                       auth: bool = False) -> Any:
        if not path.startswith("/") or path.startswith("//") or ".." in path:
            raise EroareParsare("YellowGrid: cale API invalida")
        if method == "POST" and path not in {LOGIN_PATH, REFRESH_PATH}:
            raise EroareParsare("YellowGrid: operatie nepermisa")
        if method not in {"GET", "POST"}:
            raise EroareParsare("YellowGrid: metoda nepermisa")

        for attempt in range(2):
            key = await self._obtine_cheie_client()
            headers = {
                "Accept": "application/json",
                "User-Agent": "okhttp/4.9.2",
                "x-apikey": key,
            }
            if auth and self._access:
                headers["Authorization"] = f"Bearer {self._access}"
            started = time.perf_counter()
            try:
                async with self._sesiune.request(
                    method, API_ROOT + path, headers=headers, json=body,
                    timeout=aiohttp.ClientTimeout(total=25), allow_redirects=False,
                ) as response:
                    status = response.status
                    if status != 200:
                        self._note_diagnostic(
                            path=path, method=method, result="http_error", started=started, http=status,
                        )
                        if status in (401, 403):
                            if path in {LOGIN_PATH, REFRESH_PATH} and attempt == 0:
                                fresh_key = await self._obtine_cheie_client(reinnoieste=True)
                                if fresh_key != key:
                                    continue
                                self._note_diagnostic(
                                    path=path, method=method, result="http_error",
                                    started=started, http=status,
                                )
                            raise EroareAutentificare(f"YellowGrid: acces respins (HTTP {status})")
                        raise EroareConectare(f"YellowGrid: raspuns HTTP {status}")
                    if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                        self._note_diagnostic(
                            path=path, method=method, result="response_too_large", started=started, http=status,
                        )
                        raise EroareParsare("YellowGrid: raspuns prea mare")
                    raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        self._note_diagnostic(
                            path=path, method=method, result="response_too_large", started=started, http=status,
                        )
                        raise EroareParsare("YellowGrid: raspuns prea mare")
            except asyncio.TimeoutError as err:
                self._note_diagnostic(path=path, method=method, result="timeout", started=started)
                raise EroareConectare("YellowGrid: timp de asteptare depasit") from err
            except aiohttp.ClientError as err:
                self._note_diagnostic(path=path, method=method, result="network_error", started=started)
                raise EroareConectare("YellowGrid: conexiune indisponibila") from err
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as err:
                self._note_diagnostic(
                    path=path, method=method, result="json_invalid", started=started, http=200,
                )
                raise EroareParsare("YellowGrid: JSON invalid") from err
            self._note_diagnostic(path=path, method=method, result="ok", started=started, http=200)
            return data
        raise EroareAutentificare("YellowGrid: autentificarea a esuat dupa reinnoirea configurarii")

    def _set_tokens(self, response: Any) -> None:
        if not isinstance(response, dict):
            raise EroareAutentificare("YellowGrid: raspuns de autentificare invalid")
        access, refresh = response.get("access_token"), response.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise EroareAutentificare("YellowGrid: tokenuri incomplete")
        claims = _claims(access)
        customer = _uuid(claims.get("customerUuid"))
        if not customer or (self._customer_id is not None and self._customer_id != customer):
            raise EroareAutentificare("YellowGrid: identificator de cont invalid")
        self._customer_id = customer
        self._access = access
        self._refresh = refresh
        expiry = claims.get("exp")
        self._valid_until = float(expiry) if isinstance(expiry, (int, float)) else time.time() + 120

    async def authenticate(self) -> None:
        if self._access and time.time() + TOKEN_ADVANCE_SECONDS < self._valid_until:
            return
        async with self._auth_lock:
            if self._access and time.time() + TOKEN_ADVANCE_SECONDS < self._valid_until:
                return
            if self._refresh:
                try:
                    refreshed = await self._request(REFRESH_PATH, method="POST", body={"id_token": self._refresh})
                    self._set_tokens(refreshed)
                    return
                except EroareAutentificare:
                    self._refresh = None
                    self._access = None
            response = await self._request(
                LOGIN_PATH, method="POST",
                body={"countryCode": self._cod_tara, "mobileNumber": self._telefon, "passcode": self._parola},
            )
            self._set_tokens(response)

    async def get(self, path: str) -> Any:
        await self.authenticate()
        for attempt in range(2):
            try:
                return await self._request(path, auth=True)
            except EroareAutentificare:
                if attempt:
                    raise
                self._access = None
                await self.authenticate()
        raise EroareAutentificare("YellowGrid: autentificare imposibila")

    async def get_plants(self) -> list[dict[str, Any]]:
        await self.authenticate()
        if not self._customer_id:
            raise EroareAutentificare("YellowGrid: cont neidentificat")
        response = await self.get(f"{PLANTS_PATH}/{self._customer_id}")
        if not isinstance(response, list):
            raise EroareParsare("YellowGrid: lista instalatiilor invalida")
        return [item for item in response if isinstance(item, dict)]

    @staticmethod
    def _device(value: Any) -> str:
        normalized = _uuid(value)
        if not normalized:
            raise EroareParsare("YellowGrid: instalatie fara identificator valid")
        return normalized

    async def get_telemetry(self, device_id: str) -> dict[str, Any]:
        response = await self.get(f"/core-platform/api/deviceoverview/{self._device(device_id)}/telemetry-v2")
        if not isinstance(response, dict):
            raise EroareParsare("YellowGrid: telemetrie invalida")
        return response

    async def get_flows(self, device_id: str, start: str, end: str) -> dict[str, Any]:
        path = (f"/core-platform/api/deviceenergyflow/{self._device(device_id)}"
                f"?startTime={quote(start, safe='')}&endTime={quote(end, safe='')}")
        response = await self.get(path)
        if not isinstance(response, dict):
            raise EroareParsare("YellowGrid: fluxuri invalide")
        return response

    async def get_earnings(self, device_id: str, start: str, end: str) -> dict[str, Any]:
        path = (f"/core-platform/api/deviceoverview/{self._device(device_id)}/earnings-v2/"
                f"{quote(start, safe='')}/{quote(end, safe='')}/day")
        response = await self.get(path)
        if not isinstance(response, dict):
            raise EroareParsare("YellowGrid: castiguri invalide")
        return response

    async def get_wallet(self) -> dict[str, Any]:
        await self.authenticate()
        if not self._customer_id:
            raise EroareAutentificare("YellowGrid: cont neidentificat")
        response = await self.get(f"/billing/api/wallet/{self._customer_id}")
        if not isinstance(response, dict):
            raise EroareParsare("YellowGrid: portofel invalid")
        return response
