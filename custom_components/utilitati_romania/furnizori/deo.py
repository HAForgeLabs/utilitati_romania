from __future__ import annotations

import base64
import html
import json
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_HUB_DASHBOARD = "https://deohub.distributieoltenia.ro/user/dashboard"
URL_PORTAL_LOGIN = "https://portal.distributieoltenia.ro/auth/login"
URL_PORTAL = "https://portal.distributieoltenia.ro"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}


def _text(value: Any) -> str:
    return html.unescape(re.sub(r"\s+", " ", str(value or "")).strip())


def _strip_tags(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    return _text(re.sub(r"<[^>]+>", " ", value))


def _parse_number(value: Any) -> float | None:
    text = _text(value).replace(" ", "")
    if not text:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    raw = match.group(0).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _decode_token(token: str) -> dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        value = json.loads(decoded)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _extract_locations(page: str) -> list[dict[str, Any]]:
    match = re.search(r"\blet\s+data\s*=\s*(\[.*?\])\s*;", page, flags=re.I | re.S)
    if match:
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            value = []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    locations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token_match in re.finditer(
        r"(?:informatiiContract|dateTehnice|informatiiGrup|istoricIndecsi)\?token=([^\"'&<>\s]+)",
        page,
        flags=re.I,
    ):
        token = html.unescape(token_match.group(1))
        location = _decode_token(token)
        premise = _text(location.get("PREMISE"))
        if location and premise not in seen:
            locations.append(location)
            seen.add(premise)
    return locations


def _encode_location_token(location: dict[str, Any]) -> str:
    raw = json.dumps(location, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


class _DeoSectionParser(HTMLParser):
    """Extrage perechile eticheta-valoare din blocurile DEO."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self._section_depth = 0
        self._label_depth = 0
        self._embedded_value_depth = 0
        self._label_parts: list[str] = []
        self._value_parts: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        raw = next((value or "" for key, value in attrs if key.lower() == "class"), "")
        return {item.strip().lower() for item in raw.split() if item.strip()}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag.lower() == "div":
            if self._section_depth:
                self._section_depth += 1
            elif {"left-section", "right-section"} & classes:
                self._section_depth = 1
                self._label_depth = 0
                self._embedded_value_depth = 0
                self._label_parts = []
                self._value_parts = []
            return

        if not self._section_depth:
            return

        if tag.lower() == "span":
            if self._label_depth:
                self._label_depth += 1
                if "ele-consm-result" in classes:
                    self._embedded_value_depth = self._label_depth
            elif {"data-item-value", "left-data-item-value"} & classes:
                self._label_depth = 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._section_depth:
            return

        if tag == "span" and self._label_depth:
            if self._embedded_value_depth == self._label_depth:
                self._embedded_value_depth = 0
            self._label_depth -= 1
            return

        if tag != "div":
            return

        self._section_depth -= 1
        if self._section_depth:
            return

        label = _text(" ".join(self._label_parts)).strip(" :")
        value = _text(" ".join(self._value_parts)).strip(" :")
        if label and value:
            self.values[label] = value

    def handle_data(self, data: str) -> None:
        if not self._section_depth or not data.strip():
            return
        if self._label_depth and not self._embedded_value_depth:
            self._label_parts.append(data)
        else:
            self._value_parts.append(data)



def _extract_active_meter_fields(page: str) -> dict[str, str]:
    """Extrage campurile contorului activ din lista dedicata portalului DEO."""
    result: dict[str, str] = {}
    block_match = re.search(
        r"Contor\s+energie\s+activa\s*:\s*(<ul\b[^>]*class=[\"'][^\"']*left-data-item-list[^\"']*[\"'][^>]*>.*?</ul>)",
        page,
        flags=re.I | re.S,
    )
    if not block_match:
        return result

    block = block_match.group(1)
    aliases = {
        "tip": "Contor energie activa stanga- Tip",
        "seria": "Contor energie activa stanga- Seria",
        "clasa de precizie": "Contor energie activa stanga- Clasa de precizie",
        "constanta": "Contor energie activa stanga- Constanta",
    }
    for item in re.findall(r"<li\b[^>]*>(.*?)</li>", block, flags=re.I | re.S):
        label_match = re.match(r"\s*([^:<]+?)\s*:\s*", _strip_tags(re.sub(r"<span\b[^>]*>.*?</span>", "", item, flags=re.I | re.S)))
        value_match = re.search(r"<span\b[^>]*>(.*?)</span>", item, flags=re.I | re.S)
        if not label_match or not value_match:
            continue
        label = _text(label_match.group(1)).lower()
        value = _strip_tags(value_match.group(1))
        target = aliases.get(label)
        if target and value:
            result[target] = value
    return result

def _extract_label_map(page: str) -> dict[str, str]:
    result: dict[str, str] = {}

    # Fallback pentru eventualele pagini care folosesc tabele clasice.
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, flags=re.I | re.S):
        cells = [_strip_tags(x) for x in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.I | re.S)]
        cells = [x for x in cells if x]
        if len(cells) < 2:
            continue
        for idx in range(0, len(cells) - 1, 2):
            label = cells[idx].strip(" :")
            value = cells[idx + 1].strip()
            if label and value:
                result[label] = value

    parser = _DeoSectionParser()
    parser.feed(page)
    result.update(parser.values)

    # Campurile contorului activ au prioritate fata de antetele tabelului ascuns.
    # Portalul foloseste etichete cu spatii usor diferite, care se normalizeaza la
    # aceeasi cheie; eliminam variantele vechi inainte de a introduce valoarea
    # extrasa din lista vizibila <ul class="left-data-item-list">.
    active_meter = _extract_active_meter_fields(page)
    for canonical_label, value in active_meter.items():
        canonical_normalized = _normalize_label(canonical_label)
        for existing_label in list(result):
            if _normalize_label(existing_label) == canonical_normalized:
                result.pop(existing_label, None)
        result[canonical_label] = value
    return result


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _value_by_labels(values: dict[str, str], *labels: str) -> str | None:
    normalized = [(_normalize_label(key), value) for key, value in values.items()]

    # Mai intai potrivire exacta, pentru a evita ca etichete scurte precum
    # „Tip” sau „Seria” sa preia valoarea campului vecin.
    for label in labels:
        wanted = _normalize_label(label)
        for existing, value in normalized:
            if existing == wanted:
                return value

    # Portalul prefixeaza uneori campurile contorului cu
    # „Contor energie activa stanga-”. Acceptam doar potriviri de sufix sau
    # etichete care incep cu denumirea completa cautata.
    for label in labels:
        wanted = _normalize_label(label)
        for existing, value in normalized:
            if wanted and (existing.endswith(wanted) or existing.startswith(wanted)):
                return value
    return None


def _parse_history_date(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    timestamp_match = re.search(r"Date\((\d+)\)", text)
    if timestamp_match:
        return datetime.fromtimestamp(
            int(timestamp_match.group(1)) / 1000,
            tz=timezone.utc,
        ).date().isoformat()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _history_row(
    *,
    serie: Any,
    registru: Any,
    descriere: Any,
    data_citire: Any,
    index: Any,
    consum: Any,
    tip_citire: Any,
    constanta: Any,
    motiv: Any,
) -> dict[str, Any]:
    return {
        "serie": _text(serie),
        "registru": _text(registru),
        "descriere_registru": _text(descriere),
        "data_citire": _parse_history_date(data_citire),
        "index": _parse_number(index),
        "consum": _parse_number(consum),
        "tip_citire": _text(tip_citire),
        "constanta_facturare": _parse_number(constanta),
        "motiv": _text(motiv),
    }


def _extract_history(page: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    match = re.search(r"\blet\s+data\s*=\s*(\[.*?\])\s*;", page, flags=re.I | re.S)
    if match:
        try:
            rows = json.loads(match.group(1))
        except json.JSONDecodeError:
            rows = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            result.append(_history_row(
                serie=item.get("SERIAL") or item.get("METER"),
                registru=item.get("REGISTER"),
                descriere=item.get("REGISTER_DESC"),
                data_citire=item.get("READING_DATE"),
                index=item.get("MRINDEX"),
                consum=item.get("CONSUMPTION"),
                tip_citire=item.get("READING_TYPE"),
                constanta=item.get("BILLING_CONSTANT"),
                motiv=item.get("REASON"),
            ))

    if not result:
        table_match = re.search(
            r'<table\b[^>]*id=["\']usage["\'][^>]*>(.*?)</table>',
            page,
            flags=re.I | re.S,
        )
        table = table_match.group(1) if table_match else page
        for row in re.findall(r'<tr\b[^>]*class=["\'][^"\']*graph-data[^"\']*["\'][^>]*>(.*?)</tr>', table, flags=re.I | re.S):
            cells = [_strip_tags(cell) for cell in re.findall(r'<td\b[^>]*>(.*?)</td>', row, flags=re.I | re.S)]
            if len(cells) < 9:
                continue
            result.append(_history_row(
                serie=cells[1] or cells[0],
                registru=cells[2],
                descriere=cells[3],
                data_citire=cells[4],
                index=cells[5],
                tip_citire=cells[6],
                constanta=cells[7],
                consum=cells[8],
                motiv=cells[10] if len(cells) > 10 else None,
            ))

    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in result:
        key = (
            row.get("serie"),
            row.get("registru"),
            row.get("data_citire"),
            row.get("index"),
            row.get("consum"),
        )
        unique[key] = row
    ordered = list(unique.values())
    ordered.sort(key=lambda row: row.get("data_citire") or "", reverse=True)
    return ordered


def _monthly_history(rows: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    monthly: dict[str, dict[str, Any]] = {}
    for row in rows:
        read_date = _text(row.get("data_citire"))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", read_date):
            continue
        month = read_date[:7]
        item = monthly.setdefault(month, {
            "luna": month,
            "valoare": 0.0,
            "index_final": None,
            "data_ultima_citire": None,
        })
        value = row.get("consum")
        if isinstance(value, (int, float)):
            item["valoare"] = round(float(item["valoare"]) + float(value), 3)
        if not item["data_ultima_citire"] or read_date > item["data_ultima_citire"]:
            item["data_ultima_citire"] = read_date
            item["index_final"] = row.get("index")
    return [monthly[key] for key in sorted(monthly, reverse=True)[:limit]]


class ClientFurnizorDeo(ClientFurnizor):
    cheie_furnizor = "deo"
    nume_prietenos = "Distributie Energie Oltenia"

    def __init__(self, *, sesiune: aiohttp.ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self._autentificat = False
        self._pagina_locuri = ""
        self._url_locuri = ""

    async def _request(self, method: str, url: str, **kwargs: Any) -> tuple[int, str, str]:
        try:
            async with self.sesiune.request(
                method,
                url,
                headers={**HEADERS, **kwargs.pop("headers", {})},
                timeout=aiohttp.ClientTimeout(total=60),
                **kwargs,
            ) as response:
                body = await response.text(errors="ignore")
                return response.status, body, str(response.url)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectare(f"Eroare de conectare la portalul DEO: {err}") from err

    async def _autentifica_hub(self) -> tuple[str, str]:
        ultima_pagina = ""
        ultima_adresa = ""

        for incercare in range(1, 3):
            status, login_page, login_url = await self._request(
                "GET",
                URL_HUB_DASHBOARD,
                allow_redirects=True,
            )
            if status >= 400:
                raise EroareConectare(
                    f"Portalul de autentificare DEO a raspuns cu HTTP {status}"
                )

            action_match = re.search(
                r"<form[^>]+action=[\"']([^\"']+)[\"']",
                login_page,
                flags=re.I | re.S,
            )
            if not action_match:
                raise EroareParsare("Nu am identificat formularul de autentificare DEO")

            action = urljoin(login_url, html.unescape(action_match.group(1)))

            try:
                async with self.sesiune.post(
                    action,
                    data={
                        "username": self.utilizator,
                        "password": self.parola,
                        "credentialId": "",
                    },
                    headers={
                        **HEADERS,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "null",
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                    allow_redirects=False,
                ) as raw_response:
                    raw_body = await raw_response.text(errors="ignore")
                    raw_location = raw_response.headers.get("Location", "")

                    if raw_response.status in {301, 302, 303, 307, 308} and raw_location:
                        status, dashboard, dashboard_url = await self._request(
                            "GET",
                            urljoin(action, raw_location),
                            allow_redirects=True,
                        )
                    else:
                        status = raw_response.status
                        dashboard = raw_body
                        dashboard_url = str(raw_response.url)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EroareConectare(f"Eroare de conectare la autentificarea DEO: {err}") from err

            ultima_pagina = dashboard
            ultima_adresa = dashboard_url

            autentificat = (
                status < 400
                and "login-actions/authenticate" not in dashboard_url
                and 'name="username"' not in dashboard
                and "/user/dashboard" in dashboard_url
            )
            if autentificat:
                return dashboard, dashboard_url

            error_text = ""
            for pattern in (
                r'<[^>]+id=["\']input-error["\'][^>]*>(.*?)</[^>]+>',
                r'<[^>]+class=["\'][^"\']*(?:alert|error|message)[^"\']*["\'][^>]*>(.*?)</[^>]+>',
            ):
                error_match = re.search(pattern, dashboard, flags=re.I | re.S)
                if error_match:
                    error_text = _strip_tags(error_match.group(1))[:300]
                    if error_text:
                        break
            self.sesiune.cookie_jar.clear_domain("auth.distributieoltenia.ro")
            self.sesiune.cookie_jar.clear_domain("deohub.distributieoltenia.ro")

        if 'name="username"' in ultima_pagina or "login-actions/authenticate" in ultima_adresa:
            raise EroareAutentificare("Credentialele DEO nu au fost acceptate")
        raise EroareAutentificare("Autentificarea DEO Hub nu a putut fi finalizata")

    async def _autentifica(self) -> None:
        if self._autentificat and self._pagina_locuri:
            return

        _dashboard, dashboard_url = await self._autentifica_hub()

        status, portal_page, portal_url = await self._request(
            "GET",
            URL_PORTAL_LOGIN,
            headers={"Referer": dashboard_url},
            allow_redirects=True,
        )
        if status >= 400:
            raise EroareConectare(f"Portalul utilizatorilor DEO a raspuns cu HTTP {status}")
        if "/pages/consumption-location/end_client" not in portal_url:
            if "login-actions/authenticate" in portal_url or "name=\"username\"" in portal_page:
                raise EroareAutentificare("Sesiunea DEO Hub nu a fost transferata catre Portalul Utilizatorilor")
            raise EroareParsare(f"Redirect DEO neasteptat: {portal_url.split('?')[0]}")

        self._pagina_locuri = portal_page
        self._url_locuri = portal_url
        self._autentificat = True

    async def async_testeaza_conexiunea(self) -> str:
        await self._autentifica()
        locations = _extract_locations(self._pagina_locuri)
        if not locations:
            raise EroareParsare("Nu au fost identificate locuri de consum in contul DEO")
        return str(locations[0].get("PARTNER") or self.utilizator).lower()

    async def _get_page(self, path: str, token: str) -> str:
        status, body, final_url = await self._request(
            "GET",
            f"{URL_PORTAL}{path}?token={token}",
            headers={"Referer": self._url_locuri or URL_PORTAL_LOGIN},
            allow_redirects=True,
        )
        if status in {401, 403} or "/auth/" in final_url:
            self._autentificat = False
            raise EroareAutentificare("Sesiunea portalului DEO a expirat")
        if status >= 400:
            raise EroareConectare(f"Pagina DEO {path} a raspuns cu HTTP {status}")
        return body

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        await self._autentifica()
        locations = _extract_locations(self._pagina_locuri)
        if not locations:
            raise EroareParsare("Lista locurilor de consum DEO este goala")

        conturi: list[ContUtilitate] = []
        consumuri: list[ConsumUtilitate] = []
        seen: set[str] = set()
        partner = ""
        client_name = ""

        for location in locations:
            token = _encode_location_token(location)
            premise = _text(location.get("PREMISE"))
            pod = _text(location.get("POD_LONG"))
            if not premise or premise in seen:
                continue
            seen.add(premise)
            partner = partner or _text(location.get("PARTNER"))
            client_name = client_name or _text(" ".join(filter(None, [location.get("NAME_FIRST"), location.get("NAME_LAST"), location.get("NAME_ORG")])))
            address_parts = [location.get("ADDR_CITY1"), location.get("ADDR_STREET"), location.get("ADDR_NUM"), location.get("ADDR_BUILDING"), location.get("ADDR_FLOOR"), location.get("ADDR_ROOMNUMBER"), location.get("ADDR_REGION")]
            address = ", ".join(_text(x) for x in address_parts if _text(x))

            contract_page = await self._get_page("/pages/informatiiContract", token)
            meter_page = await self._get_page("/pages/informatiiGrup", token)
            history_page = await self._get_page("/pages/istoricIndecsi", token)
            contract = _extract_label_map(contract_page)
            meter = _extract_label_map(meter_page)
            active_meter = _extract_active_meter_fields(meter_page)
            history = _extract_history(history_page)

            consumption_rows = [row for row in history if "consum" in str(row.get("descriere_registru") or "").lower() or row.get("registru") == "1.8.0"]
            production_rows = [row for row in history if any(x in str(row.get("descriere_registru") or "").lower() for x in ("productie", "producție", "inject")) or row.get("registru") == "2.8.0"]
            latest_consumption = consumption_rows[0] if consumption_rows else None
            latest_production = production_rows[0] if production_rows else None
            monthly_consumption = _monthly_history(consumption_rows)
            monthly_production = _monthly_history(production_rows)
            is_prosumer = bool(production_rows) or "prosum" in (_value_by_labels(contract, "Tip loc consum") or "").lower()
            supplier = _value_by_labels(contract, "Denumirea furnizorului", "Furnizor")
            state = _value_by_labels(contract, "Stare loc de consum (Deconectat/Conectat)", "Stare loc de consum", "Stare loc consum") or "Conectat"

            raw = {
                "pod": pod,
                "nlc": premise,
                "partner": partner,
                "contract": contract,
                "grup_masura": meter,
                "istoric_consum": consumption_rows,
                "istoric_injectie": production_rows,
                "istoric_lunar_consum": monthly_consumption,
                "istoric_lunar_injectie": monthly_production,
                "serie_contor": active_meter.get("Contor energie activa stanga- Seria") or _value_by_labels(meter, "Contor energie activa stanga- Seria", "Seria", "Serie"),
                "tip_contor": active_meter.get("Contor energie activa stanga- Tip") or _value_by_labels(meter, "Contor energie activa stanga- Tip", "Tip contor", "Tip"),
                "clasa_precizie": active_meter.get("Contor energie activa stanga- Clasa de precizie") or _value_by_labels(meter, "Contor energie activa stanga- Clasa de precizie", "Clasa de precizie"),
                "data_instalare_contor": _value_by_labels(meter, "Data instalarii contorului", "Data instalare contor"),
                "periodicitate_citire": _value_by_labels(meter, "Periodicitate de citire a grupului de masurare", "Periodicitate citire"),
                "furnizor": supplier,
                "tip_loc_consum": _value_by_labels(contract, "Tip loc consum(Consum, Producere, Producere si consum, Prosumer)", "Tip loc consum"),
            }
            conturi.append(ContUtilitate(
                id_cont=premise,
                nume=address or f"Loc consum {premise}",
                tip_cont="nlc_deo",
                id_contract=pod or premise,
                adresa=address or None,
                stare=state,
                tip_utilitate="energie",
                tip_serviciu="distributie energie electrica",
                este_prosumator=is_prosumer,
                date_brute=raw,
            ))

            values = {
                "client": client_name,
                "partener_deo": partner,
                "pod": pod,
                "nlc": premise,
                "furnizor": supplier,
                "stare_loc_consum": state,
                "tip_loc_consum": raw["tip_loc_consum"],
                "serie_contor": raw["serie_contor"],
                "tip_contor": raw["tip_contor"],
                "clasa_precizie": raw["clasa_precizie"],
                "data_instalare_contor": raw["data_instalare_contor"],
                "periodicitate_citire": raw["periodicitate_citire"],
                "index_consum": latest_consumption.get("index") if latest_consumption else None,
                "consum_ultima_perioada": latest_consumption.get("consum") if latest_consumption else None,
                "data_ultima_citire_consum": latest_consumption.get("data_citire") if latest_consumption else None,
                "index_injectie": latest_production.get("index") if latest_production else None,
                "injectie_ultima_perioada": latest_production.get("consum") if latest_production else None,
                "data_ultima_citire_injectie": latest_production.get("data_citire") if latest_production else None,
                "consum_ultimele_12_luni": round(sum(float(item.get("valoare") or 0) for item in monthly_consumption), 3),
                "injectie_ultimele_12_luni": round(sum(float(item.get("valoare") or 0) for item in monthly_production), 3),
            }
            for key, value in values.items():
                consumuri.append(ConsumUtilitate(key, value, "kWh" if key in {"index_consum", "consum_ultima_perioada", "index_injectie", "injectie_ultima_perioada", "consum_ultimele_12_luni", "injectie_ultimele_12_luni"} else None, id_cont=premise, tip_utilitate="energie", tip_serviciu="distributie"))

        consumuri.extend([
            ConsumUtilitate("numar_conturi", len(conturi), None, tip_utilitate="energie", tip_serviciu="distributie"),
            ConsumUtilitate("cod_client", partner or None, None, tip_utilitate="energie", tip_serviciu="distributie"),
            ConsumUtilitate("nume_client", client_name or None, None, tip_utilitate="energie", tip_serviciu="distributie"),
            ConsumUtilitate("este_prosumator", "da" if any(c.este_prosumator for c in conturi) else "nu", None, tip_utilitate="energie", tip_serviciu="distributie"),
        ])
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=[],
            consumuri=consumuri,
            extra={"suport_facturi": False, "suport_transmitere_index": False, "operator_distributie": True},
        )
