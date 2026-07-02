from __future__ import annotations

from datetime import date, datetime, timedelta
from html import unescape
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://www.client-hph.ro"
URL_LOGIN = f"{URL_BAZA}/index.php?action=login"
URL_STATUS = f"{URL_BAZA}/index.php?action=status"
URL_CONTRACTE = f"{URL_BAZA}/index.php?action=clienti_ace"
URL_FACTURI = f"{URL_BAZA}/index.php?action=facturi"
URL_FACTURI_POST = f"{URL_BAZA}/index.php?action=facturi&step=2"
URL_CITIRI = f"{URL_BAZA}/index.php?action=citiri_contoare"
URL_CITIRI_POST = f"{URL_BAZA}/index.php?action=citiri_contoare&step=2"
URL_FISA = f"{URL_BAZA}/index.php?action=fisa_financiara"
URL_FISA_POST = f"{URL_BAZA}/index.php?action=fisa_financiara&step=2"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class EroareApiHidroPrahova(Exception):
    pass


class EroareAutentificareHidroPrahova(EroareApiHidroPrahova):
    pass


class EroareConectareHidroPrahova(EroareApiHidroPrahova):
    pass


class EroareRaspunsHidroPrahova(EroareApiHidroPrahova):
    pass


def _debug_hph(etapa: str, **date: Any) -> None:
    try:
        _LOGGER.debug("[HIDRO PRAHOVA DIAG] %s: %s", etapa, json.dumps(date, ensure_ascii=False, default=str))
    except Exception:
        _LOGGER.debug("[HIDRO PRAHOVA DIAG] %s: %s", etapa, date)


def _curata_text(valoare: Any) -> str:
    if valoare is None:
        return ""
    text = unescape(str(valoare))
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _pare_login(pagina: str) -> bool:
    text = _curata_text(pagina).lower()
    if not text:
        return False
    return "autentificare" in text and "utilizator" in text and "parola" in text and "servicii online" in text


async def _citeste_text(raspuns: aiohttp.ClientResponse) -> str:
    continut = await raspuns.read()
    if not continut:
        return ""
    encodari: list[str] = []
    if raspuns.charset:
        encodari.append(raspuns.charset)
    encodari.extend(["utf-8", "windows-1250", "iso-8859-2", "latin-1"])
    incercate: set[str] = set()
    for encoding in encodari:
        enc = encoding.lower().strip()
        if not enc or enc in incercate:
            continue
        incercate.add(enc)
        try:
            return continut.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return continut.decode("utf-8", errors="replace")


def _valoare_numerica(valoare: Any) -> float | None:
    if valoare in (None, ""):
        return None
    text = unescape(str(valoare)).strip()
    text = re.sub(r"[^0-9,\.\-]", "", text)
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


def _data_din_text(text: str | None) -> date | None:
    if not text:
        return None
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", str(text))
    if not match:
        return None
    zi, luna, an = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        return date(an, luna, zi)
    except ValueError:
        return None


def _format_data_portal(valoare: date) -> str:
    return valoare.strftime("%d.%m.%Y")


def _payload_interval(id_client: str, hash_portal: str | None) -> dict[str, str]:
    azi = date.today()
    start = date(max(azi.year - 3, 2000), azi.month, 1)
    return {
        "id_client_ace": id_client,
        "start": _format_data_portal(start),
        "start_zi": f"{start.day:02d}",
        "start_luna": f"{start.month:02d}",
        "start_an": str(start.year),
        "stop": _format_data_portal(azi),
        "stop_zi": f"{azi.day:02d}",
        "stop_luna": f"{azi.month:02d}",
        "stop_an": str(azi.year),
        "hash": hash_portal or "",
    }


def _extrage_campuri_formular(pagina: str) -> dict[str, str]:
    campuri: dict[str, str] = {}
    html = pagina or ""

    for input_match in re.finditer(r"<input\b([^>]*)>", html, flags=re.I | re.S):
        attrs = _atribute_tag(input_match.group(1))
        nume = attrs.get("name")
        if not nume:
            continue
        tip = str(attrs.get("type") or "text").lower()
        if tip in {"button", "image", "reset", "submit"}:
            continue
        campuri[nume] = attrs.get("value", "")

    for select_match in re.finditer(r"<select\b([^>]*)>(.*?)</select>", html, flags=re.I | re.S):
        select_attrs = _atribute_tag(select_match.group(1))
        nume = select_attrs.get("name")
        if not nume:
            continue
        valoare_selectata = ""
        prima_valoare = ""
        for option_match in re.finditer(r"<option\b([^>]*)>(.*?)</option>", select_match.group(2), flags=re.I | re.S):
            option_attrs = _atribute_tag(option_match.group(1))
            valoare = option_attrs.get("value") or _curata_text(option_match.group(2))
            if not prima_valoare:
                prima_valoare = valoare
            if "selected" in option_match.group(1).lower():
                valoare_selectata = valoare
                break
        campuri[nume] = valoare_selectata or prima_valoare

    return campuri


def _payload_interval_din_formular(pagina: str, id_client: str, hash_portal: str | None) -> dict[str, str]:
    # Portalul Hidro Prahova validează strict request-ul pentru pasul 2.
    # Pentru a evita răspunsul „Cerere invalidă”, trimitem câmpurile exact ca în
    # formularul generat de portal, inclusiv hash gol, dacă așa apare în HTML.
    campuri_formular = _extrage_campuri_formular(pagina)
    payload: dict[str, str] = {}

    for cheie, valoare in campuri_formular.items():
        if cheie.lower() in {"id_client_ace", "id_client", "client"}:
            continue
        payload[cheie] = "" if valoare is None else str(valoare)

    fallback = _payload_interval(id_client, "")
    for cheie, valoare in fallback.items():
        if cheie == "hash" and "hash" in payload:
            continue
        payload.setdefault(cheie, valoare)

    payload["id_client_ace"] = id_client

    # Atenție: nu suprascriem hash-ul gol cu valoarea detectată generic în pagină.
    # Testul manual din browser arată că request-ul valid trimite hash="".
    if "hash" not in payload:
        payload["hash"] = hash_portal or ""

    return payload



def _atribute_tag(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r'([a-zA-Z0-9_:-]+)\s*=\s*(["\'])(.*?)\2', tag or "", flags=re.S):
        attrs[match.group(1).lower()] = unescape(match.group(3)).strip()
    for match in re.finditer(r'([a-zA-Z0-9_:-]+)\s*=\s*([^"\'\s>]+)', tag or "", flags=re.S):
        attrs.setdefault(match.group(1).lower(), unescape(match.group(2)).strip())
    return attrs


def _variante_payload_login(pagina: str, utilizator: str, parola: str) -> list[dict[str, str]]:
    variante: list[dict[str, str]] = []
    inputs: list[dict[str, str]] = []
    for match in re.finditer(r'<input\b([^>]*)>', pagina or "", flags=re.I | re.S):
        attrs = _atribute_tag(match.group(1))
        if attrs.get("name"):
            inputs.append(attrs)

    nume_parola = next((item.get("name") for item in inputs if item.get("type", "").lower() == "password"), None)
    nume_utilizator = next(
        (
            item.get("name")
            for item in inputs
            if item.get("name") and item.get("name") != nume_parola and item.get("type", "text").lower() in {"text", "email", ""}
        ),
        None,
    )
    if nume_utilizator and nume_parola:
        payload: dict[str, str] = {}
        for item in inputs:
            tip = item.get("type", "text").lower()
            nume = item.get("name") or ""
            if not nume or tip in {"button", "image", "reset"}:
                continue
            if nume == nume_utilizator:
                payload[nume] = utilizator
            elif nume == nume_parola:
                payload[nume] = parola
            else:
                payload[nume] = item.get("value", "")
        variante.append(payload)

    variante.extend([
        {"utilizator": utilizator, "parola": parola},
        {"username": utilizator, "password": parola},
        {"user": utilizator, "pass": parola},
        {"email": utilizator, "password": parola},
        {"login": utilizator, "password": parola},
        {"u": utilizator, "p": parola},
    ])

    rezultat: list[dict[str, str]] = []
    vazute: set[tuple[tuple[str, str], ...]] = set()
    for payload in variante:
        cheie = tuple(sorted(payload.items()))
        if cheie in vazute:
            continue
        vazute.add(cheie)
        rezultat.append(payload)
    return rezultat

def _extrage_hash(pagina: str) -> str | None:
    for pattern in (
        r'name=["\']hash["\'][^>]*value=["\']([^"\']+)',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']hash["\']',
        r'\bhash=([a-f0-9]{16,64})\b',
        r'\bhash["\']?\s*[:=]\s*["\']([a-f0-9]{16,64})',
    ):
        match = re.search(pattern, pagina or "", flags=re.I)
        if match:
            return unescape(match.group(1)).strip()
    return None


def _adauga_client(
    clienti: dict[str, dict[str, str]],
    id_client: str | None,
    nume: str | None = None,
    hash_portal: str | None = None,
) -> None:
    id_curat = str(id_client or "").strip()
    if not id_curat or id_curat in {"0", "-1"}:
        return
    clienti.setdefault(
        id_curat,
        {
            "id_client": id_curat,
            "nume": (nume or f"Client {id_curat}").strip(),
            "hash": hash_portal or "",
        },
    )
    if nume and clienti[id_curat].get("nume") in {"", f"Client {id_curat}"}:
        clienti[id_curat]["nume"] = nume.strip()
    if hash_portal and not clienti[id_curat].get("hash"):
        clienti[id_curat]["hash"] = hash_portal


def _extrage_clienti(pagina: str) -> list[dict[str, str]]:
    clienti: dict[str, dict[str, str]] = {}
    hash_portal = _extrage_hash(pagina)
    pagina_text = pagina or ""

    # Portalul Simetrix folosește în request câmpul intern id_client_ace.
    # În pagină poate fi afișat și codul public al clientului, mult mai lung.
    # Pentru POST trebuie păstrat id-ul intern din value/input/link, nu codul public afișat.
    for select_match in re.finditer(r'<select\b([^>]*)>(.*?)</select>', pagina_text, flags=re.I | re.S):
        select_attrs = _atribute_tag(select_match.group(1))
        nume_select = " ".join(
            str(select_attrs.get(cheie) or "") for cheie in ("name", "id", "class", "onchange")
        ).lower()
        if "client" not in nume_select and "id_client" not in select_match.group(0).lower():
            continue
        for option_match in re.finditer(r'<option\b([^>]*)>(.*?)</option>', select_match.group(2), flags=re.I | re.S):
            option_attrs = _atribute_tag(option_match.group(1))
            id_client = option_attrs.get("value") or ""
            eticheta = _curata_text(option_match.group(2))
            _adauga_client(clienti, id_client, eticheta or None, hash_portal)

    for input_match in re.finditer(r'<input\b([^>]*)>', pagina_text, flags=re.I | re.S):
        attrs = _atribute_tag(input_match.group(1))
        nume = str(attrs.get("name") or attrs.get("id") or "").lower()
        valoare = attrs.get("value") or ""
        if "client" in nume and valoare:
            _adauga_client(clienti, valoare, None, hash_portal)

    for link_match in re.finditer(r'(?:id_client_ace|id_client|client)=(\d+)', pagina_text, flags=re.I):
        _adauga_client(clienti, link_match.group(1), None, hash_portal)

    for js_match in re.finditer(
        r'(?:id_client_ace|id_client|client)["\']?\s*[:=]\s*["\']?(\d+)',
        pagina_text,
        flags=re.I,
    ):
        _adauga_client(clienti, js_match.group(1), None, hash_portal)

    if clienti:
        return list(clienti.values())

    # Fallback doar dacă nu există niciun id tehnic în HTML. Acesta poate întoarce
    # codul public al clientului și este păstrat strict ca soluție de avarie.
    text = _curata_text(pagina_text)
    for id_client in re.findall(r"\b(?:client|cod client|id client)\D{0,20}(\d{3,})\b", text, flags=re.I):
        _adauga_client(clienti, id_client, f"Client {id_client}", hash_portal)

    return list(clienti.values())



def _extrage_candidati_id_tehnic(*pagini: str) -> list[str]:
    candidati: list[str] = []

    def adauga(valoare: str | None) -> None:
        text = str(valoare or "").strip()
        if not re.fullmatch(r"\d{3,8}", text):
            return
        if text in {"000", "001", "100", "200", "404", "500"}:
            return
        if text.startswith("20") and len(text) == 4:
            return
        if text not in candidati:
            candidati.append(text)

    for pagina in pagini:
        html = pagina or ""
        for tag_match in re.finditer(r"<(?:input|option|button|select)\b([^>]*)>", html, flags=re.I | re.S):
            attrs = _atribute_tag(tag_match.group(1))
            for cheie in ("value", "data-value", "data-id", "data-key", "rel"):
                adauga(attrs.get(cheie))

        for pattern in (
            r"(?:id_client_ace|id_client|client_ace|client)\D{0,40}(\d{3,8})",
            r"(\d{3,8})\D{0,40}(?:id_client_ace|id_client|client_ace)",
            r"(?:combo|autocombo|valoare|value|id)\D{0,40}(\d{3,8})",
            r"[\[({,]\s*[\"']?(\d{3,8})[\"']?\s*[,|;:]",
        ):
            for match in re.finditer(pattern, html, flags=re.I | re.S):
                adauga(match.group(1))

    return candidati


def _este_raspuns_fisa_valid(pagina: str) -> bool:
    text = _curata_text(pagina).lower()
    if not text:
        return False
    if "cerere invalida" in text or "adresa accesata este invalida" in text:
        return False
    return "sold final" in text or "sold initial" in text or "facturat" in text or "incasat" in text

def _extrage_rezumat_fisa(pagina: str) -> dict[str, Any]:
    rezultat: dict[str, Any] = {}
    text = _curata_text(pagina)
    match_client = re.search(r"Client\s+(.+?)\s+Interval\s+", text, flags=re.I)
    if match_client:
        rezultat["nume_client"] = match_client.group(1).strip()
    match_interval = re.search(r"Interval\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\s*-\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})", text, flags=re.I)
    if match_interval:
        rezultat["interval"] = match_interval.group(1).strip()
    match_initial = re.search(r"Sold initial\s+\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\s+([-0-9.,]+)", text, flags=re.I)
    if match_initial:
        rezultat["sold_initial"] = _valoare_numerica(match_initial.group(1))
    match_final = re.search(r"Sold final\s+\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\s+([-0-9.,]+)", text, flags=re.I)
    if match_final:
        rezultat["sold_final"] = _valoare_numerica(match_final.group(1))
    match_facturat = re.search(r"Facturat\s+\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\s*-\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\s+([-0-9.,]+)", text, flags=re.I)
    if match_facturat:
        rezultat["total_facturat"] = _valoare_numerica(match_facturat.group(1))
    match_incasat = re.search(r"Incasat\s+\d{1,2}[.\-/]\d{1,2}[.\-/]\d{1,4}\s*-\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\s+([-0-9.,]+)", text, flags=re.I)
    if match_incasat:
        rezultat["total_incasat"] = _valoare_numerica(match_incasat.group(1))
    return rezultat


def _extrage_facturi_fisa(pagina: str, id_cont: str) -> list[dict[str, Any]]:
    facturi: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<tr[^>]*>\s*<td[^>]*rowspan=["\']?3[^>]*>(?P<gestiune>.*?)</td>\s*'
        r'<td[^>]*rowspan=["\']?3[^>]*>(?P<numar>.*?)</td>\s*'
        r'<td[^>]*rowspan=["\']?3[^>]*>(?P<data>.*?)</td>\s*'
        r'<td[^>]*rowspan=["\']?3[^>]*>(?P<total>.*?)</td>\s*'
        r'(?P<prima>.*?)</tr>\s*<tr[^>]*>(?P<incasat>.*?)</tr>\s*<tr[^>]*>(?P<restant>.*?)</tr>',
        flags=re.I | re.S,
    )
    for match in pattern.finditer(pagina or ""):
        gestiune_html = match.group("gestiune") or ""
        link_match = re.search(r'href=["\']([^"\']*copie_factura[^"\']+)', gestiune_html, flags=re.I)
        link_pdf = urljoin(URL_BAZA + "/", unescape(link_match.group(1))) if link_match else None
        id_pdf_match = re.search(r"id_factura=(\d+)", link_pdf or "")

        numar = _curata_text(match.group("numar"))
        data_emitere = _data_din_text(_curata_text(match.group("data")))
        total = _valoare_numerica(_curata_text(match.group("total")))
        incasat_text = _curata_text(match.group("incasat"))
        restant_text = _curata_text(match.group("restant"))
        incasat_val = None
        restant_val = None
        match_incasat = re.search(r"INCASAT\s+([-0-9.,]+)", incasat_text, flags=re.I)
        if match_incasat:
            incasat_val = _valoare_numerica(match_incasat.group(1))
        match_restant = re.search(r"RESTANT\s+([-0-9.,]+)", restant_text, flags=re.I)
        if match_restant:
            restant_val = _valoare_numerica(match_restant.group(1))

        if not numar and not id_pdf_match:
            continue
        facturi.append(
            {
                "id_factura": id_pdf_match.group(1) if id_pdf_match else numar,
                "numar_factura": numar,
                "data_emitere": data_emitere,
                "valoare": total,
                "incasat": incasat_val,
                "restant": restant_val,
                "stare": "neachitata" if restant_val and restant_val > 0.01 else "platita",
                "link_pdf": link_pdf,
                "id_cont": id_cont,
            }
        )
    facturi.sort(key=lambda item: item.get("data_emitere") or date.min, reverse=True)
    return facturi



def _extrage_facturi_emise(pagina: str, id_cont: str) -> list[dict[str, Any]]:
    facturi: list[dict[str, Any]] = []
    for rand_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", pagina or "", flags=re.I | re.S):
        rand_html = rand_match.group(1) or ""
        celule = re.findall(r"<td[^>]*>(.*?)</td>", rand_html, flags=re.I | re.S)
        if len(celule) < 7:
            continue

        gestiune = _curata_text(celule[0])
        numar = _curata_text(celule[1])
        data_emitere = _data_din_text(_curata_text(celule[2]))
        total_valoare = _valoare_numerica(_curata_text(celule[3]))
        total_tva = _valoare_numerica(_curata_text(celule[4]))
        total_factura = _valoare_numerica(_curata_text(celule[5]))
        rest_plata = _valoare_numerica(_curata_text(celule[6]))

        if not numar or not re.search(r"\d{4,}", numar) or data_emitere is None:
            continue

        link_match = re.search(r'href=["\']([^"\']*(?:copie_factura|id_factura)[^"\']*)', rand_html, flags=re.I)
        link_pdf = urljoin(URL_BAZA + "/", unescape(link_match.group(1))) if link_match else None
        id_pdf_match = re.search(r"id_factura=(\d+)", link_pdf or "")

        facturi.append(
            {
                "id_factura": id_pdf_match.group(1) if id_pdf_match else numar,
                "numar_factura": numar,
                "data_emitere": data_emitere,
                "valoare": total_factura if total_factura is not None else total_valoare,
                "valoare_fara_tva": total_valoare,
                "tva": total_tva,
                "incasat": None if rest_plata is None or total_factura is None else max(total_factura - rest_plata, 0.0),
                "restant": rest_plata,
                "stare": "neachitata" if rest_plata and rest_plata > 0.01 else "platita",
                "link_pdf": link_pdf,
                "id_cont": id_cont,
                "gestiune": gestiune,
                "sursa": "facturi_emise",
            }
        )
    facturi.sort(key=lambda item: item.get("data_emitere") or date.min, reverse=True)
    return facturi


def _este_raspuns_facturi_valid(pagina: str) -> bool:
    text = _curata_text(pagina).lower()
    if not text:
        return False
    if "cerere invalida" in text or "adresa accesata este invalida" in text:
        return False
    return "facturi emise" in text and "nr factura" in text and "rest plata" in text

def _extrage_contoare(pagina: str, id_cont: str) -> list[dict[str, Any]]:
    contoare: list[dict[str, Any]] = []
    vazute: set[tuple[str, float]] = set()

    def adauga(serie: str | None, index: float | None, consum: float | None = None) -> None:
        serie_curata = re.sub(r"\s+", "", str(serie or "").strip())
        if not serie_curata or index is None:
            return
        # Evităm codurile publice de client / contract și păstrăm doar serii plauzibile de contor.
        if not re.fullmatch(r"[A-Z0-9\-/]{4,}", serie_curata, flags=re.I):
            return
        if serie_curata.isdigit() and len(serie_curata) > 10:
            return
        cheie = (serie_curata, float(index))
        if cheie in vazute:
            return
        vazute.add(cheie)
        contoare.append({"serie": serie_curata, "index": index, "consum": consum, "id_cont": id_cont})

    for rand_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", pagina or "", flags=re.I | re.S):
        rand_html = rand_match.group(1) or ""
        celule = re.findall(r"<td[^>]*>(.*?)</td>", rand_html, flags=re.I | re.S)
        if len(celule) < 2:
            continue
        valori = [_curata_text(celula) for celula in celule]
        valori = [valoare for valoare in valori if valoare not in {"", "ÎNAPOI", "INAPOI"}]
        if len(valori) < 2:
            continue

        # Tabelul Hidro Prahova are, de regulă:
        # Serie | Index vechi | Index nou | Consum | Observații
        serie = valori[0]
        index = _valoare_numerica(valori[1])
        consum = _valoare_numerica(valori[3]) if len(valori) > 3 else None
        adauga(serie, index, consum)

    if contoare:
        return contoare

    text = _curata_text(pagina)
    for match in re.finditer(
        r"(?:serie\s+index\s+vechi\s+index\s+nou\s+consum\s+observa(?:tii|ții)\s+)?([A-Z0-9\-/]{4,})\s+([0-9]+(?:[.,][0-9]+)?)",
        text,
        flags=re.I,
    ):
        serie = match.group(1).strip()
        index = _valoare_numerica(match.group(2))
        adauga(serie, index)

    return contoare



def _extrage_status_citire(pagina: str) -> dict[str, Any]:
    """Extrage perioada de transmitere index din pagina principală de status.

    Portalul Hidro Prahova afișează un mesaj de forma „Indecsii se declara in 9 zile”.
    Nu oferă data exactă, deci calculăm data următoarei ferestre pe baza zilei curente.
    """
    text = _curata_text(pagina)
    text_lower = text.lower()
    rezultat: dict[str, Any] = {
        "citire_index_permisa": None,
        "zile_pana_citire_index": None,
        "data_urmatoare_citire_index": None,
        "perioada_citire": None,
    }

    match_zile = re.search(
        r"(?:indic(?:ii|ele)|indecsii|index(?:ii|ele))\s+se\s+declar[ăa]\s+(?:in|în)\s+(\d+)\s+zil(?:e|a)",
        text_lower,
        flags=re.I,
    )
    if match_zile:
        zile = int(match_zile.group(1))
        data_deschidere = date.today() + timedelta(days=zile)
        rezultat["zile_pana_citire_index"] = zile
        rezultat["data_urmatoare_citire_index"] = data_deschidere
        rezultat["citire_index_permisa"] = zile <= 0
        rezultat["perioada_citire"] = (
            "Disponibilă astăzi" if zile <= 0 else f"Disponibilă din {_format_data_portal(data_deschidere)}"
        )
        return rezultat

    if re.search(r"(?:indic(?:ii|ele)|indecsii|index(?:ii|ele)).{0,80}(?:se\s+pot\s+declara|pot\s+fi\s+declara|declara\s+acum|declarare\s+activ)", text_lower, flags=re.I):
        rezultat["citire_index_permisa"] = True
        rezultat["zile_pana_citire_index"] = 0
        rezultat["data_urmatoare_citire_index"] = date.today()
        rezultat["perioada_citire"] = "Disponibilă astăzi"
        return rezultat

    if re.search(r"(?:indic|indecs|index)", text_lower, flags=re.I) and re.search(r"nu\s+se\s+(?:pot\s+)?declara", text_lower, flags=re.I):
        rezultat["citire_index_permisa"] = False
        rezultat["perioada_citire"] = "Indisponibilă"

    return rezultat


class ClientApiHidroPrahova:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._autentificat = False

    def _headers(self, *, referer: str | None = None) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": USER_AGENT,
            "Referer": referer or URL_LOGIN,
        }

    async def _get(self, url: str, *, referer: str | None = None, necesita_login: bool = True) -> str:
        if necesita_login and not self._autentificat:
            await self.async_login()
        try:
            async with self._sesiune.get(url, headers=self._headers(referer=referer), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                text = await _citeste_text(raspuns)
                if raspuns.status >= 400:
                    raise EroareRaspunsHidroPrahova(f"Hidro Prahova a returnat HTTP {raspuns.status} pentru {url}")
                if necesita_login and _pare_login(text):
                    self._autentificat = False
                    raise EroareAutentificareHidroPrahova("Sesiunea Hidro Prahova a expirat")
                return text
        except EroareApiHidroPrahova:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareHidroPrahova(f"Eroare de conectare la Hidro Prahova: {err}") from err
        except TimeoutError as err:
            raise EroareConectareHidroPrahova("Timeout la Hidro Prahova") from err

    async def _post(self, url: str, data: dict[str, str], *, referer: str | None = None, necesita_login: bool = True) -> str:
        if necesita_login and not self._autentificat:
            await self.async_login()
        try:
            async with self._sesiune.post(
                url,
                headers={**self._headers(referer=referer), "Content-Type": "application/x-www-form-urlencoded", "Origin": URL_BAZA},
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await _citeste_text(raspuns)
                if raspuns.status >= 400:
                    raise EroareRaspunsHidroPrahova(f"Hidro Prahova a returnat HTTP {raspuns.status} pentru {url}")
                if necesita_login and _pare_login(text):
                    self._autentificat = False
                    raise EroareAutentificareHidroPrahova("Sesiunea Hidro Prahova a expirat")
                return text
        except EroareApiHidroPrahova:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareHidroPrahova(f"Eroare de conectare la Hidro Prahova: {err}") from err
        except TimeoutError as err:
            raise EroareConectareHidroPrahova("Timeout la Hidro Prahova") from err

    async def async_login(self) -> None:
        login_html = await self._get(URL_LOGIN, necesita_login=False)
        if not login_html:
            raise EroareConectareHidroPrahova("Pagina de login Hidro Prahova nu a putut fi citită")

        for payload in _variante_payload_login(login_html, self._utilizator, self._parola):
            payload_final = {**payload, "bcontinuare": "CONTINUA", "submit": "CONTINUA"}
            pagini_incercate: list[tuple[str, str]] = []
            try:
                pagini_incercate.append(("POST", await self._post(URL_STATUS, payload_final, referer=URL_LOGIN, necesita_login=False)))
            except EroareApiHidroPrahova:
                pass

            try:
                async with self._sesiune.get(
                    URL_STATUS,
                    headers=self._headers(referer=URL_LOGIN),
                    params=payload_final,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as raspuns:
                    pagini_incercate.append(("GET", await _citeste_text(raspuns)))
            except (aiohttp.ClientError, TimeoutError):
                pass

            for metoda, pagina in pagini_incercate:
                _debug_hph(
                    "login incercare",
                    metoda=metoda,
                    campuri=[cheie for cheie in payload.keys()],
                    status_page_len=len(pagina or ""),
                    pare_login=_pare_login(pagina),
                    are_contracte="clienti_ace" in (pagina or ""),
                )
                if pagina and not _pare_login(pagina) and ("clienti_ace" in pagina or "logout" in pagina or "Facturi" in _curata_text(pagina)):
                    self._autentificat = True
                    return

        raise EroareAutentificareHidroPrahova("Credentialele Hidro Prahova nu au fost acceptate sau formularul de login s-a schimbat")

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        pagina_contracte = await self._get(URL_CONTRACTE, referer=URL_STATUS)
        pagina_facturi = await self._get(URL_FACTURI, referer=URL_CONTRACTE)
        pagina_citiri = await self._get(URL_CITIRI, referer=URL_FACTURI)
        pagina_fisa = await self._get(URL_FISA, referer=URL_CITIRI)
        clienti = (
            _extrage_clienti(pagina_facturi)
            or _extrage_clienti(pagina_citiri)
            or _extrage_clienti(pagina_fisa)
            or _extrage_clienti(pagina_contracte)
        )
        return {"clienti": clienti, "pagina": pagina_contracte}

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        pagina_status = await self._get(URL_STATUS, referer=URL_LOGIN)
        status_citire = _extrage_status_citire(pagina_status)
        pagina_contracte = await self._get(URL_CONTRACTE, referer=URL_STATUS)
        pagina_facturi_form = await self._get(URL_FACTURI, referer=URL_CONTRACTE)
        pagina_citiri_form = await self._get(URL_CITIRI, referer=URL_FACTURI)
        pagina_fisa_form = await self._get(URL_FISA, referer=URL_CITIRI)
        clienti = (
            _extrage_clienti(pagina_facturi_form)
            or _extrage_clienti(pagina_citiri_form)
            or _extrage_clienti(pagina_fisa_form)
            or _extrage_clienti(pagina_contracte)
        )
        hash_fisa = (
            _extrage_hash(pagina_fisa_form)
            or _extrage_hash(pagina_facturi_form)
            or _extrage_hash(pagina_citiri_form)
            or _extrage_hash(pagina_contracte)
        )
        candidati_id = _extrage_candidati_id_tehnic(
            pagina_facturi_form,
            pagina_citiri_form,
            pagina_fisa_form,
            pagina_contracte,
        )

        _debug_hph(
            "clienti detectati",
            numar=len(clienti),
            clienti=[{
                "id_client": item.get("id_client"),
                "nume": item.get("nume"),
                "are_hash": bool(item.get("hash") or hash_fisa),
            } for item in clienti],
            candidati_id=candidati_id[:20],
            pagina_contracte_len=len(pagina_contracte or ""),
            pagina_facturi_form_len=len(pagina_facturi_form or ""),
            pagina_citiri_form_len=len(pagina_citiri_form or ""),
            pagina_fisa_form_len=len(pagina_fisa_form or ""),
        )

        if not clienti and not candidati_id:
            raise EroareRaspunsHidroPrahova("Nu am găsit niciun client/contract în portalul Hidro Prahova")

        toate_facturile: list[dict[str, Any]] = []
        toate_conturile: list[dict[str, Any]] = []
        toate_contoarele: list[dict[str, Any]] = []
        pagini: dict[str, str] = {
            "status": pagina_status,
            "contracte": pagina_contracte,
            "facturi_form": pagina_facturi_form,
            "citiri_form": pagina_citiri_form,
            "fisa_form": pagina_fisa_form,
        }
        _debug_hph(
            "status citire",
            status_len=len(pagina_status or ""),
            citire_index_permisa=status_citire.get("citire_index_permisa"),
            zile_pana_citire_index=status_citire.get("zile_pana_citire_index"),
            data_urmatoare_citire_index=status_citire.get("data_urmatoare_citire_index"),
            perioada_citire=status_citire.get("perioada_citire"),
        )
        fise_validate: dict[str, str] = {}

        ids_clienti_existenti = {str(item.get("id_client") or "").strip() for item in clienti}
        for candidat in candidati_id:
            if candidat in ids_clienti_existenti:
                continue
            try:
                fisa_test = await self._post(URL_FISA_POST, _payload_interval_din_formular(pagina_fisa_form, candidat, hash_fisa), referer=URL_FISA)
            except EroareApiHidroPrahova:
                continue
            if _este_raspuns_fisa_valid(fisa_test):
                rezumat_test = _extrage_rezumat_fisa(fisa_test)
                clienti.insert(0, {
                    "id_client": candidat,
                    "nume": str(rezumat_test.get("nume_client") or f"Client {candidat}"),
                    "hash": hash_fisa or "",
                })
                fise_validate[candidat] = fisa_test
                ids_clienti_existenti.add(candidat)
                _debug_hph("client validat prin proba", id_client=candidat, fisa_len=len(fisa_test or ""))

        clienti = [item for item in clienti if re.fullmatch(r"\d{3,8}", str(item.get("id_client") or "").strip())]

        if not clienti:
            raise EroareRaspunsHidroPrahova("Am găsit contul în portal, dar nu am putut identifica ID-ul tehnic Hidro Prahova necesar pentru fișa financiară")

        for client in clienti:
            id_client = str(client.get("id_client") or "").strip()
            hash_client = str(client.get("hash") or hash_fisa or "").strip()
            if not id_client:
                continue

            payload = _payload_interval(id_client, hash_client)
            facturi_emise: list[dict[str, Any]] = []
            try:
                facturi_form_actual = await self._get(URL_FACTURI, referer=URL_CONTRACTE)
                hash_facturi = _extrage_hash(facturi_form_actual) or hash_client
                payload_facturi = _payload_interval_din_formular(facturi_form_actual, id_client, hash_facturi)
                facturi_html = await self._post(URL_FACTURI_POST, payload_facturi, referer=URL_FACTURI)
                pagini[f"facturi_{id_client}"] = facturi_html
                facturi_emise = _extrage_facturi_emise(facturi_html, id_client)
                _debug_hph(
                    "facturi emise",
                    id_client=id_client,
                    facturi_len=len(facturi_html or ""),
                    hash_prefix=(hash_facturi or "")[:8],
                    raspuns_valid=_este_raspuns_facturi_valid(facturi_html),
                    numar_facturi=len(facturi_emise),
                    mostra_text=_curata_text(facturi_html)[:300] if not facturi_emise else "",
                )
            except EroareApiHidroPrahova:
                _LOGGER.debug("Nu s-au putut citi facturile emise Hidro Prahova pentru clientul %s", id_client, exc_info=True)

            fisa_html = ""
            rezumat: dict[str, Any] = {}
            facturi_fisa: list[dict[str, Any]] = []
            try:
                fisa_form_actual = await self._get(URL_FISA, referer=URL_FACTURI_POST if facturi_emise else URL_CITIRI)
                hash_fisa_actual = _extrage_hash(fisa_form_actual) or hash_client
                payload_fisa = _payload_interval_din_formular(fisa_form_actual, id_client, hash_fisa_actual)
                fisa_html = fise_validate.get(id_client) or await self._post(URL_FISA_POST, payload_fisa, referer=URL_FISA)
                pagini[f"fisa_{id_client}"] = fisa_html
                if _este_raspuns_fisa_valid(fisa_html):
                    rezumat = _extrage_rezumat_fisa(fisa_html)
                    facturi_fisa = _extrage_facturi_fisa(fisa_html, id_client)
                    _debug_hph(
                        "fisa financiara",
                        id_client=id_client,
                        fisa_len=len(fisa_html or ""),
                        hash_prefix=(hash_fisa_actual or "")[:8],
                        are_tabel_facturi="FACTURI" in (fisa_html or "") or "Facturi" in (fisa_html or ""),
                        numar_facturi=len(facturi_fisa),
                        sold_final=rezumat.get("sold_final"),
                        total_facturat=rezumat.get("total_facturat"),
                        mostra_text=_curata_text(fisa_html)[:300] if not facturi_fisa else "",
                    )
                else:
                    _debug_hph(
                        "fisa financiara invalida",
                        id_client=id_client,
                        fisa_len=len(fisa_html or ""),
                        hash_prefix=(hash_fisa_actual or "")[:8],
                        mostra_text=_curata_text(fisa_html)[:300],
                    )
            except EroareApiHidroPrahova:
                _LOGGER.debug("Nu s-a putut citi fisa financiara Hidro Prahova pentru clientul %s", id_client, exc_info=True)

            facturi = facturi_fisa or facturi_emise
            if not facturi and not rezumat:
                continue

            nume_client = str(rezumat.get("nume_client") or client.get("nume") or f"Client {id_client}").strip()
            toate_facturile.extend(facturi)

            try:
                citiri_form = await self._get(URL_CITIRI, referer=URL_FISA_POST if fisa_html else URL_FACTURI_POST)
                hash_citiri = _extrage_hash(citiri_form) or hash_client
                payload_citiri = _payload_interval_din_formular(citiri_form, id_client, hash_citiri)
                citiri_html = await self._post(URL_CITIRI_POST, payload_citiri, referer=URL_CITIRI)
                pagini[f"citiri_{id_client}"] = citiri_html
                contoare_client_citite = _extrage_contoare(citiri_html, id_client)
                toate_contoarele.extend(contoare_client_citite)
                _debug_hph(
                    "citiri contoare",
                    id_client=id_client,
                    citiri_len=len(citiri_html or ""),
                    hash_prefix=(hash_citiri or "")[:8],
                    numar_contoare=len(contoare_client_citite),
                    contoare=contoare_client_citite[:5],
                    mostra_text=_curata_text(citiri_html)[:300] if not contoare_client_citite else "",
                )
            except EroareApiHidroPrahova:
                _LOGGER.debug("Nu s-au putut citi contoarele Hidro Prahova pentru clientul %s", id_client, exc_info=True)

            sold = rezumat.get("sold_final")
            if sold is None:
                sold = sum(float(item.get("restant") or 0.0) for item in facturi)
            restante = [item for item in facturi if (item.get("restant") or 0) > 0.01]
            ultima_factura = facturi[0] if facturi else None
            ultima_plata = _ultima_plata_din_facturi(facturi)
            cont = {
                "id_cont": id_client,
                "id_contract": id_client,
                "nume": nume_client,
                "adresa": None,
                "sold_final": sold,
                "sold_curent": sold,
                "total_neachitat": max(float(sold or 0.0), 0.0),
                "de_plata": max(float(sold or 0.0), 0.0),
                "numar_facturi": len(facturi),
                "numar_facturi_neachitate": len(restante),
                "factura_restanta": bool(restante),
                "valoare_ultima_factura": ultima_factura.get("valoare") if ultima_factura else None,
                "id_ultima_factura": ultima_factura.get("numar_factura") if ultima_factura else None,
                "data_ultima_factura": ultima_factura.get("data_emitere") if ultima_factura else None,
                "urmatoarea_scadenta": _scadenta_estimativa(restante[0] if restante else ultima_factura),
                "data_ultima_plata": ultima_plata.get("data") if ultima_plata else None,
                "valoare_ultima_plata": ultima_plata.get("valoare") if ultima_plata else None,
                "numar_plati": sum(1 for item in facturi if item.get("incasat") not in (None, 0)),
                "numar_contoare": len([c for c in toate_contoarele if c.get("id_cont") == id_client]),
                "index_contor": None,
                "citire_index_permisa": status_citire.get("citire_index_permisa"),
                "zile_pana_citire_index": status_citire.get("zile_pana_citire_index"),
                "data_urmatoare_citire_index": status_citire.get("data_urmatoare_citire_index"),
                "perioada_citire": status_citire.get("perioada_citire"),
                "rezumat_financiar": rezumat,
            }
            contoare_client = [c for c in toate_contoarele if c.get("id_cont") == id_client]
            if contoare_client:
                cont["index_contor"] = contoare_client[0].get("index")
            toate_conturile.append(cont)

        return {"conturi": toate_conturile, "facturi": toate_facturile, "contoare": toate_contoarele, "pagini": pagini}


def _scadenta_estimativa(factura: dict[str, Any] | None) -> date | None:
    if not factura:
        return None
    data_emitere = factura.get("data_emitere")
    if isinstance(data_emitere, date):
        return data_emitere + timedelta(days=15)
    return None


def _ultima_plata_din_facturi(facturi: list[dict[str, Any]]) -> dict[str, Any] | None:
    plati: list[dict[str, Any]] = []
    for item in facturi:
        valoare = item.get("incasat")
        if valoare in (None, 0):
            continue
        plati.append({"data": item.get("data_emitere"), "valoare": valoare})
    plati.sort(key=lambda item: item.get("data") or date.min, reverse=True)
    return plati[0] if plati else None


def _valoare_cont(cont: dict[str, Any], cheie: str) -> Any:
    valoare = cont.get(cheie)
    if isinstance(valoare, date):
        return valoare.isoformat()
    return valoare


class ClientFurnizorHidroPrahova(ClientFurnizor):
    cheie_furnizor = "hidro_prahova"
    nume_prietenos = "Hidro Prahova"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiHidroPrahova(self.sesiune, self.utilizator, self.parola)
        try:
            rezultat = await api.async_validate_credentials()
        except EroareAutentificareHidroPrahova as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareHidroPrahova as err:
            raise EroareConectare(str(err)) from err
        except EroareApiHidroPrahova as err:
            raise EroareParsare(str(err)) from err

        clienti = rezultat.get("clienti") or []
        if clienti:
            return "_".join(str(item.get("id_client") or "").strip() for item in clienti if item.get("id_client")) or self.utilizator.strip().lower()
        return self.utilizator.strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiHidroPrahova(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificareHidroPrahova as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareHidroPrahova as err:
            raise EroareConectare(str(err)) from err
        except EroareApiHidroPrahova as err:
            raise EroareParsare(str(err)) from err

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute)
        consumuri = self._mapeaza_consumuri(date_brute, conturi)
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={
                "numar_conturi": len(conturi),
                "numar_facturi": len(facturi),
                "numar_contoare": len(date_brute.get("contoare") or []),
            },
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        rezultat: list[ContUtilitate] = []
        for item in date_brute.get("conturi") or []:
            id_cont = str(item.get("id_cont") or "client").strip()
            rezultat.append(
                ContUtilitate(
                    id_cont=id_cont,
                    nume=str(item.get("nume") or f"Client {id_cont}").strip(),
                    tip_cont="apa_canal",
                    id_contract=str(item.get("id_contract") or id_cont),
                    adresa=item.get("adresa"),
                    stare="activ",
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=dict(item),
                )
            )
        return rezultat

    def _mapeaza_facturi(self, date_brute: dict[str, Any]) -> list[FacturaUtilitate]:
        rezultat: list[FacturaUtilitate] = []
        for item in date_brute.get("facturi") or []:
            id_factura = str(item.get("id_factura") or item.get("numar_factura") or "factura").strip()
            numar = str(item.get("numar_factura") or id_factura).strip()
            rezultat.append(
                FacturaUtilitate(
                    id_factura=id_factura,
                    titlu=f"Factura {numar}",
                    valoare=item.get("restant") if (item.get("restant") or 0) > 0.01 else item.get("valoare"),
                    moneda="RON",
                    data_emitere=item.get("data_emitere"),
                    data_scadenta=_scadenta_estimativa(item),
                    stare=item.get("stare") or "necunoscuta",
                    categorie="apa_canal",
                    id_cont=item.get("id_cont"),
                    id_contract=item.get("id_cont"),
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=dict(item),
                )
            )
        rezultat.sort(key=lambda factura: factura.data_emitere or date.min, reverse=True)
        return rezultat

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[ConsumUtilitate]:
        rezultat: list[ConsumUtilitate] = []
        conturi_brute = {str(item.get("id_cont") or ""): item for item in date_brute.get("conturi") or []}
        for cont in conturi:
            raw = conturi_brute.get(cont.id_cont, {})
            for cheie, unitate in (
                ("de_plata", "RON"),
                ("sold_curent", "RON"),
                ("total_neachitat", "RON"),
                ("sold_final", "RON"),
                ("valoare_ultima_factura", "RON"),
                ("id_ultima_factura", None),
                ("data_ultima_factura", None),
                ("urmatoarea_scadenta", None),
                ("factura_restanta", None),
                ("numar_facturi", None),
                ("numar_facturi_neachitate", None),
                ("numar_plati", None),
                ("data_ultima_plata", None),
                ("valoare_ultima_plata", "RON"),
                ("numar_contoare", None),
                ("index_contor", "m³"),
                ("citire_index_permisa", None),
                ("zile_pana_citire_index", "zile"),
                ("data_urmatoare_citire_index", None),
                ("perioada_citire", None),
            ):
                rezultat.append(
                    ConsumUtilitate(
                        cheie=cheie,
                        valoare=_valoare_cont(raw, cheie),
                        unitate=unitate,
                        id_cont=cont.id_cont,
                        tip_utilitate="apa",
                        tip_serviciu="apa_canal",
                        date_brute={"sursa": "hidro_prahova"},
                    )
                )
        return rezultat
