from __future__ import annotations

import html
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://contulmeu.reteleelectrice.ro"
URL_LOGIN_FORM = f"{URL_BAZA}/PEDRO_SiteLogin?ec=302&startURL=%2Fs%2F"
URL_LOGIN = (
    f"{URL_BAZA}/PEDRO_SiteLogin?startURL=%2Fs%2F"
    f"&refURL=https%3A%2F%2Fcontulmeu.reteleelectrice.ro%2Fs%2F"
)
URL_PORTAL = f"{URL_BAZA}/s/"
URL_AURA = f"{URL_BAZA}/s/sfsites/aura"
URL_DETALII_POD = f"{URL_BAZA}/PED_ProxyCallWSAsync_Curve_VF"
URL_CITIRI = f"{URL_BAZA}/PED_ProxyCallWSAsync_SmartMeter_Vf"
URL_CONTOR = f"{URL_BAZA}/PED_ProxyCallWSAsynSmartMeterCurrentData"

HEADERS_BROWSER = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}
TIMEOUT = aiohttp.ClientTimeout(total=35)

TIPURI_CONSUM_TOTAL = ("EA", "EAN", "EANO", "EANW")
TIPURI_CONSUM_ZONE = ("EAZ", "EAV", "EAG")
TIPURI_INJECTIE = ("EAP",)


def _text(value: Any) -> str:
    return html.unescape(re.sub(r"\s+", " ", str(value or "")).strip())


def _strip_tags(value: Any) -> str:
    return _text(re.sub(r"<[^>]+>", " ", str(value or "")))


def _number(value: Any) -> float | None:
    raw = _text(value).replace(" ", "")
    if not raw:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", raw)
    if not match:
        return None
    token = match.group(0)
    if "," in token and "." in token:
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


def _compact_number(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    rounded = round(float(value), 3)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def _date(value: Any) -> date | None:
    raw = _text(value)
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _iso_date(value: Any) -> str | None:
    parsed = _date(value)
    return parsed.isoformat() if parsed else (_text(value) or None)




def _masked_pod(value: Any) -> str:
    raw = _text(value)
    if len(raw) <= 4:
        return "****"
    return f"***{raw[-4:]}"

def _state_label(item: dict[str, Any]) -> str:
    disconnected = bool(item.get("Disconnected__c"))
    voltage_status = _text(item.get("VoltageStatus__c")).lower()
    active = item.get("Active__c")
    contract_state = _text(item.get("Contract_State__c")).lower()
    if disconnected or "deenerg" in voltage_status or contract_state in {"inactive", "closed"}:
        return "Deconectat"
    if active is False:
        return "Inactiv"
    if "energ" in voltage_status or active is True or contract_state == "active":
        return "Conectat"
    return "Necunoscut"


def _month_key(period: date) -> str:
    return period.strftime("%Y-%m")


def _decode_aura_config(page: str) -> dict[str, Any]:
    match = re.search(
        r"(?:var\s+|window\.)?auraConfig\s*=\s*",
        page or "",
        flags=re.I,
    )
    if not match:
        return {}
    start = (page or "").find("{", match.end())
    if start < 0:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode((page or "")[start:])
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _frontdoor_url(page: str) -> str | None:
    decoded = html.unescape(page or "")
    for source, target in (
        (r"\/", "/"),
        (r"\u0026", "&"),
        (r"\u003d", "="),
        (r"\x26", "&"),
        (r"\x3d", "="),
        (r"\x2f", "/"),
        (r"\x2F", "/"),
    ):
        decoded = decoded.replace(source, target)

    patterns = (
        r"(?P<url>(?:https?://contulmeu\.reteleelectrice\.ro)?/secur/frontdoor\.jsp\?[^\"'<>\s]+)",
        r"(?:window\.)?location(?:\.href)?\s*=\s*[\"'](?P<url>[^\"']*frontdoor\.jsp[^\"']*)",
        r"(?:window\.)?location\.replace\(\s*[\"'](?P<url>[^\"']*frontdoor\.jsp[^\"']*)",
        r"(?:frontdoorUrl|frontDoorUrl|url)\s*=\s*[\"'](?P<url>[^\"']*frontdoor\.jsp[^\"']*)",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, flags=re.I)
        if not match:
            continue
        candidate = _text(match.group("url")).rstrip(";,)>")
        if candidate:
            return urljoin(URL_BAZA, candidate)
    return None


def _parse_aura_response(payload: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as err:
        raise EroareParsare("Raspuns Salesforce Aura invalid") from err
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        raise EroareParsare("Raspuns Salesforce Aura fara actiuni")
    return [item for item in actions if isinstance(item, dict)]


def _extract_async_json(page: str) -> dict[str, Any]:
    match = re.search(
        r"<span\b[^>]*id=[\"'][^\"']*asyncResponse[^\"']*[\"'][^>]*>(.*?)</span>",
        page,
        flags=re.I | re.S,
    )
    if not match:
        return {}
    raw = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as err:
        raise EroareParsare("Raspuns Visualforce invalid") from err
    return value if isinstance(value, dict) else {}


class _FormReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.global_inputs: list[dict[str, str]] = []
        self._current: dict[str, Any] | None = None

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        values = self._attrs(attrs)
        if tag_name == "form":
            self._current = {
                "id": values.get("id") or values.get("name") or "",
                "action": values.get("action") or "",
                "method": (values.get("method") or "post").lower(),
                "inputs": [],
                "controls": [],
            }
            self.forms.append(self._current)
        elif tag_name in {"input", "button", "a"}:
            control = {"tag": tag_name, **values}
            if self._current is not None:
                self._current["controls"].append(control)
                if tag_name == "input":
                    self._current["inputs"].append(values)
            elif tag_name == "input" and values.get("name"):
                self.global_inputs.append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current = None


def _forms(page: str) -> list[dict[str, Any]]:
    parser = _FormReader()
    parser.feed(page or "")
    for form in parser.forms:
        form["inputs"].extend(parser.global_inputs)
    return parser.forms


def _select_form(page: str, marker: str | None = None) -> dict[str, Any] | None:
    forms = _forms(page)
    if marker:
        marker_lower = marker.lower()
        for form in forms:
            if marker_lower in str(form.get("id") or "").lower() or marker_lower in str(form.get("action") or "").lower():
                return form
    return forms[0] if forms else None


def _form_data(form: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in form.get("inputs") or []:
        name = _text(item.get("name"))
        if not name:
            continue
        input_type = _text(item.get("type")).lower()
        if input_type in {"checkbox", "radio"} and "checked" not in item:
            continue
        result[name] = str(item.get("value") or "")
    return result


def _jsf_submit_field(form: dict[str, Any], page: str) -> tuple[str, str]:
    form_id = _text(form.get("id"))
    controls = form.get("controls") or []

    for item in controls:
        control_type = _text(item.get("type")).lower()
        name = _text(item.get("name"))
        if name and control_type in {"submit", "button"}:
            return name, _text(item.get("value")) or name

    for item in controls:
        onclick = html.unescape(str(item.get("onclick") or ""))
        if not onclick:
            continue
        for name, value in re.findall(
            r"['\"]([^'\"]+)['\"]\s*:\s*['\"]([^'\"]+)['\"]",
            onclick,
        ):
            if form_id and not name.startswith(f"{form_id}:"):
                continue
            if name == value:
                return name, value

    if form_id:
        match = re.search(
            rf"['\"]({re.escape(form_id)}:j_id\d+)['\"]\s*:\s*['\"]\1['\"]",
            html.unescape(page or ""),
            flags=re.I,
        )
        if match:
            return match.group(1), match.group(1)

    return "", ""


def _reading_points(readings: list[dict[str, Any]], types: tuple[str, ...]) -> list[dict[str, Any]]:
    direct: dict[tuple[date, str], dict[str, Any]] = {}
    for item in readings:
        if not isinstance(item, dict):
            continue
        reading_date = _date(item.get("measureDate"))
        serial = _text(item.get("SerialNumber"))
        if reading_date is None:
            continue
        multiplier = _number(item.get("constanta")) or 1.0
        key = (reading_date, serial)
        record = direct.setdefault(
            key,
            {
                "values": [],
                "tip_citire": _text(item.get("typeOfReading")),
            },
        )
        if not record.get("tip_citire"):
            record["tip_citire"] = _text(item.get("typeOfReading"))
        for meter in item.get("meter") or []:
            if not isinstance(meter, dict):
                continue
            energy_type = _text(meter.get("typeofenergy_measured")).upper()
            value = _number(meter.get("Value"))
            if energy_type and value is not None:
                record["values"].append((energy_type, value * multiplier))

    points: list[dict[str, Any]] = []
    for (reading_date, serial), record in direct.items():
        by_type = {energy_type: value for energy_type, value in record.get("values") or []}
        selected: float | None = None
        selected_type: str | None = None
        for energy_type in types:
            if energy_type in by_type:
                selected = by_type[energy_type]
                selected_type = energy_type
                break
        if selected is None and any(energy_type in TIPURI_CONSUM_TOTAL for energy_type in types):
            zone_values = [by_type[item] for item in TIPURI_CONSUM_ZONE if item in by_type]
            if zone_values:
                selected = sum(zone_values)
                selected_type = "+".join(item for item in TIPURI_CONSUM_ZONE if item in by_type)
        if selected is None:
            continue
        points.append(
            {
                "data": reading_date,
                "serie_contor": serial,
                "index": _compact_number(selected),
                "tip_energie": selected_type,
                "tip_citire": _text(record.get("tip_citire")) or None,
            }
        )
    points.sort(key=lambda item: (item["data"], item.get("serie_contor") or ""))
    return points


def _shift_month(month: date, offset: int) -> date:
    absolute = month.year * 12 + (month.month - 1) + offset
    return date(absolute // 12, absolute % 12 + 1, 1)


def _period_month(current: dict[str, Any]) -> date | None:
    current_date = current.get("data")
    if not isinstance(current_date, date):
        return None
    reading_type = _text(current.get("tip_citire")).lower()
    if "demont" in reading_type:
        return _shift_month(current_date.replace(day=1), -1)
    if current_date.day == 1:
        return (current_date - timedelta(days=1)).replace(day=1)
    return current_date.replace(day=1)


def _monthly_history(points: list[dict[str, Any]], register: str) -> list[dict[str, Any]]:
    by_serial: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        serial = _text(point.get("serie_contor"))
        if serial:
            by_serial[serial].append(point)

    buckets: dict[str, dict[str, Any]] = {}
    for serial, serial_points in by_serial.items():
        serial_points.sort(key=lambda item: item["data"])
        for previous, current in zip(serial_points, serial_points[1:]):
            previous_value = _number(previous.get("index"))
            current_value = _number(current.get("index"))
            current_date = current.get("data")
            previous_date = previous.get("data")
            period_month = _period_month(current)
            if (
                previous_value is None
                or current_value is None
                or not isinstance(current_date, date)
                or not isinstance(previous_date, date)
                or period_month is None
            ):
                continue
            delta = current_value - previous_value
            if delta < 0:
                continue

            month = _month_key(period_month)
            bucket = buckets.setdefault(
                month,
                {
                    "luna": month,
                    "data_citire": current_date.isoformat(),
                    "index_initial": _compact_number(previous_value),
                    "index_final": _compact_number(current_value),
                    "valoare": 0.0,
                    "registru": register,
                    "serii_contor": set(),
                    "numar_intervale": 0,
                    "prima_data": previous_date,
                    "ultima_data": current_date,
                    "date_disponibile": True,
                },
            )
            bucket["valoare"] += delta
            bucket["serii_contor"].add(serial)
            bucket["numar_intervale"] += 1
            if previous_date < bucket["prima_data"]:
                bucket["prima_data"] = previous_date
                bucket["index_initial"] = _compact_number(previous_value)
            if current_date >= bucket["ultima_data"]:
                bucket["ultima_data"] = current_date
                bucket["data_citire"] = current_date.isoformat()
                bucket["index_final"] = _compact_number(current_value)

    if not buckets:
        return []

    month_dates = [datetime.strptime(month, "%Y-%m").date() for month in buckets]
    latest_month = max(month_dates)
    earliest_month = max(min(month_dates), _shift_month(latest_month, -11))

    result: list[dict[str, Any]] = []
    cursor = latest_month
    while cursor >= earliest_month:
        month = _month_key(cursor)
        bucket = buckets.get(month)
        if bucket is None:
            result.append(
                {
                    "luna": month,
                    "data_citire": None,
                    "index_initial": None,
                    "index_final": None,
                    "valoare": None,
                    "registru": register,
                    "serie_contor": None,
                    "serii_contor": [],
                    "numar_intervale": 0,
                    "date_disponibile": False,
                }
            )
        else:
            series = sorted(bucket.pop("serii_contor"))
            bucket.pop("prima_data", None)
            bucket.pop("ultima_data", None)
            bucket["valoare"] = _compact_number(bucket["valoare"])
            bucket["serii_contor"] = series
            bucket["serie_contor"] = series[0] if len(series) == 1 else None
            result.append(bucket)
        cursor = _shift_month(cursor, -1)
    return result


class ClientFurnizorReteleElectrice(ClientFurnizor):
    cheie_furnizor = "retele_electrice"
    nume_prietenos = "Retele Electrice Romania"

    def __init__(self, *, sesiune: aiohttp.ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self._aura_context: dict[str, Any] | None = None
        self._aura_token: str | None = None
        self._authenticated = False


    async def _request_text(self, method: str, url: str, **kwargs: Any) -> tuple[aiohttp.ClientResponse, str]:
        try:
            async with self.sesiune.request(method, url, timeout=TIMEOUT, **kwargs) as response:
                text = await response.text(errors="replace")
                if response.status >= 500:
                    raise EroareConectare(f"Portalul Retele Electrice a raspuns cu HTTP {response.status}")
                return response, text
        except aiohttp.ClientError as err:
            raise EroareConectare(f"Nu se poate contacta portalul Retele Electrice: {err}") from err
        except TimeoutError as err:
            raise EroareConectare("Portalul Retele Electrice nu a raspuns la timp") from err

    async def _login(self) -> None:
        navigation_headers = {
            **HEADERS_BROWSER,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DNT": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        }
        login_response, login_page = await self._request_text(
            "GET",
            URL_LOGIN_FORM,
            headers=navigation_headers,
            allow_redirects=True,
        )
        form = _select_form(login_page, "loginForm")
        if form is None:
            fallback_response, fallback_page = await self._request_text(
                "GET",
                URL_LOGIN,
                headers=navigation_headers,
                allow_redirects=True,
            )
            login_response = fallback_response
            login_page = fallback_page
            form = _select_form(login_page, "loginForm")

        if form is None:
            raise EroareConectare("Formularul de autentificare Retele Electrice nu a fost gasit")

        data = _form_data(form)
        username_name = next(
            (
                str(item.get("name"))
                for item in form.get("inputs") or []
                if str(item.get("name") or "").lower().endswith(":username")
                or str(item.get("type") or "").lower() in {"email", "text"}
                and "user" in str(item.get("name") or "").lower()
            ),
            "",
        )
        password_name = next(
            (
                str(item.get("name"))
                for item in form.get("inputs") or []
                if str(item.get("name") or "").lower().endswith(":password")
                or str(item.get("type") or "").lower() == "password"
            ),
            "",
        )
        submit_name, submit_value = _jsf_submit_field(form, login_page)
        if not submit_name and _text(form.get("id")) == "loginPage:loginForm" and "logintest()" in login_page:
            submit_name = "loginPage:loginForm:j_id25"
            submit_value = submit_name
        if not username_name or not password_name:
            raise EroareParsare("Campurile de autentificare Retele Electrice nu au fost identificate")

        form_id = _text(form.get("id"))
        if form_id:
            data.setdefault(form_id, form_id)
        data[username_name] = self.utilizator
        data[password_name] = self.parola
        if submit_name:
            data[submit_name] = submit_value or submit_name

        action_raw = str(form.get("action") or "").strip()
        login_url = str(login_response.url)
        action = urljoin(login_url, action_raw) if action_raw else URL_LOGIN
        if "/PEDRO_SiteLogin" in action:
            action = URL_LOGIN
        headers = {
            **navigation_headers,
            "Origin": URL_BAZA,
            "Referer": URL_LOGIN_FORM,
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-User": "?1",
        }
        response, login_result = await self._request_text(
            "POST",
            action,
            data=data,
            headers=headers,
            allow_redirects=True,
        )

        frontdoor = _frontdoor_url(login_result)
        if frontdoor:
            await self._request_text(
                "GET",
                frontdoor,
                headers={**HEADERS_BROWSER, "Referer": str(response.url)},
                allow_redirects=True,
            )

        authenticated = await self._load_aura_config()
        if not authenticated:
            lowered = login_result.lower()
            if "parola" in lowered or "password" in lowered or "invalid" in lowered or "incorect" in lowered:
                raise EroareAutentificare("Datele de autentificare Retele Electrice sunt invalide")
            if response.url.path.lower().endswith("pedro_sitelogin") and not frontdoor:
                raise EroareAutentificare("Autentificarea Retele Electrice a esuat")
            raise EroareAutentificare("Sesiunea Retele Electrice nu a putut fi initializata")
        self._authenticated = True

    async def _load_aura_config(self) -> bool:
        candidates = (URL_PORTAL, f"{URL_BAZA}/s/activeDelegations")
        for url in candidates:
            response, page = await self._request_text(
                "GET",
                url,
                headers=HEADERS_BROWSER,
                allow_redirects=True,
            )
            login_detected = (
                "pedro_sitelogin" in str(response.url).lower()
                or "loginform" in page.lower()
            )
            config = _decode_aura_config(page) if not login_detected else {}
            context = config.get("context") if isinstance(config, dict) else None
            attributes = config.get("attributes") if isinstance(config, dict) else None
            attributes = attributes if isinstance(attributes, dict) else {}
            authenticated = str(attributes.get("authenticated") or "").lower()
            cookie_name = _text(config.get("eikoocnekot")) if isinstance(config, dict) else ""
            token = ""
            if cookie_name:
                cookies = self.sesiune.cookie_jar.filter_cookies(URL(URL_BAZA))
                morsel = cookies.get(cookie_name)
                token = morsel.value if morsel is not None else ""
            if not token and isinstance(config, dict):
                token = _text(config.get("token"))

            if login_detected or not isinstance(context, dict):
                continue
            if authenticated not in {"true", "1"}:
                continue
            if not token:
                continue
            self._aura_context = {
                "mode": context.get("mode") or "PROD",
                "fwuid": context.get("fwuid"),
                "app": context.get("app") or "siteforce:communityApp",
                "loaded": context.get("loaded") or {},
                "dn": context.get("dn") or [],
                "globals": context.get("globals") or {},
                "uad": True,
            }
            self._aura_token = token
            return True
        return False

    async def _ensure_login(self) -> None:
        if self._authenticated and self._aura_context and self._aura_token:
            return
        await self._login()

    async def _aura_action(
        self,
        *,
        descriptor: str,
        calling_descriptor: str,
        params: dict[str, Any] | None = None,
        route_name: str,
        retry: bool = True,
    ) -> Any:
        await self._ensure_login()
        message = {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": descriptor,
                    "callingDescriptor": calling_descriptor,
                    "params": params or {},
                    "version": None,
                }
            ]
        }
        data = {
            "message": json.dumps(message, ensure_ascii=False, separators=(",", ":")),
            "aura.context": json.dumps(self._aura_context, ensure_ascii=False, separators=(",", ":")),
            "aura.pageURI": "/s/",
            "aura.token": self._aura_token or "",
        }
        headers = {
            "Accept": "*/*",
            "Accept-Language": HEADERS_BROWSER["Accept-Language"],
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": URL_BAZA,
            "Referer": URL_PORTAL,
            "User-Agent": HEADERS_BROWSER["User-Agent"],
        }
        response, payload = await self._request_text(
            "POST",
            f"{URL_AURA}?r=1&other.{route_name}=1",
            data=data,
            headers=headers,
        )
        try:
            actions = _parse_aura_response(payload)
        except EroareParsare:
            if retry and (response.status in {401, 403} or "login" in payload.lower()):
                self._authenticated = False
                self._aura_context = None
                self._aura_token = None
                return await self._aura_action(
                    descriptor=descriptor,
                    calling_descriptor=calling_descriptor,
                    params=params,
                    route_name=route_name,
                    retry=False,
                )
            raise
        action = actions[0] if actions else {}
        state = _text(action.get("state")).upper()
        if state != "SUCCESS":
            errors = action.get("error") or []
            error_text = _text(errors)
            if retry and (response.status in {401, 403} or "invalid session" in error_text.lower()):
                self._authenticated = False
                self._aura_context = None
                self._aura_token = None
                return await self._aura_action(
                    descriptor=descriptor,
                    calling_descriptor=calling_descriptor,
                    params=params,
                    route_name=route_name,
                    retry=False,
                )
            raise EroareConectare(error_text or "Actiunea Salesforce Aura a esuat")
        return action.get("returnValue")

    async def _get_pods(self) -> list[dict[str, Any]]:
        value = await self._aura_action(
            descriptor="apex://PED_Utility/ACTION$getPODs",
            calling_descriptor="markup://c:PED_HomePage",
            route_name="PED_Utility.getPODs",
        )
        if not isinstance(value, list):
            raise EroareParsare("Lista POD Retele Electrice are un format neasteptat")
        pods = [item for item in value if isinstance(item, dict) and _text(item.get("POD__c") or item.get("Name"))]
        _LOGGER.debug("Retele Electrice a returnat %s locuri de consum", len(pods))
        return pods

    async def _visualforce_action(
        self,
        *,
        url: str,
        method_name: str,
        params: str,
        retry: bool = True,
    ) -> dict[str, Any]:
        await self._ensure_login()
        response, form_page = await self._request_text("GET", url, headers=HEADERS_BROWSER, allow_redirects=True)
        if "pedro_sitelogin" in str(response.url).lower():
            if retry:
                self._authenticated = False
                self._aura_context = None
                self._aura_token = None
                await self._ensure_login()
                return await self._visualforce_action(
                    url=url,
                    method_name=method_name,
                    params=params,
                    retry=False,
                )
            raise EroareAutentificare("Sesiunea Retele Electrice a expirat")
        form = _select_form(form_page, "j_id0:j_id2")
        if form is None:
            raise EroareParsare("Formularul Visualforce Retele Electrice nu a fost gasit")
        data = _form_data(form)
        form_id = _text(form.get("id")) or "j_id0:j_id2"
        data.update(
            {
                "AJAXREQUEST": "_viewRoot",
                form_id: form_id,
                "methodN": method_name,
                "params": params,
                "uniqueId": str(uuid.uuid4()),
                f"{form_id}:j_id3": f"{form_id}:j_id3",
            }
        )
        headers = {
            "Accept": "*/*",
            "Accept-Language": HEADERS_BROWSER["Accept-Language"],
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": URL_BAZA,
            "Referer": url,
            "User-Agent": HEADERS_BROWSER["User-Agent"],
            "X-Requested-With": "XMLHttpRequest",
        }
        _, result_page = await self._request_text("POST", url, data=data, headers=headers)
        result = _extract_async_json(result_page)
        if not result and retry and "login" in result_page.lower():
            self._authenticated = False
            self._aura_context = None
            self._aura_token = None
            await self._ensure_login()
            return await self._visualforce_action(
                url=url,
                method_name=method_name,
                params=params,
                retry=False,
            )
        return result

    @staticmethod
    def _fiscal_params(item: dict[str, Any], pod: str) -> tuple[str, str]:
        fiscal_code = _text(item.get("Fiscal_Code__c"))
        consumer_type = _text(item.get("Consumer_Type_Account__c")).lower()
        personal = consumer_type in {"casnic", "residential", "household"} or len(re.sub(r"\D", "", fiscal_code)) == 13
        if personal:
            return f",,{fiscal_code},{pod}", f"{fiscal_code},,{pod}"
        return f",{fiscal_code},,{pod}", f",{fiscal_code},{pod}"

    async def _get_pod_technical(self, pod: str) -> dict[str, Any]:
        result = await self._visualforce_action(
            url=URL_DETALII_POD,
            method_name="queryPOD",
            params=f"{pod},Client_Company",
        )
        row = result.get("row") if isinstance(result, dict) else None
        return row if isinstance(row, dict) else result

    async def _get_readings(self, item: dict[str, Any], pod: str) -> dict[str, Any]:
        prefix, _ = self._fiscal_params(item, pod)
        if not _text(item.get("Fiscal_Code__c")):
            return {}
        end = date.today()
        start = end - timedelta(days=410)
        params = (
            f"{prefix},{start.strftime('%d/%m/%Y')} 00:00:00,"
            f"{end.strftime('%d/%m/%Y')} 23:59:59"
        )
        return await self._visualforce_action(
            url=URL_CITIRI,
            method_name="RetriveSingleSelf",
            params=params,
        )

    async def _get_meter_info(self, item: dict[str, Any], pod: str) -> dict[str, Any]:
        _, params = self._fiscal_params(item, pod)
        if not _text(item.get("Fiscal_Code__c")):
            return {}
        return await self._visualforce_action(
            url=URL_CONTOR,
            method_name="FindOutMeterCurrentInfo",
            params=params,
        )

    async def async_testeaza_conexiunea(self) -> str:
        pods = await self._get_pods()
        if not pods:
            raise EroareParsare("Nu exista locuri de consum in contul Retele Electrice")
        return self.utilizator.strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        pods = await self._get_pods()
        conturi: list[ContUtilitate] = []
        consumuri: list[ConsumUtilitate] = []
        client_general: str | None = None
        este_prosumator_global = False

        for item in pods:
            pod = _text(item.get("POD__c") or item.get("Name"))
            if not pod:
                continue
            address = _text(item.get("POD_Address__c"))
            client_name = _text(item.get("Account_Name__c"))
            if not client_general and client_name:
                client_general = client_name

            try:
                technical = await self._get_pod_technical(pod)
            except (EroareConectare, EroareParsare) as err:
                _LOGGER.debug("Datele tehnice Retele Electrice nu sunt disponibile pentru POD %s: %s", _masked_pod(pod), err)
                technical = {}
            try:
                readings_response = await self._get_readings(item, pod)
            except (EroareConectare, EroareParsare) as err:
                _LOGGER.warning("Istoricul Retele Electrice nu a putut fi citit pentru POD %s: %s", _masked_pod(pod), err)
                readings_response = {}

            technical_meters = technical.get("Contor") if isinstance(technical, dict) else None
            technical_meter = next((value for value in technical_meters or [] if isinstance(value, dict)), {})
            needs_meter_fallback = not (
                _text(technical_meter.get("seria"))
                and _text(technical_meter.get("det_tip") or technical_meter.get("marca"))
                and _text(technical_meter.get("data_montare"))
            )
            if needs_meter_fallback:
                try:
                    meter_response = await self._get_meter_info(item, pod)
                except (EroareConectare, EroareParsare) as err:
                    _LOGGER.debug("Datele curente ale contorului nu sunt disponibile pentru POD %s: %s", _masked_pod(pod), err)
                    meter_response = {}
            else:
                meter_response = {}

            readings = readings_response.get("XML_Readings") if isinstance(readings_response, dict) else None
            readings = [row for row in readings or [] if isinstance(row, dict)]
            consumption_points = _reading_points(readings, TIPURI_CONSUM_TOTAL)
            injection_points = _reading_points(readings, TIPURI_INJECTIE)
            monthly_consumption = _monthly_history(consumption_points, "consum")
            monthly_injection = _monthly_history(injection_points, "injectie")
            latest_consumption = consumption_points[-1] if consumption_points else None
            latest_injection = injection_points[-1] if injection_points else None
            _LOGGER.debug(
                "Retele Electrice POD %s: citiri_consum=%s citiri_injectie=%s luni_consum=%s luni_injectie=%s",
                _masked_pod(pod),
                len(consumption_points),
                len(injection_points),
                len(monthly_consumption),
                len(monthly_injection),
            )

            meter_row = meter_response.get("row") if isinstance(meter_response, dict) else None
            meter_row = meter_row if isinstance(meter_row, dict) else {}
            client_name = _text(technical.get("nume_client")) or client_name
            address = _text(technical.get("adresa_locons")) or address
            if not client_general and client_name:
                client_general = client_name
            series = _text(technical_meter.get("seria") or meter_row.get("METER") or item.get("EA_METER_SERIE__c"))
            meter_type = _text(technical_meter.get("det_tip") or technical_meter.get("marca") or meter_row.get("METER_TYPE") or item.get("EA_METER_TYPE__c"))
            meter_precision = _text(technical_meter.get("precizie"))
            installation_date = _iso_date(technical_meter.get("data_montare") or meter_row.get("INSTALLATION_DATE"))
            meter_status = _text(meter_row.get("METER_STATUS"))
            phase = _text(meter_row.get("METER_FAZIC") or technical_meter.get("tipmontaj_cod"))
            consumption_power = _number(technical.get("kw_aprobata") or item.get("Absorbed_Power_KW__c"))
            production_power = _number(technical.get("kw_evacuata") or item.get("Injected_Power_KW__c") or item.get("Evacuated_Power_KW__c"))
            is_prosumer = bool(item.get("isProductor__c")) or bool(injection_points) or bool((production_power or 0) > 0)
            este_prosumator_global = este_prosumator_global or is_prosumer
            distributor = item.get("DistributionCompany__r") or {}
            distributor_name = _text(distributor.get("Name")) if isinstance(distributor, dict) else ""
            supplier_name = _text(technical.get("furnizor"))
            contract_number = _strip_tags(item.get("CROS_Number_Contract__c")) or pod
            contract_validity = _iso_date(item.get("Validity_of_the_supply_contract__c"))
            voltage_value = _compact_number(_number(item.get("Voltage__c") or item.get("Nominal_Voltage_kV__c")))
            connection_point = _text(technical.get("u_delimitare")) or _text(item.get("Voltage_Level__c"))
            connection_state = _state_label(item)
            reading_frequency = _text(item.get("Reading_Path__c"))
            remote_reading = item.get("Remotely_read_meter__c")
            if remote_reading in (None, ""):
                remote_reading = "Da" if item.get("Remote_Sensing__c") or item.get("Smart_meter__c") else "Nu"

            raw = {
                "pod": pod,
                "client": client_name or None,
                "adresa_loc_consum": address or None,
                "tip_loc_consum": "Prosumator" if is_prosumer else _text(item.get("Consumer_Type_Account__c")) or "Consumator",
                "stare_loc_consum": connection_state,
                "operator_distributie": distributor_name or self.nume_prietenos,
                "furnizor": supplier_name or None,
                "serie_contor": series or None,
                "tip_contor": meter_type or None,
                "clasa_precizie": meter_precision or None,
                "data_instalare_contor": installation_date,
                "periodicitate_citire": reading_frequency or None,
                "masurare_orara": "Da" if item.get("Smart_meter__c") or item.get("IsSmartMeter__c") else "Nu",
                "masurare_zone_orare": "Da" if any(
                    any(zone in _text(row.get("tip_energie")).upper().split("+") for zone in TIPURI_CONSUM_ZONE)
                    for row in consumption_points
                ) else "Nu",
                "putere_aprobata_consum": _compact_number(consumption_power),
                "putere_aprobata_producere": _compact_number(production_power),
                "validitate_contract": contract_validity,
                "numar_atr": _text(technical.get("atr_number")) or None,
                "data_inregistrare_atr": _iso_date(technical.get("atr_date")),
                "cod_punct_masurare": pod,
                "punct_racordare": connection_point or None,
                "tensiune_delimitare": voltage_value,
                "telecitit": _text(technical.get("telecitit")) or _text(remote_reading) or None,
                "stare_contor": meter_status or None,
                "configuratie_faze": phase or None,
                "istoric_citiri": [
                    {
                        "data": point["data"].isoformat(),
                        "index": point.get("index"),
                        "serie_contor": point.get("serie_contor"),
                        "tip_energie": point.get("tip_energie"),
                        "tip_citire": point.get("tip_citire"),
                    }
                    for point in consumption_points[-20:]
                ],
                "istoric_lunar_consum": monthly_consumption,
                "istoric_lunar_injectie": monthly_injection,
            }
            conturi.append(
                ContUtilitate(
                    id_cont=pod,
                    nume=address or f"POD {pod}",
                    tip_cont="pod_retele_electrice",
                    id_contract=contract_number,
                    adresa=address or None,
                    stare=connection_state,
                    tip_utilitate="energie",
                    tip_serviciu="distributie energie electrica",
                    este_prosumator=is_prosumer,
                    date_brute=raw,
                )
            )

            latest_consumption_value = latest_consumption.get("index") if latest_consumption else None
            latest_injection_value = latest_injection.get("index") if latest_injection else None
            consumption_last = next(
                (row.get("valoare") for row in monthly_consumption if row.get("valoare") is not None),
                None,
            )
            injection_last = next(
                (row.get("valoare") for row in monthly_injection if row.get("valoare") is not None),
                None,
            )
            consumption_total = _compact_number(sum(float(row.get("valoare") or 0) for row in monthly_consumption)) if monthly_consumption else None
            injection_total = _compact_number(sum(float(row.get("valoare") or 0) for row in monthly_injection)) if monthly_injection else None
            consumption_date = latest_consumption["data"].isoformat() if latest_consumption else None
            injection_date = latest_injection["data"].isoformat() if latest_injection else None

            values: list[tuple[str, Any, str | None]] = [
                ("client", client_name or None, None),
                ("pod", pod, None),
                ("adresa_loc_consum", address or None, None),
                ("loc_consum", pod, None),
                ("furnizor", supplier_name or None, None),
                ("stare_loc_consum", connection_state, None),
                ("tip_loc_consum", raw["tip_loc_consum"], None),
                ("serie_contor", series or None, None),
                ("tip_contor", meter_type or None, None),
                ("clasa_precizie", meter_precision or None, None),
                ("data_instalare_contor", installation_date, None),
                ("periodicitate_citire", reading_frequency or None, None),
                ("index_consum", latest_consumption_value, "kWh"),
                ("consum_ultima_perioada", consumption_last, "kWh"),
                ("consum_ultimele_12_luni", consumption_total, "kWh"),
                ("data_ultima_citire_consum", consumption_date, None),
                ("index_injectie", latest_injection_value, "kWh"),
                ("injectie_ultima_perioada", injection_last, "kWh"),
                ("injectie_ultimele_12_luni", injection_total, "kWh"),
                ("data_ultima_citire_injectie", injection_date, None),
                ("putere_aprobata_consum", _compact_number(consumption_power), "kW"),
                ("putere_aprobata_producere", _compact_number(production_power), "kW"),
                ("validitate_contract", contract_validity, None),
                ("numar_atr", raw["numar_atr"], None),
                ("data_inregistrare_atr", raw["data_inregistrare_atr"], None),
                ("cod_punct_masurare", pod, None),
                ("punct_racordare", raw["punct_racordare"], None),
                ("tensiune_delimitare", raw["tensiune_delimitare"], "kV"),
                ("masurare_orara", raw["masurare_orara"], None),
                ("masurare_zone_orare", raw["masurare_zone_orare"], None),
            ]
            for key, value, unit in values:
                if value in (None, ""):
                    continue
                consumuri.append(
                    ConsumUtilitate(
                        key,
                        value,
                        unit,
                        id_cont=pod,
                        tip_utilitate="energie",
                        tip_serviciu="distributie",
                    )
                )

        consumuri.extend(
            [
                ConsumUtilitate("numar_conturi", len(conturi), None, tip_utilitate="energie", tip_serviciu="distributie"),
                ConsumUtilitate("nume_client", client_general, None, tip_utilitate="energie", tip_serviciu="distributie"),
                ConsumUtilitate("este_prosumator", "da" if este_prosumator_global else "nu", None, tip_utilitate="energie", tip_serviciu="distributie"),
            ]
        )
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=[],
            consumuri=consumuri,
            extra={
                "suport_facturi": False,
                "suport_transmitere_index": False,
                "operator_distributie": True,
            },
        )
