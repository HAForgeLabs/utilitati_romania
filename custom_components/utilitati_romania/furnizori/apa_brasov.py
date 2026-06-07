from __future__ import annotations

import asyncio
import html
import logging
import re
import unicodedata
from dataclasses import dataclass
from calendar import monthrange
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse

from aiohttp import ClientError, ClientResponse, ClientSession

from ..const import (
    CONF_ACCOUNT_ID,
    CONF_CONTRACT_ID,
    CONF_PREMISE_LABEL,
)
from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from ..naming import build_location_alias
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

_LOCKURI_SESIUNE: dict[str, asyncio.Lock] = {}


URL_BAZA = "https://myaccount.apabrasov.ro"
CALE_LOGIN = "/crosweb/auth/doli?from=%2Fcrosweb%2Fmyaccount%2Fservicii_online"
CALE_SERVICII = "/crosweb/myaccount/servicii_online"
CALE_FACTURI = "/crosweb/myaccount/evidenta_online"
CALE_PLATI = "/crosweb/myaccount/evplati_online"
CALE_INDEX = "/crosweb/myaccount/index_online"
CALE_CONSUM = "/crosweb/myaccount/evolcons_online"
CALE_CONTRACT = "/crosweb/myaccount/info_cntr"

ANTETE_IMPLICITE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.7,en;q=0.6",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}


class EroareApiApaBrasov(Exception):
    pass


class EroareAutentificareApaBrasov(EroareApiApaBrasov):
    pass


@dataclass(slots=True)
class OptiuneContractApaBrasov:
    loccons_id: str
    selector_value: str
    context_id: str | None
    eticheta: str


def _curata_text(valoare: Any) -> str:
    if valoare is None:
        return ""
    text = html.unescape(str(valoare))
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _text_html(pagina: str) -> str:
    return _curata_text(pagina)


def _valoare_numerica(valoare: Any) -> float | None:
    if valoare in (None, ""):
        return None
    text = html.unescape(str(valoare)).strip()
    text = re.sub(r"[^\d,\.\-]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _data_din_text(text: str) -> date | None:
    if not text:
        return None
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text)
    if match:
        zi, luna, an = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        try:
            return date(an, luna, zi)
        except ValueError:
            return None
    match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if match:
        an, luna, zi = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        try:
            return date(an, luna, zi)
        except ValueError:
            return None
    return None


def _date_din_text(text: str) -> list[date]:
    rezultat: list[date] = []
    for match in re.finditer(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text or ""):
        try:
            rezultat.append(date(int(match.group(3)), int(match.group(2)), int(match.group(1))))
        except ValueError:
            continue
    return rezultat


def _extrage_context_id(pagina: str) -> str | None:
    match = re.search(r'name=["\']crosweb\.contextID["\'][^>]*value=["\']([^"\']+)', pagina, flags=re.I)
    if match:
        return html.unescape(match.group(1)).strip()
    match = re.search(r"crosweb\.contextID\s*[:=]\s*[\"']([^\"']+)", pagina, flags=re.I)
    if match:
        return html.unescape(match.group(1)).strip()
    return None


def _extrage_loccons_din_url(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    valori = query.get("p_loccons.id") or query.get("loccons.id")
    if valori and str(valori[0]).strip():
        return str(valori[0]).strip()
    return None


def _extrage_loccons_din_valoare(valoare: str | None) -> str | None:
    text = html.unescape(str(valoare or "")).strip()
    if not text:
        return None
    if _este_loccons_numeric(text):
        return text
    loccons_id = _extrage_loccons_din_url(text)
    if loccons_id:
        return loccons_id
    match = re.search(r"(?:p_loccons\.id|loccons\.id|loccons_id|loccons)\D{0,12}(\d{3,})", text, flags=re.I)
    if match:
        return match.group(1)
    return None


def _extrage_valoare_selectata(attrs: str) -> str:
    for pattern in (
        r"value=[\"']([^\"']*)[\"']",
        r"data-value=[\"']([^\"']*)[\"']",
        r"data-id=[\"']([^\"']*)[\"']",
        r"data-loccons=[\"']([^\"']*)[\"']",
    ):
        match = re.search(pattern, attrs or "", flags=re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _cod_contract_din_text(text: str) -> str | None:
    match = re.search(r"\bP\d{3}/\d{2,}\b", html.unescape(text or ""), flags=re.I)
    if match:
        return match.group(0).upper()
    return None




def _lock_sesiune(utilizator: str) -> asyncio.Lock:
    cheie = (utilizator or "").strip().lower() or "default"
    lock = _LOCKURI_SESIUNE.get(cheie)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKURI_SESIUNE[cheie] = lock
    return lock


def _capitalize_nume(text: str) -> str:
    parti = []
    for token in re.split(r"(\s+|-)", text.strip().lower()):
        if not token or token.isspace() or token == "-":
            parti.append(token)
        elif len(token) <= 2 and token.isalpha():
            parti.append(token.upper())
        else:
            parti.append(token[:1].upper() + token[1:])
    return "".join(parti).strip()


def _normalizeaza_text_pentru_potrivire(text: str | None) -> str:
    text = _curata_text(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extrage_adresa_apa_brasov(text: str | None) -> str:
    sursa = _curata_text(text or "")
    if not sursa:
        return ""

    # Eliminăm prefixele tehnice din portal înainte să căutăm strada. Ordinea contează:
    # mai întâi contractul/data, apoi numerele rămase, altfel data poate rămâne ruptă
    # și devine greșit început de denumire.
    sursa = re.sub(r"\b(?:corespondenta|coresponden[tț]ă)\b", " ", sursa, flags=re.I)
    sursa = re.sub(r"\bP\d{3}/\d+\b", " ", sursa, flags=re.I)
    sursa = re.sub(r"\b\d{3,}/\d{1,2}\.\d{1,2}\.\d{4}\b", " ", sursa)
    sursa = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", " ", sursa)
    sursa = re.sub(r"\b\d{4,}\b", " ", sursa)
    sursa = re.sub(
        r"\b(?:nedeterminat|apa\s+rece\s+contorizata|brasov\s+populatie|populatie|excel|nu)\b",
        " ",
        sursa,
        flags=re.I,
    )
    sursa = re.sub(r"\s+", " ", sursa).strip(" -,;:")

    pattern_strada = (
        r"(?:strada|str\.|str\b|calea|bd\.?|bulevardul|bulevard|aleea|alee)\s+"
        r"(.+?)(?:\s*,?\s*(?:loc|jud)\b|$)"
    )
    potriviri = list(re.finditer(pattern_strada, sursa, flags=re.I))
    if potriviri:
        segment = potriviri[-1].group(1).strip()
    else:
        segment = sursa

    segment = re.sub(r"\b(?:loc|jud|brasov)\b.*$", "", segment, flags=re.I).strip(" -,.;:")
    segment = re.sub(r"\b(?:nr|num[aă]r(?:ul)?)\.?\s*", "nr ", segment, flags=re.I)
    segment = re.sub(r"\s+", " ", segment).strip(" -,.;:")
    return segment


def nume_scurt_locatie_apa_brasov(text: str | None, fallback: str | None = None) -> str:
    segment = _extrage_adresa_apa_brasov(text)
    if not segment:
        segment = _extrage_adresa_apa_brasov(fallback)
    if not segment:
        return "Loc consum"

    nr = None
    nr_match = re.search(r"\bnr\s*([0-9]+\s*[A-Za-z]?)\b", segment, flags=re.I)
    if nr_match:
        nr = nr_match.group(1).replace(" ", "").upper()
        segment = segment[: nr_match.start()].strip(" -,.:")
    else:
        # Pentru variantele de forma "ION HELIADE RADULESCU 2A".
        nr_match = re.search(r"\b([0-9]+\s*[A-Za-z]?)\b", segment)
        if nr_match:
            nr = nr_match.group(1).replace(" ", "").upper()
            segment = segment[: nr_match.start()].strip(" -,.:")

    segment = re.sub(r"\b(?:loc|jud|brasov)\b.*$", "", segment, flags=re.I).strip(" -,.:")
    if not segment:
        segment = "Loc consum"
    nume = _capitalize_nume(segment)
    if nr:
        nume = f"{nume} {nr}"
    return nume or "Loc consum"


def _interval_index_din_text(text: str | None) -> tuple[int, int] | None:
    match = re.search(
        r"(?:(?:intervalul|perioada)\s+)?(\d{1,2})\s*[-–—]\s*(\d{1,2})(?:\s+(?:al|ale)\s+lunii)?",
        text or "",
        flags=re.I,
    )
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    if not (1 <= start <= 31 and 1 <= end <= 31):
        return None
    return start, end


def _zile_pana_interval_citire(perioada: str | None, azi: date | None = None) -> int | None:
    interval = _interval_index_din_text(perioada)
    if not interval:
        return None
    start, end = interval
    azi = azi or date.today()
    ultima_zi_luna = monthrange(azi.year, azi.month)[1]
    start_curent = min(start, ultima_zi_luna)
    end_curent = min(end, ultima_zi_luna)
    if start_curent <= azi.day <= end_curent:
        return 0
    if azi.day < start_curent:
        return (date(azi.year, azi.month, start_curent) - azi).days
    luna = azi.month + 1
    an = azi.year
    if luna == 13:
        luna = 1
        an += 1
    start_urmator = min(start, monthrange(an, luna)[1])
    return (date(an, luna, start_urmator) - azi).days

def _cheie_contract_optiune(text: str, loccons_id: str) -> str:
    """Construiește o cheie stabilă pentru o opțiune din portal.

    La Apă Brașov același contract poate avea mai multe locuri de autocitire
    pe pagina de index. De aceea cheia nu trebuie să fie doar codul contractului,
    ci combină codul cu adresa curățată.
    """
    cod = _cod_contract_din_text(text) or ""
    adresa = _normalizeaza_text_pentru_potrivire(_extrage_adresa_apa_brasov(text))
    if cod and adresa:
        return f"{cod}|{adresa}"
    return cod or loccons_id or text.strip().lower()


def _este_loccons_numeric(valoare: str | None) -> bool:
    return bool(valoare and re.fullmatch(r"\d{3,}", str(valoare).strip()))


def _perioada_index_din_text(text: str) -> str | None:
    match = re.search(
        r"intervalul\s+(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+al\s+lunii",
        text or "",
        flags=re.I,
    )
    if match:
        return f"{int(match.group(1)):02d}-{int(match.group(2)):02d} ale lunii"
    match = re.search(r"perioada\s+(\d{1,2})\s*[-–—]\s*(\d{1,2})", text or "", flags=re.I)
    if match:
        return f"{int(match.group(1)):02d}-{int(match.group(2)):02d} ale lunii"
    return None


def _url_pagina(cale: str, loccons_id: str | None) -> str:
    query = {"p_loccons.id": loccons_id or ""}
    return f"{URL_BAZA}{cale}?{urlencode(query)}"


def _randuri_tabele(pagina: str) -> list[list[str]]:
    randuri: list[list[str]] = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", pagina, flags=re.I | re.S):
        celule = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)
        rand = [_curata_text(celula) for celula in celule]
        rand = [celula for celula in rand if celula != ""]
        if rand:
            randuri.append(rand)
    return randuri


def _primul_numar_document(rand: list[str], fallback: str) -> str:
    for celula in rand:
        match = re.search(r"\b\d{4,}\b", celula)
        if match:
            return match.group(0)
    return fallback


def _stare_din_text(text: str) -> str | None:
    t = text.lower()
    if any(x in t for x in ("neachitat", "neplatit", "neplătit", "restant", "restanta", "restanță")):
        return "neplătită"
    if any(x in t for x in ("achitat", "platit", "plătit", "incasat", "încasat")):
        return "plătită"
    return None


def _extrage_total_plata(pagini: list[str]) -> float | None:
    for pagina in pagini:
        text = _text_html(pagina)
        patternuri = [
            r"total\s+de\s+plata\s+per\s+client\s*:?\s*([-\d\.,]+)\s*lei",
            r"total\s+de\s+plată\s+per\s+client\s*:?\s*([-\d\.,]+)\s*lei",
            r"total\s+de\s+plata\s*:?\s*([-\d\.,]+)\s*lei",
            r"sold\s*(?:curent)?\s*:?\s*([-\d\.,]+)\s*lei",
        ]
        for pattern in patternuri:
            match = re.search(pattern, text, flags=re.I)
            if match:
                return _valoare_numerica(match.group(1))
    return None


def _extrage_facturi(pagina: str, loccons_id: str) -> list[dict[str, Any]]:
    facturi: list[dict[str, Any]] = []
    for rand in _randuri_tabele(pagina):
        text = " | ".join(rand)
        if not re.search(r"\b\d{6,}\b", text):
            continue
        date_gasite = _date_din_text(text)
        if len(date_gasite) < 2:
            continue

        id_factura = ""
        serie_numar = ""
        for celula in rand:
            celula_curata = celula.strip()
            if not id_factura and re.fullmatch(r"\d{6,}", celula_curata):
                id_factura = celula_curata
                continue
            if not serie_numar and re.search(r"\b[A-Z]{2,}\d{2}\s+\d+\b", celula_curata, flags=re.I):
                serie_numar = celula_curata

        valori_monetare: list[float] = []
        pozitie_a_doua_data = -1
        date_intalnite = 0
        for index_celula, celula in enumerate(rand):
            if _data_din_text(celula):
                date_intalnite += 1
                if date_intalnite == 2:
                    pozitie_a_doua_data = index_celula
                    break
        celule_dupa_scadenta = rand[pozitie_a_doua_data + 1 :] if pozitie_a_doua_data >= 0 else rand
        for celula in celule_dupa_scadenta:
            if re.search(r"\d+[,.]\d{2}", celula):
                valoare = _valoare_numerica(celula)
                if valoare is not None:
                    valori_monetare.append(valoare)

        if not valori_monetare:
            continue

        factura = {
            "number": id_factura or _primul_numar_document(rand, f"{loccons_id}-{len(facturi) + 1}"),
            "series_number": serie_numar,
            "issue_date": date_gasite[0].isoformat(),
            "due_date": date_gasite[1].isoformat(),
            "amount": valori_monetare[0],
            "vat_amount": valori_monetare[1] if len(valori_monetare) > 1 else None,
            "currency": "RON",
            "status": _stare_din_text(text),
            "raw": rand,
        }
        if not any(f["number"] == factura["number"] for f in facturi):
            facturi.append(factura)
    return facturi


def _extrage_plati(pagina: str, loccons_id: str) -> list[dict[str, Any]]:
    plati: list[dict[str, Any]] = []
    chei_vazute: set[tuple[str, float | None, str]] = set()
    for rand in _randuri_tabele(pagina):
        text = " | ".join(rand)
        sume = [_valoare_numerica(c) for c in rand if "lei" in c.lower() or re.search(r"\d+[,.]\d{2}", c)]
        sume = [s for s in sume if s is not None]
        date_gasite = _date_din_text(text)
        if not sume or not date_gasite:
            continue
        suma = sume[0]
        serie = ""
        for celula in rand:
            if re.search(r"\b(OP|CAB)\s*\d+\b", celula, flags=re.I):
                serie = celula.strip()
                break
        data_plata = date_gasite[0].isoformat()
        cheie = (data_plata, suma, serie)
        if cheie in chei_vazute:
            continue
        chei_vazute.add(cheie)
        plata = {
            "document_id": f"{loccons_id}-plata-{data_plata}-{len(plati) + 1}",
            "series_number": serie,
            "date": data_plata,
            "amount": suma,
            "currency": "RON",
            "raw": rand,
        }
        plati.append(plata)
    return plati


def _extrage_consumuri(pagina: str) -> list[dict[str, Any]]:
    consumuri: list[dict[str, Any]] = []
    for rand in _randuri_tabele(pagina):
        text = " | ".join(rand)
        if not any(unit in text.lower() for unit in ("mc", "m3", "m³")):
            continue
        valori = [_valoare_numerica(c) for c in rand if re.search(r"\d", c)]
        valori = [v for v in valori if v is not None]
        date_gasite = _date_din_text(text)
        if not valori:
            continue
        consumuri.append(
            {
                "value": valori[-1],
                "unit": "m³",
                "date": date_gasite[-1].isoformat() if date_gasite else None,
                "raw": rand,
            }
        )
    return consumuri


def _extrage_index(pagina: str) -> dict[str, Any]:
    text = _text_html(pagina)
    text_lower = text.lower()
    randuri = _randuri_tabele(pagina)
    candidati: list[tuple[date, float]] = []

    for rand in randuri:
        rand_text = " | ".join(rand)
        data_citire = _data_din_text(rand_text)
        if data_citire is None:
            continue
        pozitie_data = next((idx for idx, celula in enumerate(rand) if _data_din_text(celula)), -1)
        if pozitie_data < 0:
            continue
        for celula in rand[pozitie_data + 1 :]:
            valoare = _valoare_numerica(celula)
            if valoare is None:
                continue
            if 0 <= valoare < 100000:
                candidati.append((data_citire, valoare))
                break

    if candidati:
        data_index, valoare_index = max(candidati, key=lambda item: item[0])
    else:
        data_index = None
        valoare_index = None
        match = re.search(r"(?:index|citir[eaă])[^\d]{0,80}([\d\.,]+)", text, flags=re.I)
        if match:
            valoare_index = _valoare_numerica(match.group(1))

    perioada = _perioada_index_din_text(text)
    blocat = any(
        x in text_lower
        for x in (
            "nu se poate transmite",
            "nu este perioada",
            "citirea nu este permis",
            "reveniți în perioada",
            "reveniti in perioada",
            "perioada alocată",
            "perioada alocata",
        )
    )
    formular_transmitere = bool(re.search(r'<(input|button)[^>]+type=["\']submit', pagina, flags=re.I))
    permis = formular_transmitere and not blocat
    return {
        "value": valoare_index,
        "date": data_index.isoformat() if data_index else None,
        "is_open": bool(permis),
        "period": perioada,
        "days_until_open": _zile_pana_interval_citire(perioada),
        "raw_text": text[:1000],
    }


def _extrage_contract(pagina: str, loccons_id: str) -> dict[str, Any]:
    text = _text_html(pagina)
    adresa = None
    for pattern in (
        r"(?:adres[ăa]|loc(?:ul)?\s+de\s+consum)\s*:?\s*([^|]{8,160})",
        r"contract\s+furnizare\s*:?\s*([^|]{8,160})",
    ):
        match = re.search(pattern, text, flags=re.I)
        if match:
            adresa = match.group(1).strip(" -:;")
            break
    numar_contract = None
    match = re.search(r"(?:contract|cod\s+client|client)\s*:?\s*(\d{3,})", text, flags=re.I)
    if match:
        numar_contract = match.group(1)
    return {
        "loccons_id": loccons_id,
        "contract_id": numar_contract or loccons_id,
        "address": adresa,
        "raw_text": text[:2000],
    }


class ApiApaBrasov:
    def __init__(self, sesiune: ClientSession) -> None:
        self.sesiune = sesiune
        self._autentificat = False

    async def _request(self, metoda: str, url: str, **kwargs) -> ClientResponse:
        headers = dict(ANTETE_IMPLICITE)
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            raspuns = await self.sesiune.request(metoda, url, headers=headers, **kwargs)
        except (ClientError, TimeoutError) as err:
            raise EroareApiApaBrasov(f"Portalul Apă Brașov nu este disponibil: {err}") from err
        return raspuns

    async def _text(self, raspuns: ClientResponse) -> str:
        try:
            return await raspuns.text(errors="ignore")
        except UnicodeDecodeError:
            return (await raspuns.read()).decode("utf-8", "ignore")

    def _este_pagina_login(self, pagina: str, url_final: str = "") -> bool:
        text = pagina.lower()
        url_lower = url_final.lower()
        return (
            "/auth/doli" in url_lower
            or "name=\"username\"" in text
            or "name='username'" in text
            or "name=\"password\"" in text
            or "name='password'" in text
        )

    async def login(self, utilizator: str, parola: str) -> None:
        url_login = f"{URL_BAZA}{CALE_LOGIN}"
        raspuns_initial = await self._request("GET", url_login, allow_redirects=True)
        pagina_initiala = await self._text(raspuns_initial)
        if raspuns_initial.status >= 400:
            raise EroareAutentificareApaBrasov(f"Pagina de autentificare a returnat HTTP {raspuns_initial.status}.")

        payload = {
            "username": utilizator,
            "password": parola,
            "login": "Autentificare",
            "rememberme": "on",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": URL_BAZA,
            "Referer": str(raspuns_initial.url) or url_login,
        }
        raspuns = await self._request(
            "POST",
            url_login,
            data=urlencode(payload),
            headers=headers,
            allow_redirects=False,
        )
        if raspuns.status in (302, 303):
            locatie = raspuns.headers.get("Location") or CALE_SERVICII
            url_redirect = urljoin(URL_BAZA, locatie)
            raspuns_final = await self._request(
                "GET",
                url_redirect,
                headers={"Referer": url_login},
                allow_redirects=True,
            )
            pagina_finala = await self._text(raspuns_final)
        else:
            pagina_finala = await self._text(raspuns)
            raspuns_final = raspuns

        if raspuns_final.status >= 400 or self._este_pagina_login(pagina_finala, str(raspuns_final.url)):
            fragment = _text_html(pagina_finala)[:300]
            _LOGGER.debug("Autentificare Apă Brașov respinsă: HTTP %s, %s", raspuns_final.status, fragment)
            raise EroareAutentificareApaBrasov("Autentificarea în portalul Apă Brașov a eșuat.")
        if pagina_initiala and not pagina_finala:
            raise EroareAutentificareApaBrasov("Portalul Apă Brașov nu a returnat pagina de cont după autentificare.")
        self._autentificat = True

    async def _pagina_autentificata(self, cale: str, loccons_id: str | None = None) -> str:
        url = _url_pagina(cale, loccons_id)
        raspuns = await self._request("GET", url, allow_redirects=True)
        pagina = await self._text(raspuns)
        if raspuns.status >= 400:
            raise EroareApiApaBrasov(f"Portalul Apă Brașov a returnat HTTP {raspuns.status} pentru {cale}.")
        if self._este_pagina_login(pagina, str(raspuns.url)):
            raise EroareAutentificareApaBrasov("Sesiunea Apă Brașov a expirat.")
        return pagina

    def _extrage_optiuni_locuri_consum(self, pagina: str) -> list[OptiuneContractApaBrasov]:
        optiuni: list[OptiuneContractApaBrasov] = []
        chei_vazute: set[str] = set()
        context_id = _extrage_context_id(pagina)

        def adauga_optiune(loccons_id: str, selector_value: str, eticheta: str) -> None:
            eticheta_curata = _curata_text(eticheta)
            if not eticheta_curata:
                return
            if not _cod_contract_din_text(eticheta_curata) and "brasov" not in eticheta_curata.lower():
                return
            identificator = (_extrage_loccons_din_valoare(loccons_id) or loccons_id.strip() or _cod_contract_din_text(eticheta_curata) or selector_value.strip())
            if not identificator:
                return
            cheie = _cheie_contract_optiune(eticheta_curata, identificator)
            if cheie in chei_vazute:
                return
            chei_vazute.add(cheie)
            optiuni.append(
                OptiuneContractApaBrasov(
                    loccons_id=identificator,
                    selector_value=selector_value.strip(),
                    context_id=context_id,
                    eticheta=eticheta_curata,
                )
            )

        for href, text_link in re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", pagina, flags=re.I | re.S):
            loccons_id = _extrage_loccons_din_url(html.unescape(href)) or ""
            if loccons_id:
                adauga_optiune(loccons_id, loccons_id, text_link)

        for select_html in re.findall(r"<select\b[^>]*>(.*?)</select>", pagina, flags=re.I | re.S):
            for option_match in re.finditer(r"<option\b([^>]*)>(.*?)</option>", select_html, flags=re.I | re.S):
                attrs = option_match.group(1) or ""
                text_opt = option_match.group(2) or ""
                value = _extrage_valoare_selectata(attrs)
                loccons_id = _extrage_loccons_din_valoare(value) or _extrage_loccons_din_valoare(attrs) or ""
                adauga_optiune(loccons_id, value, text_opt)

        loccons_curent = _extrage_loccons_din_url("")
        if not optiuni and "servicii_online" in pagina.lower():
            optiuni.append(
                OptiuneContractApaBrasov(
                    loccons_id=loccons_curent or "principal",
                    selector_value=loccons_curent or "",
                    context_id=context_id,
                    eticheta="Cont principal",
                )
            )
        return optiuni

    def _gaseste_select_pentru_contract(self, pagina: str, cod_contract: str) -> tuple[str, str | None] | None:
        cod_normalizat = (cod_contract or "").strip().upper()
        if not cod_normalizat:
            return None
        context_id = _extrage_context_id(pagina)
        for select_html in re.findall(r"<select\b[^>]*>(.*?)</select>", pagina, flags=re.I | re.S):
            for option_match in re.finditer(r"<option\b([^>]*)>(.*?)</option>", select_html, flags=re.I | re.S):
                attrs = option_match.group(1) or ""
                text_opt = _curata_text(option_match.group(2) or "")
                if cod_normalizat not in text_opt.upper():
                    continue
                value = _extrage_valoare_selectata(attrs)
                if value:
                    return value, context_id
        return None

    def _gaseste_select_pentru_locatie(
        self,
        pagina: str,
        *,
        cod_contract: str | None = None,
        eticheta: str | None = None,
        loccons_id: str | None = None,
    ) -> tuple[str, str | None] | None:
        """Alege opțiunea corectă din dropdown-ul paginii curente.

        Portalul Apă Brașov folosește valori diferite în dropdown în funcție de pagină:
        la facturi/plăți sunt contracte, iar la transmitere index sunt locuri de consum/autocitire.
        De aceea nu putem reutiliza orb aceeași valoare de selector pe toate paginile.
        """
        cod_normalizat = (cod_contract or _cod_contract_din_text(eticheta or "") or "").strip().upper()
        tinta_norm = _normalizeaza_text_pentru_potrivire(eticheta)
        loccons_curat = str(loccons_id or "").strip()
        context_id = _extrage_context_id(pagina)
        candidati: list[tuple[int, str, str]] = []

        for select_html in re.findall(r"<select\b[^>]*>(.*?)</select>", pagina, flags=re.I | re.S):
            for option_match in re.finditer(r"<option\b([^>]*)>(.*?)</option>", select_html, flags=re.I | re.S):
                attrs = option_match.group(1) or ""
                text_opt = _curata_text(option_match.group(2) or "")
                value = _extrage_valoare_selectata(attrs)
                if not value:
                    continue

                scor = 0
                opt_norm = _normalizeaza_text_pentru_potrivire(text_opt)
                loccons_opt = _extrage_loccons_din_valoare(value) or _extrage_loccons_din_valoare(attrs) or ""
                cod_opt = _cod_contract_din_text(text_opt) or ""

                if loccons_curat and loccons_opt and loccons_curat == loccons_opt:
                    scor += 120
                if cod_normalizat and cod_opt and cod_normalizat == cod_opt.upper():
                    scor += 80
                if tinta_norm:
                    cuvinte_tinta = [c for c in tinta_norm.split() if len(c) >= 3 or re.fullmatch(r"\d+[A-Z]?", c)]
                    potriviri = sum(1 for cuv in cuvinte_tinta if cuv in opt_norm)
                    scor += min(potriviri * 12, 90)
                    nr_tinta = re.findall(r"\b\d+[A-Z]?\b", tinta_norm)
                    if nr_tinta and any(nr in opt_norm for nr in nr_tinta):
                        scor += 60

                if scor > 0:
                    candidati.append((scor, value, text_opt))

        if not candidati:
            return None
        candidati.sort(key=lambda item: item[0], reverse=True)
        return candidati[0][1], context_id

    async def _loccons_pentru_pagina(
        self,
        cale: str,
        loccons_id: str,
        selector_value: str | None,
        context_id: str | None,
        eticheta: str | None,
    ) -> str:
        """Rezolvă locul de consum potrivit pentru o anumită pagină din portal."""
        loccons_curat = str(loccons_id or "").strip()
        cod_contract = _cod_contract_din_text(loccons_curat) or _cod_contract_din_text(eticheta or "")

        # Pentru majoritatea paginilor, id-ul numeric al contractului funcționează direct.
        # Excepția importantă este pagina de index, unde dropdown-ul conține uneori mai multe
        # locuri de autocitire pentru același contract.
        if cale != CALE_INDEX and _este_loccons_numeric(loccons_curat):
            return loccons_curat

        pagina = await self._pagina_autentificata(cale, loccons_curat if _este_loccons_numeric(loccons_curat) else None)
        selectie = self._gaseste_select_pentru_locatie(
            pagina,
            cod_contract=cod_contract,
            eticheta=eticheta,
            loccons_id=loccons_curat if _este_loccons_numeric(loccons_curat) else None,
        )
        if selectie:
            valoare_selectata, context_curent = selectie
            loc_rezolvat = _extrage_loccons_din_valoare(valoare_selectata)
            if loc_rezolvat and cale != CALE_INDEX:
                return loc_rezolvat
            loc_rezolvat = await self._schimba_loc_consum(cale, valoare_selectata, context_curent or context_id)
            if loc_rezolvat:
                return loc_rezolvat

        # Folosim selector_value doar când nu avem cod de contract. În portalul
        # Apă Brașov același selector numeric are semnificații diferite pe pagini
        # diferite, deci fallback-ul orb poate muta contractul pe altă locație.
        if selector_value and not cod_contract:
            loc_rezolvat = await self._schimba_loc_consum(cale, selector_value, context_id)
            if loc_rezolvat:
                return loc_rezolvat

        loccons_din_selector = _extrage_loccons_din_valoare(selector_value)
        if loccons_din_selector:
            return loccons_din_selector
        return loccons_curat if loccons_curat != "principal" else ""

    async def _schimba_loc_consum(self, cale: str, selector_value: str, context_id: str | None = None) -> str | None:
        if not selector_value:
            return None

        pagina = await self._pagina_autentificata(cale, None)
        context = _extrage_context_id(pagina) or context_id
        if not context:
            return _extrage_loccons_din_valoare(selector_value)

        payload = {
            "crosweb.contextID": context,
            "body.selectie_loc.loc": selector_value,
            "crosweb.trigger$body.submitLoc": "",
        }
        raspuns = await self._request(
            "POST",
            _url_pagina(cale, None),
            data=urlencode(payload),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": URL_BAZA,
                "Referer": _url_pagina(cale, None),
            },
            allow_redirects=False,
        )
        locatie = raspuns.headers.get("Location") or ""
        loc_redirect = _extrage_loccons_din_url(locatie)
        if loc_redirect:
            return loc_redirect

        pagina_raspuns = await self._text(raspuns)
        return _extrage_loccons_din_valoare(str(raspuns.url)) or _extrage_loccons_din_valoare(pagina_raspuns)

    async def _posteaza_loc_consum_si_returneaza_pagina(
        self,
        cale: str,
        selector_value: str,
        context_id: str | None = None,
    ) -> str | None:
        """Selectează o opțiune din dropdown și returnează pagina rezultată.

        Portalul Apă Brașov nu expune întotdeauna un p_loccons.id numeric după
        schimbarea locației. Pentru parsare este mai sigur să folosim pagina HTML
        rezultată imediat după POST, nu să încercăm să reconstruim ulterior URL-ul.
        """
        if not selector_value:
            return None

        pagina_initiala = await self._pagina_autentificata(cale, None)
        context = _extrage_context_id(pagina_initiala) or context_id
        if not context:
            return None

        payload = {
            "crosweb.contextID": context,
            "body.selectie_loc.loc": selector_value,
            "crosweb.trigger$body.submitLoc": "",
        }
        raspuns = await self._request(
            "POST",
            _url_pagina(cale, None),
            data=urlencode(payload),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": URL_BAZA,
                "Referer": _url_pagina(cale, None),
            },
            allow_redirects=False,
        )

        locatie = raspuns.headers.get("Location") or ""
        if locatie:
            url_redirect = urljoin(URL_BAZA, locatie)
            raspuns_redirect = await self._request("GET", url_redirect, allow_redirects=True)
            pagina_redirect = await self._text(raspuns_redirect)
            if raspuns_redirect.status >= 400:
                raise EroareApiApaBrasov(
                    f"Portalul Apă Brașov a returnat HTTP {raspuns_redirect.status} după schimbarea locației."
                )
            if self._este_pagina_login(pagina_redirect, str(raspuns_redirect.url)):
                raise EroareAutentificareApaBrasov("Sesiunea Apă Brașov a expirat.")
            return pagina_redirect

        pagina = await self._text(raspuns)
        if pagina and not self._este_pagina_login(pagina, str(raspuns.url)):
            return pagina

        return await self._pagina_autentificata(cale, None)

    async def _pagina_pentru_locatie(
        self,
        cale: str,
        loccons_id: str,
        selector_value: str | None,
        context_id: str | None,
        eticheta: str | None,
    ) -> tuple[str, str]:
        """Returnează pagina selectată corect pentru contractul curent.

        La Apă Brașov valoarea numerică din dropdown este locală paginii:
        facturi/plăți/consum folosesc 0/1 pentru contracte, iar indexul poate
        avea 0/1/2 deoarece un contract poate avea mai multe locuri de autocitire.
        Din acest motiv fiecare pagină este selectată separat, după codul
        contractului și adresă, iar pagina rezultată este parsată imediat.
        """
        loccons_curat = str(loccons_id or "").strip()
        selector_curat = str(selector_value or "").strip()
        cod_contract = _cod_contract_din_text(loccons_curat) or _cod_contract_din_text(eticheta or "")

        pagina_initiala = await self._pagina_autentificata(
            cale,
            loccons_curat if _este_loccons_numeric(loccons_curat) else None,
        )

        selectie = self._gaseste_select_pentru_locatie(
            pagina_initiala,
            cod_contract=cod_contract,
            eticheta=eticheta or loccons_curat,
            loccons_id=loccons_curat if _este_loccons_numeric(loccons_curat) else None,
        )
        if selectie:
            valoare_selectata, context_curent = selectie
            pagina_selectata = await self._posteaza_loc_consum_si_returneaza_pagina(
                cale, valoare_selectata, context_curent or context_id
            )
            if pagina_selectata:
                if cod_contract and cod_contract not in _text_html(pagina_selectata).upper():
                    _LOGGER.debug(
                        "Apă Brașov: pagina %s selectată cu value=%s nu conține contractul %s; se ignoră pagina.",
                        cale,
                        valoare_selectata,
                        cod_contract,
                    )
                else:
                    return pagina_selectata, (
                        _extrage_loccons_din_valoare(valoare_selectata)
                        or _extrage_loccons_din_valoare(pagina_selectata)
                        or loccons_curat
                        or (cod_contract or "")
                    )

        if selector_curat and not cod_contract:
            pagina_selectata = await self._posteaza_loc_consum_si_returneaza_pagina(cale, selector_curat, context_id)
            if pagina_selectata:
                return pagina_selectata, (
                    _extrage_loccons_din_valoare(selector_curat)
                    or _extrage_loccons_din_valoare(pagina_selectata)
                    or loccons_curat
                )

        if cod_contract and cod_contract not in _text_html(pagina_initiala).upper():
            _LOGGER.debug(
                "Apă Brașov: nu s-a putut selecta contractul %s pe pagina %s. Opțiunea salvată: loccons=%s selector=%s etichetă=%s",
                cod_contract,
                cale,
                loccons_curat,
                selector_curat,
                eticheta,
            )
            raise EroareApiApaBrasov(f"Nu am putut selecta contractul Apă Brașov {cod_contract} pe pagina {cale}.")
        return pagina_initiala, loccons_curat or (cod_contract or "")

    async def _obtine_contracte_autentificat(self) -> list[OptiuneContractApaBrasov]:
        # Lista de contracte trebuie construită din pagina de facturi, nu din pagina de index.
        # Pagina de index poate conține mai multe locuri de autocitire pentru același contract
        # și ar crea dispozitive duplicate sau asociate greșit.
        pagina_facturi = await self._pagina_autentificata(CALE_FACTURI, None)
        optiuni_brute = self._extrage_optiuni_locuri_consum(pagina_facturi)

        optiuni: list[OptiuneContractApaBrasov] = []
        chei_vazute: set[str] = set()
        for optiune in optiuni_brute:
            cheie = _cheie_contract_optiune(optiune.eticheta, optiune.loccons_id)
            if cheie in chei_vazute:
                continue
            chei_vazute.add(cheie)

            loccons_rezolvat = _extrage_loccons_din_valoare(optiune.loccons_id)
            if not loccons_rezolvat and optiune.selector_value:
                loccons_rezolvat = await self._schimba_loc_consum(
                    CALE_FACTURI,
                    optiune.selector_value,
                    optiune.context_id,
                )

            optiuni.append(
                OptiuneContractApaBrasov(
                    loccons_id=loccons_rezolvat or optiune.loccons_id or optiune.selector_value,
                    selector_value=optiune.selector_value,
                    context_id=optiune.context_id,
                    eticheta=optiune.eticheta,
                )
            )

        if not optiuni:
            optiuni.append(
                OptiuneContractApaBrasov(
                    loccons_id="principal",
                    selector_value="",
                    context_id=None,
                    eticheta="Cont principal",
                )
            )
        return optiuni

    async def obtine_contracte(self, utilizator: str, parola: str) -> list[OptiuneContractApaBrasov]:
        async with _lock_sesiune(utilizator):
            await self.login(utilizator, parola)
            return await self._obtine_contracte_autentificat()

    async def _rezolva_loccons_id(
        self,
        loccons_id: str,
        selector_value: str | None,
        context_id: str | None,
        eticheta: str | None = None,
    ) -> str:
        loccons_direct = _extrage_loccons_din_valoare(loccons_id)
        if loccons_direct:
            return loccons_direct

        selector_curat = (selector_value or "").strip()

        # În beta-urile anterioare s-a putut salva în config entry fie valoarea
        # dropdown-ului (0/1/2), fie descrierea lungă a contractului. Valoarea
        # dropdown-ului NU este globală: pe pagina de facturi P020/139 poate fi 1,
        # iar pe pagina de index poate fi 2. De aceea, dacă avem codul contractului,
        # întâi căutăm codul în pagina curentă și abia apoi folosim selector_value
        # ca fallback. Altfel putem rezolva greșit P020/139 către P020/102.
        cod_contract = _cod_contract_din_text(loccons_id) or _cod_contract_din_text(eticheta or "")

        if cod_contract:
            for cale in (CALE_FACTURI, CALE_PLATI, CALE_CONSUM, CALE_CONTRACT, CALE_INDEX):
                pagina = await self._pagina_autentificata(cale, None)
                selectie = self._gaseste_select_pentru_contract(pagina, cod_contract)
                if not selectie:
                    continue
                valoare_selectata, context_curent = selectie
                loc_rezolvat = _extrage_loccons_din_valoare(valoare_selectata)
                if loc_rezolvat:
                    return loc_rezolvat
                loc_rezolvat = await self._schimba_loc_consum(cale, valoare_selectata, context_curent)
                if loc_rezolvat:
                    return loc_rezolvat

        loccons_din_selector = _extrage_loccons_din_valoare(selector_curat)
        if loccons_din_selector:
            return loccons_din_selector

        for cale in (CALE_FACTURI, CALE_PLATI, CALE_CONSUM, CALE_CONTRACT, CALE_INDEX):
            if selector_curat:
                loc_rezolvat = await self._schimba_loc_consum(cale, selector_curat, context_id)
                if loc_rezolvat:
                    return loc_rezolvat

        return "" if loccons_id == "principal" else loccons_id

    async def _obtine_date_dashboard_autentificat(
        self,
        loccons_id: str,
        selector_value: str | None = None,
        context_id: str | None = None,
        eticheta: str | None = None,
        pagina_servicii: str | None = None,
    ) -> dict[str, Any]:
        loccons_rezolvat = await self._rezolva_loccons_id(loccons_id, selector_value, context_id, eticheta)
        eticheta_tinta = str(eticheta or loccons_id or loccons_rezolvat or "")

        pagina_servicii = pagina_servicii or await self._pagina_autentificata(CALE_SERVICII, None)
        pagina_facturi, loc_facturi = await self._pagina_pentru_locatie(
            CALE_FACTURI, loccons_rezolvat or loccons_id, selector_value, context_id, eticheta_tinta
        )
        pagina_plati, loc_plati = await self._pagina_pentru_locatie(
            CALE_PLATI, loccons_rezolvat or loccons_id, selector_value, context_id, eticheta_tinta
        )
        pagina_index, loc_index = await self._pagina_pentru_locatie(
            CALE_INDEX, loccons_rezolvat or loccons_id, selector_value, context_id, eticheta_tinta
        )
        pagina_consum, loc_consum = await self._pagina_pentru_locatie(
            CALE_CONSUM, loccons_rezolvat or loccons_id, selector_value, context_id, eticheta_tinta
        )
        pagina_contract, loc_contract = await self._pagina_pentru_locatie(
            CALE_CONTRACT, loccons_rezolvat or loccons_id, selector_value, context_id, eticheta_tinta
        )

        id_stabil = loc_facturi or loc_contract or loccons_rezolvat or loccons_id
        facturi = _extrage_facturi(pagina_facturi, id_stabil)
        plati = _extrage_plati(pagina_plati, id_stabil)
        consumuri = _extrage_consumuri(pagina_consum)
        index = _extrage_index(pagina_index)
        contract = _extrage_contract(pagina_contract, loc_contract or id_stabil)
        contract["index_loccons_id"] = loc_index
        total_plata = _extrage_total_plata([pagina_facturi, pagina_servicii])

        ultima_factura = sorted(
            facturi,
            key=lambda item: item.get("issue_date") or "",
            reverse=True,
        )[0] if facturi else None
        ultima_plata = sorted(
            plati,
            key=lambda item: item.get("date") or "",
            reverse=True,
        )[0] if plati else None
        ultimul_consum = consumuri[-1] if consumuri else None

        return {
            "loccons_id": id_stabil,
            "selector_value": selector_value,
            "context_id": context_id,
            "page_loccons": {
                "facturi": loc_facturi,
                "plati": loc_plati,
                "consum": loc_consum,
                "contract": loc_contract,
                "index": loc_index,
            },
            "current_balance": {"value": total_plata, "currency": "RON"},
            "invoices": facturi,
            "payments": plati,
            "consumptions": consumuri,
            "last_invoice": ultima_factura,
            "last_payment": ultima_plata,
            "last_consumption": ultimul_consum,
            "last_meter_reading": index,
            "contract": contract,
        }

    async def obtine_date_dashboard(
        self,
        utilizator: str,
        parola: str,
        loccons_id: str,
        selector_value: str | None = None,
        context_id: str | None = None,
        eticheta: str | None = None,
    ) -> dict[str, Any]:
        async with _lock_sesiune(utilizator):
            await self.login(utilizator, parola)
            return await self._obtine_date_dashboard_autentificat(
                loccons_id,
                selector_value,
                context_id,
                eticheta,
            )

    async def obtine_toate_datele_dashboard(self, utilizator: str, parola: str) -> list[tuple[OptiuneContractApaBrasov, dict[str, Any]]]:
        async with _lock_sesiune(utilizator):
            await self.login(utilizator, parola)
            pagina_servicii = await self._pagina_autentificata(CALE_SERVICII, None)
            contracte = await self._obtine_contracte_autentificat()
            rezultate: list[tuple[OptiuneContractApaBrasov, dict[str, Any]]] = []
            for contract in contracte:
                date_brute = await self._obtine_date_dashboard_autentificat(
                    contract.loccons_id,
                    contract.selector_value,
                    contract.context_id,
                    contract.eticheta,
                    pagina_servicii,
                )
                rezultate.append((contract, date_brute))
            return rezultate


class ClientFurnizorApaBrasov(ClientFurnizor):
    cheie_furnizor = "apa_brasov"
    nume_prietenos = "Apă Brașov"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ApiApaBrasov(sesiune)

    async def async_obtine_contracte_disponibile(self) -> list[OptiuneContractApaBrasov]:
        try:
            return await self.api.obtine_contracte(self.utilizator, self.parola)
        except EroareAutentificareApaBrasov as err:
            raise EroareAutentificare(str(err)) from err
        except EroareApiApaBrasov as err:
            raise EroareConectare(str(err)) from err
        except Exception as err:
            raise EroareParsare(f"Eroare neașteptată la obținerea contractelor Apă Brașov: {err}") from err

    async def async_testeaza_conexiunea(self) -> str:
        contracte = await self.async_obtine_contracte_disponibile()
        if contracte:
            return contracte[0].loccons_id
        return self.utilizator.lower()

    def _construieste_modele_din_date(
        self,
        *,
        date_brute: dict[str, Any],
        loccons_id_initial: str,
        eticheta: str,
    ) -> tuple[ContUtilitate, list[FacturaUtilitate], list[ConsumUtilitate], dict[str, Any]]:
        loccons_final = str(date_brute.get("loccons_id") or loccons_id_initial)
        contract = date_brute.get("contract") or {}
        adresa = contract.get("address") or eticheta
        id_contract = str(contract.get("contract_id") or loccons_final)

        nume_locatie = nume_scurt_locatie_apa_brasov(adresa or eticheta, loccons_final)

        cont = ContUtilitate(
            id_cont=loccons_final,
            id_contract=id_contract,
            nume=nume_locatie,
            tip_cont="contract",
            adresa=adresa or eticheta,
            stare="activ",
            tip_utilitate="apa",
            tip_serviciu="apa_canal",
            date_brute=date_brute,
        )

        facturi: list[FacturaUtilitate] = []
        for factura in date_brute.get("invoices") or []:
            issue_date = _data_din_text(str(factura.get("issue_date") or ""))
            due_date = _data_din_text(str(factura.get("due_date") or ""))
            facturi.append(
                FacturaUtilitate(
                    id_factura=str(factura.get("number") or f"{loccons_final}-{len(facturi) + 1}"),
                    titlu="Factură apă/canal",
                    valoare=_valoare_numerica(factura.get("amount")),
                    moneda=factura.get("currency") or "RON",
                    data_emitere=issue_date,
                    data_scadenta=due_date,
                    stare=factura.get("status"),
                    categorie="factura",
                    id_cont=loccons_final,
                    id_contract=id_contract,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=factura,
                )
            )

        ultima_factura = date_brute.get("last_invoice") or {}
        ultima_plata = date_brute.get("last_payment") or {}
        ultimul_consum = date_brute.get("last_consumption") or {}
        ultimul_index = date_brute.get("last_meter_reading") or {}
        sold = date_brute.get("current_balance") or {}

        valoare_sold = _valoare_numerica(sold.get("value"))
        valoare_ultima_factura = _valoare_numerica(ultima_factura.get("amount"))
        valoare_ultima_plata = _valoare_numerica(ultima_plata.get("amount"))
        valoare_consum = _valoare_numerica(ultimul_consum.get("value"))
        valoare_index = _valoare_numerica(ultimul_index.get("value"))

        consumuri: list[ConsumUtilitate] = [
            ConsumUtilitate("sold_curent", valoare_sold, "RON", id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=sold),
            ConsumUtilitate("current_balance", valoare_sold, "RON", id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=sold),
            ConsumUtilitate("de_plata", max(valoare_sold or 0.0, 0.0), "RON", id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=sold),
            ConsumUtilitate("valoare_ultima_factura", valoare_ultima_factura, "RON", perioada=ultima_factura.get("issue_date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura),
            ConsumUtilitate("id_ultima_factura", str(ultima_factura.get("number") or ""), None, perioada=ultima_factura.get("issue_date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura),
            ConsumUtilitate("serie_ultima_factura", str(ultima_factura.get("series_number") or ""), None, perioada=ultima_factura.get("issue_date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura),
            ConsumUtilitate("numar_facturi", len(date_brute.get("invoices") or []), None, id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute={"facturi": date_brute.get("invoices") or []}),
            ConsumUtilitate("factura_restanta", "da" if (valoare_sold or 0) > 0 else "nu", None, id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=sold),
            ConsumUtilitate("urmatoarea_scadenta", ultima_factura.get("due_date") or "", None, perioada=ultima_factura.get("due_date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura),
            ConsumUtilitate("ultima_plata", valoare_ultima_plata, "RON", perioada=ultima_plata.get("date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
            ConsumUtilitate("last_payment", valoare_ultima_plata, "RON", perioada=ultima_plata.get("date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
            ConsumUtilitate("numar_plati", len(date_brute.get("payments") or []), None, id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute={"plati": date_brute.get("payments") or []}),
            ConsumUtilitate("last_consumption", valoare_consum, ultimul_consum.get("unit") or "m³", perioada=ultimul_consum.get("date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_consum),
            ConsumUtilitate("ultim_consum", valoare_consum, ultimul_consum.get("unit") or "m³", perioada=ultimul_consum.get("date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_consum),
            ConsumUtilitate("last_meter_reading", valoare_index, "m³", perioada=ultimul_index.get("date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_index),
            ConsumUtilitate("ultim_index", valoare_index, "m³", perioada=ultimul_index.get("date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_index),
            ConsumUtilitate("index_contor", valoare_index, "m³", perioada=ultimul_index.get("date"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_index),
            ConsumUtilitate("citire_index_permisa", "da" if ultimul_index.get("is_open") else "nu", None, perioada=ultimul_index.get("period"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_index),
            ConsumUtilitate("perioada_citire", ultimul_index.get("period") or "", None, perioada=ultimul_index.get("period"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_index),
            ConsumUtilitate("zile_pana_citire_index", ultimul_index.get("days_until_open"), "zile", perioada=ultimul_index.get("period"), id_cont=loccons_final, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultimul_index),
        ]

        extra = {
            "premise_label": eticheta,
            "account_id": loccons_final,
            "contract_id": id_contract,
            "current_balance": sold,
            "last_invoice": ultima_factura,
            "last_payment": ultima_plata,
            "last_consumption": ultimul_consum,
            "last_meter_reading": ultimul_index,
            "invoices": date_brute.get("invoices") or [],
            "payments": date_brute.get("payments") or [],
            "contract": contract,
        }
        return cont, facturi, consumuri, extra

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        loccons_id = str(self.optiuni.get(CONF_ACCOUNT_ID) or self.optiuni.get(CONF_CONTRACT_ID) or "principal").strip()
        selector_value = str(self.optiuni.get("apa_brasov_selector_value") or "").strip()
        context_id = str(self.optiuni.get("apa_brasov_context_id") or "").strip() or None
        eticheta = str(self.optiuni.get(CONF_PREMISE_LABEL) or loccons_id or self.utilizator).strip()

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []
        extra_locatii: list[dict[str, Any]] = []

        try:
            if loccons_id == "__all__":
                # Pentru conturile cu mai multe locații folosim o singură sesiune de portal.
                # Rezumatul furnizorului este calculat local din datele locațiilor citite,
                # fără o rundă suplimentară de login/requesturi pentru device-ul principal.
                for contract, date_brute in await self.api.obtine_toate_datele_dashboard(self.utilizator, self.parola):
                    cont, facturi_locatie, consumuri_locatie, extra_locatie = self._construieste_modele_din_date(
                        date_brute=date_brute,
                        loccons_id_initial=contract.loccons_id,
                        eticheta=contract.eticheta,
                    )
                    conturi.append(cont)
                    facturi.extend(facturi_locatie)
                    consumuri.extend(consumuri_locatie)
                    extra_locatii.append(extra_locatie)
            else:
                date_brute = await self.api.obtine_date_dashboard(
                    self.utilizator,
                    self.parola,
                    loccons_id,
                    selector_value,
                    context_id,
                    eticheta,
                )
                cont, facturi_locatie, consumuri_locatie, extra_locatie = self._construieste_modele_din_date(
                    date_brute=date_brute,
                    loccons_id_initial=loccons_id,
                    eticheta=eticheta,
                )
                conturi.append(cont)
                facturi.extend(facturi_locatie)
                consumuri.extend(consumuri_locatie)
                extra_locatii.append(extra_locatie)
        except EroareAutentificareApaBrasov as err:
            raise EroareAutentificare(str(err)) from err
        except EroareApiApaBrasov as err:
            raise EroareConectare(str(err)) from err
        except Exception as err:
            raise EroareParsare(f"Eroare neașteptată la citirea datelor Apă Brașov: {err}") from err

        extra: dict[str, Any] = {
            "premise_label": eticheta,
            "account_id": loccons_id,
            "contract_id": loccons_id,
            "locations": extra_locatii,
            "location_count": len(conturi),
        }
        if extra_locatii:
            # Păstrăm câmpurile vechi pentru compatibilitate cu diagnostic/card.
            extra.update(extra_locatii[0])

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra=extra,
        )
