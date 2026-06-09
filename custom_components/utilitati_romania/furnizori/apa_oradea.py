from __future__ import annotations

from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
import logging
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://plataonline.apaoradea.ro"
URL_LOGIN = f"{URL_BAZA}/"
URL_PROCESARE_LOGIN = f"{URL_BAZA}/index.php"
URL_CONT = f"{URL_BAZA}/account.php"
URL_CONTRACTE = f"{URL_BAZA}/contract-list.php"
URL_FACTURI = f"{URL_BAZA}/invoice-list.php"
URL_PLATI = f"{URL_BAZA}/payment-list.php"
URL_CONTOARE = f"{URL_BAZA}/contor-list.php"
URL_CITIRE = f"{URL_BAZA}/contor-reading.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class EroareApiApaOradea(Exception):
    pass


class EroareAutentificareApaOradea(EroareApiApaOradea):
    pass


class EroareConectareApaOradea(EroareApiApaOradea):
    pass


class EroareRaspunsApaOradea(EroareApiApaOradea):
    pass


class _ParserTabele(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.links: list[dict[str, str]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []
        self._link_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        tag = tag.lower()
        if tag == "a":
            self._link_stack.append({"href": attrs_dict.get("href", ""), "text": ""})
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif tag == "tr" and self._in_table:
            self._in_row = True
            self._current_row = []
        elif tag in {"td", "th"} and self._in_row:
            self._in_cell = True
            self._current_cell = []
        elif tag == "br" and self._in_cell:
            self._current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._link_stack:
            link = self._link_stack.pop()
            link["text"] = _curata_text(link.get("text", ""))
            if link.get("href"):
                self.links.append(link)
        elif tag in {"td", "th"} and self._in_cell:
            self._current_row.append(_curata_text(" ".join(self._current_cell)))
            self._current_cell = []
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if any(cell for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = []
            self._in_row = False
        elif tag == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = []
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell.append(data)
        if self._link_stack:
            self._link_stack[-1]["text"] += data


class ClientApiApaOradea:
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

    async def async_login(self) -> None:
        try:
            async with self._sesiune.get(URL_LOGIN, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                await raspuns.text()
                if raspuns.status >= 400:
                    raise EroareConectareApaOradea(f"Apa Oradea a returnat HTTP {raspuns.status} la pagina de login")

            async with self._sesiune.post(
                URL_PROCESARE_LOGIN,
                headers={**self._headers(referer=URL_LOGIN), "Content-Type": "application/x-www-form-urlencoded", "Origin": URL_BAZA},
                data={"username": self._utilizator, "password": self._parola},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status not in (200, 302, 303):
                    raise EroareAutentificareApaOradea(f"Autentificare Apa Oradea eșuată: HTTP {raspuns.status}")
                if raspuns.status == 200 and _pare_pagina_login(text):
                    raise EroareAutentificareApaOradea("Credentialele Apa Oradea nu au fost acceptate")

            pagina_cont = await self.async_get_page(URL_CONT, necesita_login=False)
            if _pare_pagina_login(pagina_cont):
                raise EroareAutentificareApaOradea("Sesiunea Apa Oradea nu a fost creată corect")
            self._autentificat = True
        except EroareApiApaOradea:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareApaOradea(f"Eroare de conectare la Apa Oradea: {err}") from err
        except TimeoutError as err:
            raise EroareConectareApaOradea("Timeout la Apa Oradea") from err

    async def async_get_page(self, url: str, *, necesita_login: bool = True) -> str:
        if necesita_login and not self._autentificat:
            await self.async_login()
        try:
            async with self._sesiune.get(url, headers=self._headers(referer=URL_CONT), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403) or _pare_pagina_login(text):
                    if necesita_login:
                        self._autentificat = False
                        await self.async_login()
                        return await self.async_get_page(url, necesita_login=False)
                    raise EroareAutentificareApaOradea("Sesiunea Apa Oradea a expirat")
                if raspuns.status >= 400:
                    raise EroareRaspunsApaOradea(f"Apa Oradea a returnat HTTP {raspuns.status} pentru {url}")
                return text
        except EroareApiApaOradea:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareApaOradea(f"Eroare de conectare la Apa Oradea pentru {url}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareApaOradea(f"Timeout la Apa Oradea pentru {url}") from err

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        pagina_cont = await self.async_get_page(URL_CONT)
        return {"account": pagina_cont}

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        pagini: dict[str, str] = {}
        for cheie, url in (
            ("account", URL_CONT),
            ("contracte", URL_CONTRACTE),
            ("facturi", URL_FACTURI),
            ("plati", URL_PLATI),
            ("contoare", URL_CONTOARE),
            ("citire", URL_CITIRE),
        ):
            try:
                pagini[cheie] = await self.async_get_page(url)
            except EroareApiApaOradea:
                if cheie in {"account", "contracte", "facturi"}:
                    raise
                _LOGGER.debug("Nu s-a putut citi pagina Apa Oradea %s", cheie, exc_info=True)
                pagini[cheie] = ""


        contracte = _extrage_contracte(pagini)
        for contract in contracte:
            id_contract = str(contract.get("id_contract") or "").strip()
            if not id_contract:
                continue
            try:
                detalii = await self.async_get_page(f"{URL_BAZA}/contract.php?id={id_contract}")
                contract["html_detalii"] = detalii
                detalii_extrase = _extrage_detalii_contract(detalii, id_contract)
                contract.update(detalii_extrase)
            except EroareApiApaOradea:
                _LOGGER.debug("Nu s-au putut citi detaliile contractului Apa Oradea %s", id_contract, exc_info=True)

        facturi = _extrage_facturi(pagini, contracte)
        plati = _extrage_plati(pagini, contracte)
        contoare = _extrage_contoare(pagini, contracte)
        return {"contracte": contracte, "facturi": facturi, "plati": plati, "contoare": contoare, "pagini": pagini}


class ClientFurnizorApaOradea(ClientFurnizor):
    cheie_furnizor = "apa_oradea"
    nume_prietenos = "Compania de Apă Oradea"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiApaOradea(self.sesiune, self.utilizator, self.parola)
        try:
            rezultat = await api.async_validate_credentials()
        except EroareAutentificareApaOradea as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaOradea as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaOradea as err:
            raise EroareParsare(str(err)) from err

        id_client = _extrage_id_client(_text_html(rezultat.get("account") or ""))
        return id_client or self.utilizator.strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiApaOradea(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificareApaOradea as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaOradea as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaOradea as err:
            raise EroareParsare(str(err)) from err

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute, conturi)
        consumuri = self._mapeaza_consumuri(date_brute, conturi, facturi)
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={"portal_url": URL_BAZA, "numar_contracte": len(conturi), "numar_facturi": len(facturi)},
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        conturi: list[ContUtilitate] = []
        vazute: set[str] = set()
        for contract in date_brute.get("contracte", []) or []:
            id_contract = str(contract.get("id_contract") or contract.get("cod_electronic") or "").strip()
            if not id_contract or id_contract in vazute:
                continue
            vazute.add(id_contract)
            nr_contract = str(contract.get("numar_contract") or "").strip()
            adresa = str(contract.get("adresa") or "").strip()
            nume = str(contract.get("nume") or contract.get("titular") or "").strip()
            raw = dict(contract)
            conturi.append(
                ContUtilitate(
                    id_cont=id_contract,
                    nume=_nume_cont_apa_oradea(adresa, nr_contract, id_contract),
                    tip_cont="apa",
                    id_contract=nr_contract or id_contract,
                    adresa=adresa,
                    stare=str(contract.get("stare") or "").strip() or None,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute={**raw, "nume_titular": nume},
                )
            )

        if not conturi:
            account_text = _text_html((date_brute.get("pagini") or {}).get("account") or "")
            id_client = _extrage_id_client(account_text) or self.utilizator.strip().lower()
            conturi.append(
                ContUtilitate(
                    id_cont=id_client,
                    nume="Compania de Apă Oradea",
                    tip_cont="apa",
                    id_contract=id_client,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute={"id_client": id_client},
                )
            )
        return conturi

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        cont_default = conturi[0] if conturi else None
        vazute: set[str] = set()

        for item in date_brute.get("facturi", []) or []:
            id_factura = str(item.get("id_factura") or item.get("numar_factura") or item.get("titlu") or "").strip()
            numar_factura = str(item.get("numar_factura") or id_factura).strip()
            cheie_factura = _cheie_factura_apa_oradea(numar_factura or id_factura)
            if not id_factura or not cheie_factura or cheie_factura in vazute:
                continue
            vazute.add(cheie_factura)

            id_cont = self._gaseste_id_cont(item, conturi) or (cont_default.id_cont if cont_default else None)
            valoare = _valoare_numerica(item.get("valoare") or item.get("total") or item.get("suma"))
            rest = _valoare_numerica(item.get("rest_plata") or item.get("sold") or item.get("de_plata"))
            if rest is None:
                rest = valoare
            stare = _stare_factura(item, rest)
            raw = dict(item)
            raw["rest_plata"] = rest if rest is not None else valoare
            raw["pdf_url"] = item.get("url")
            raw["numar_factura"] = numar_factura

            facturi.append(
                FacturaUtilitate(
                    id_factura=id_factura,
                    titlu=str(item.get("titlu") or numar_factura or f"Factura {id_factura}").strip(),
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=_data_sigura(item.get("data_emitere") or item.get("data")),
                    data_scadenta=_data_sigura(item.get("data_scadenta") or item.get("scadenta")),
                    stare=stare,
                    categorie="consum",
                    id_cont=id_cont,
                    id_contract=str(item.get("id_contract") or id_cont or ""),
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=raw,
                )
            )
        facturi.sort(key=lambda f: f.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont(self, item: dict[str, Any], conturi: list[ContUtilitate]) -> str | None:
        valori = {_normalizeaza_potrivire(str(v)) for v in (item.get("id_contract"), item.get("contract"), item.get("adresa"), item.get("cod_beneficiar")) if v}
        for cont in conturi:
            raw = cont.date_brute or {}
            candidati = {_normalizeaza_potrivire(str(v)) for v in (cont.id_cont, cont.id_contract, cont.adresa, raw.get("cod_beneficiar"), raw.get("referinta_interna")) if v}
            if valori & candidati:
                return cont.id_cont
            for valoare in valori:
                if valoare and any(valoare in c or c in valoare for c in candidati if c):
                    return cont.id_cont
        return None

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate], facturi: list[FacturaUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        plati = [p for p in (date_brute.get("plati") or []) if isinstance(p, dict)]
        contoare = [c for c in (date_brute.get("contoare") or []) if isinstance(c, dict)]
        total_de_plata = 0.0
        for cont in conturi:
            facturi_cont = [f for f in facturi if f.id_cont == cont.id_cont]
            plati_cont = [p for p in plati if self._gaseste_id_cont(p, [cont]) == cont.id_cont]
            contoare_cont = [c for c in contoare if self._gaseste_id_cont(c, [cont]) == cont.id_cont]
            neachitate = [f for f in facturi_cont if f.stare in {"neplatita", "scadenta"}]
            sold = round(sum(float(f.date_brute.get("rest_plata") if f.date_brute.get("rest_plata") is not None else f.valoare or 0) for f in neachitate), 2)
            if not facturi_cont and cont.date_brute.get("sold") is not None:
                sold = round(float(cont.date_brute.get("sold") or 0), 2)
            total_de_plata += max(sold, 0.0)
            ultima_factura = facturi_cont[0] if facturi_cont else None
            ultima_plata = _ultima_dupa_data(plati_cont, "data")
            ultim_contor = _ultima_dupa_data(contoare_cont, "data")

            consumuri.extend([
                ConsumUtilitate("de_plata", sold, "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("sold_curent", sold, "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("factura_restanta", "da" if sold > 0 else "nu", None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_facturi", len(facturi_cont) or _int_sigur(cont.date_brute.get("numar_facturi")) or 0, "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_facturi_neachitate", len(neachitate), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_plati", len(plati_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_contoare", _numar_contoare_cont(cont, contoare_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
            ])
            if ultima_factura:
                consumuri.extend([
                    ConsumUtilitate("valoare_ultima_factura", ultima_factura.valoare, "RON", perioada=_date_text(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("id_ultima_factura", _numar_factura_afisat(ultima_factura), None, perioada=_date_text(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("data_ultima_factura", _date_text(ultima_factura.data_emitere), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("urmatoarea_scadenta", _date_text(ultima_factura.data_scadenta), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                ])
            if ultima_plata:
                consumuri.extend([
                    ConsumUtilitate("data_ultima_plata", ultima_plata.get("data"), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                    ConsumUtilitate("valoare_ultima_plata", _valoare_numerica(ultima_plata.get("valoare")), "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                    ConsumUtilitate("ultima_plata", _valoare_numerica(ultima_plata.get("valoare")), "RON", perioada=ultima_plata.get("data"), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                ])
            if ultim_contor:
                consumuri.append(ConsumUtilitate("index_contor", _valoare_numerica(ultim_contor.get("index_nou") or ultim_contor.get("index")), "m³", perioada=ultim_contor.get("data"), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultim_contor))

        consumuri.extend([
            ConsumUtilitate("de_plata", round(total_de_plata, 2), "RON", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("sold_curent", round(total_de_plata, 2), "RON", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_contracte", len(conturi), "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_facturi", len(facturi), "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_plati", len(plati), "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
        ])
        return consumuri


def _parse_html(html_text: str) -> _ParserTabele:
    parser = _ParserTabele()
    try:
        parser.feed(html_text or "")
    except Exception:
        _LOGGER.debug("Parsare HTML parțială Apa Oradea", exc_info=True)
    return parser


def _curata_text(valoare: Any) -> str:
    if valoare is None:
        return ""
    text = unescape(str(valoare))
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _text_html(html_text: str) -> str:
    return _curata_text(html_text)


def _normalizeaza_potrivire(text: str | None) -> str:
    text = _curata_text(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def _pare_pagina_login(html_text: str) -> bool:
    text = _normalizeaza_potrivire(_text_html(html_text or ""))
    return "USERNAME" in text and "PASSWORD" in text and "LOGOUT" not in text


def _extrage_id_client(text: str) -> str | None:
    match = re.search(r"ID\s*client\s*[:#]?\s*([0-9A-Za-z_.-]+)", text or "", flags=re.I)
    return match.group(1).strip() if match else None


def _extrage_contracte(pagini: dict[str, str]) -> list[dict[str, Any]]:
    contracte: list[dict[str, Any]] = []
    vazute: set[str] = set()
    for cheie in ("contracte", "account", "facturi", "contoare"):
        html_text = pagini.get(cheie) or ""
        parser = _parse_html(html_text)
        for link in parser.links:
            href = link.get("href") or ""
            id_contract = _id_din_url(href, "contract.php")
            if id_contract and id_contract not in vazute:
                vazute.add(id_contract)
                contracte.append({"id_contract": id_contract, "url": urljoin(URL_BAZA + "/", href), "sursa": cheie, "eticheta": link.get("text")})
        for tabel in parser.tables:
            for row in _rows_cu_header(tabel):
                text = " ".join(str(v) for v in row.values())
                id_contract = _id_din_text_contract(text)
                if id_contract and id_contract not in vazute:
                    vazute.add(id_contract)
                    contracte.append({"id_contract": id_contract, "sursa": cheie, "raw_row": row, **_contract_din_row(row)})
    return contracte


def _extrage_detalii_contract(html_text: str, id_contract: str) -> dict[str, Any]:
    text = _text_html(html_text)
    detalii: dict[str, Any] = {"id_contract": id_contract}
    patterns = {
        "numar_contract": r"Contract\s+nr\.?.*?Nr\.?\s*:?\s*([0-9A-Za-z./ -]+?)\s+Tip\s+contract",
        "tip_contract": r"Tip\s+contract\s+(.+?)\s+Nume\s+",
        "nume": r"\bNume\s+(.+?)\s+Cod\s+beneficiar",
        "cod_beneficiar": r"Cod\s+beneficiar\s*/\s*Subunitate\s+([0-9A-Za-z /.-]+?)\s+CNP",
        "cod_electronic": r"Cod\s+electronic\s+contract\s+([0-9A-Za-z_.-]+)",
        "referinta_interna": r"Referință\s+internă\s+(.+?)\s+Adresa\s+",
        "adresa": r"Adresa\s+(.+?)\s+Facturi\s+emise",
        "numar_facturi": r"Facturi\s+emise\s+(\d+)",
        "numar_contoare": r"Nr\.\s*contoare\s+(\d+)",
    }
    for cheie, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            detalii[cheie] = _curata_text(match.group(1))
    facturi = _extrage_facturi_din_text_contract(text, id_contract)
    if facturi:
        detalii["facturi_recente"] = facturi
    return detalii


def _contract_din_row(row: dict[str, str]) -> dict[str, Any]:
    text = " ".join(row.values())
    return {
        "numar_contract": _primul(row, "contract", "nr contract", "numar contract", "nr") or _nr_contract_din_text(text),
        "adresa": _primul(row, "adresa", "loc consum", "punct consum"),
        "nume": _primul(row, "nume", "titular", "client"),
        "stare": _primul(row, "stare", "status"),
    }


def _extrage_facturi(pagini: dict[str, str], contracte: list[dict[str, Any]]) -> list[dict[str, Any]]:
    facturi: list[dict[str, Any]] = []
    vazute: set[str] = set()
    id_contract_default = _id_contract_default(contracte)
    contract_by_id = {str(c.get("id_contract")): c for c in contracte if c.get("id_contract")}

    html_text = pagini.get("facturi") or ""
    parser = _parse_html(html_text)
    linkuri_facturi: dict[str, str] = {}
    for link in parser.links:
        href = link.get("href") or ""
        id_factura = _id_din_url(href, "invoice-download.php") or _id_din_url(href, "invoice.php")
        text_link = _curata_text(link.get("text") or "")
        if id_factura and text_link:
            linkuri_facturi[text_link] = urljoin(URL_BAZA + "/", href)

    for tabel in parser.tables:
        for rand in tabel:
            factura = _factura_din_rand_apa_oradea(rand, id_contract_default, contract_by_id)
            if not factura:
                continue
            cheie = _cheie_factura_apa_oradea(factura.get("numar_factura") or factura.get("id_factura"))
            if not cheie or cheie in vazute:
                continue
            vazute.add(cheie)
            factura["url"] = factura.get("url") or _url_factura_din_linkuri(factura.get("numar_factura"), linkuri_facturi)
            facturi.append(factura)

    # Facturile recente din coloana dreaptă sunt folosite doar ca fallback.
    # Lista principală de facturi are valori, rest de plată și scadență, deci are prioritate.
    for contract in contracte:
        for factura in contract.get("facturi_recente") or []:
            cheie = _cheie_factura_apa_oradea(factura.get("numar_factura") or factura.get("id_factura"))
            if not cheie or cheie in vazute:
                continue
            vazute.add(cheie)
            factura.setdefault("id_contract", str(contract.get("id_contract") or id_contract_default or ""))
            facturi.append(factura)

    return facturi


def _extrage_facturi_din_text_contract(text: str, id_contract: str) -> list[dict[str, Any]]:
    facturi: list[dict[str, Any]] = []
    pattern = re.compile(
        r"Factura\s+(.+?)\s+Nr\.\s*:?\s*([0-9A-Za-z_.-]+)\s*/\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})(.*?)(?=Factura\s+|Vezi\s+toate\s+facturile|S\.C\.|$)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(text or ""):
        descriere = _curata_text(match.group(1))
        numar = _curata_text(match.group(2))
        data_emitere = _curata_text(match.group(3))
        stare_text = _curata_text(match.group(4))
        facturi.append({
            "id_factura": numar,
            "numar_factura": numar,
            "titlu": f"Factura {descriere} {numar}".strip(),
            "data_emitere": data_emitere,
            "id_contract": id_contract,
            "stare_text": stare_text,
        })
    return facturi


def _extrage_plati(pagini: dict[str, str], contracte: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plati: list[dict[str, Any]] = []
    id_contract_default = _id_contract_default(contracte)
    parser = _parse_html(pagini.get("plati") or "")

    for tabel in parser.tables:
        for rand in tabel:
            text = _curata_text(" ".join(rand))
            plata = _plata_din_text_apa_oradea(text, id_contract_default)
            if plata:
                plati.append(plata)

    if not plati:
        plata = _plata_din_text_apa_oradea(_text_html(pagini.get("plati") or ""), id_contract_default)
        if plata:
            plati.append(plata)

    return plati


def _extrage_contoare(pagini: dict[str, str], contracte: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contoare: list[dict[str, Any]] = []
    id_contract_default = _id_contract_default(contracte)

    # Pagina de transmitere index conține starea curentă a contoarelor, fără tabel HTML.
    # O citim prima, ca senzorul de index să reflecte cel mai nou index disponibil.
    contoare.extend(_contoare_din_text_citire(_text_html(pagini.get("citire") or ""), id_contract_default))

    parser = _parse_html(pagini.get("contoare") or "")
    for tabel in parser.tables:
        for rand in tabel:
            contor = _contor_din_rand_apa_oradea(rand, id_contract_default)
            if contor:
                contoare.append(contor)

    return contoare



def _id_contract_default(contracte: list[dict[str, Any]]) -> str | None:
    if len(contracte) == 1:
        valoare = contracte[0].get("id_contract") or contracte[0].get("cod_electronic")
        return str(valoare).strip() if valoare else None
    return None


def _cheie_factura_apa_oradea(value: Any) -> str:
    text = _normalizeaza_potrivire(str(value or "")).upper()
    text = re.sub(r"\bCAO\s*[-_/]?\s*", "", text)
    match = re.search(r"\b(\d{3,})\b", text)
    return match.group(1) if match else text


def _factura_din_rand_apa_oradea(rand: list[str], id_contract_default: str | None, contract_by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    text = _curata_text(" ".join(rand))
    if not re.search(r"Factura\s+fiscala\s+Nr\.\s*CAO", text, flags=re.I):
        return None

    match_nr = re.search(r"Factura\s+fiscala\s+Nr\.\s*(CAO\s*[-_/]?\s*\d+)", text, flags=re.I)
    if not match_nr:
        return None
    numar = re.sub(r"\s+", "", match_nr.group(1).upper()).replace("CAO_", "CAO-")
    if not numar.startswith("CAO-"):
        numar = numar.replace("CAO", "CAO-", 1)

    data_emitere = _data_dupa_eticheta(text, r"Data\s+emitere")
    data_scadenta = _data_dupa_eticheta(text, r"Data\s+scaden[țt]ă")
    valoare = _valoare_dupa_eticheta(text, r"Sum[ăa]\s+emis[ăa]\s+factur[ăa]")
    rest = _valoare_dupa_eticheta(text, r"Rest\s+de\s+plat[ăa]")
    id_contract = _id_contract_din_factura_row({}, text, contract_by_id) or id_contract_default

    return {
        "id_factura": numar,
        "numar_factura": numar,
        "titlu": f"Factura {numar}",
        "data_emitere": data_emitere,
        "data_scadenta": data_scadenta,
        "valoare": valoare,
        "sold": rest,
        "rest_plata": rest,
        "id_contract": id_contract,
        "raw_row": {f"col_{idx}": valoare_rand for idx, valoare_rand in enumerate(rand)},
    }


def _data_dupa_eticheta(text: str, eticheta_pattern: str) -> str | None:
    match = re.search(eticheta_pattern + r"\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})", text or "", flags=re.I)
    data = _data_din_text(match.group(1)) if match else None
    return data.isoformat() if data else None


def _valoare_dupa_eticheta(text: str, eticheta_pattern: str) -> str | None:
    match = re.search(eticheta_pattern + r"\s+(-?\d+(?:[.,]\d+)?)\s*Lei", text or "", flags=re.I)
    return f"{match.group(1)} Lei" if match else None


def _url_factura_din_linkuri(numar_factura: Any, linkuri: dict[str, str]) -> str | None:
    cheie = _cheie_factura_apa_oradea(numar_factura)
    for text_link, url in linkuri.items():
        if _cheie_factura_apa_oradea(text_link) == cheie:
            return url
    return None


def _plata_din_text_apa_oradea(text: str, id_contract_default: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    data_plata = _data_dupa_eticheta(text, r"Data\s+pl[ăa][țt]ii\s*:")
    valoare = _valoare_dupa_eticheta(text, r"Valoare")
    if not data_plata and not valoare:
        return None
    stare = None
    match_stare = re.search(r"Stare\s*:\s*(.+?)(?:\s+Valoare|\s+Explica[țt]ie|$)", text, flags=re.I | re.S)
    if match_stare:
        stare = _curata_text(match_stare.group(1))
    return {
        "data": data_plata,
        "valoare": valoare,
        "stare": stare,
        "id_contract": id_contract_default,
        "raw_text": text[:1000],
    }


def _contor_din_rand_apa_oradea(rand: list[str], id_contract_default: str | None) -> dict[str, Any] | None:
    text = _curata_text(" ".join(rand))
    if "Contor nr" not in text or "Index nou" not in text:
        return None
    serie = _extrage_dupa_pattern(text, r"Contor\s+nr\s+(.+?)(?:\s+Interval\s+de\s+citire|\s+Index\s+vechi|$)")
    index_vechi = _extrage_dupa_pattern(text, r"Index\s+vechi\s+(-?\d+(?:[.,]\d+)?)")
    index_nou = _extrage_dupa_pattern(text, r"Index\s+nou\s+(-?\d+(?:[.,]\d+)?)")
    consum = _extrage_dupa_pattern(text, r"Consum\s+(-?\d+(?:[.,]\d+)?)")
    data = None
    match_interval = re.search(r"Interval\s+de\s+citire\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s*-\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})", text, flags=re.I)
    if match_interval:
        data_obj = _data_din_text(match_interval.group(2))
        data = data_obj.isoformat() if data_obj else None
    return {
        "serie": serie,
        "index": index_nou,
        "index_vechi": index_vechi,
        "index_nou": index_nou,
        "consum": consum,
        "data": data,
        "id_contract": id_contract_default,
        "raw_row": {f"col_{idx}": valoare for idx, valoare in enumerate(rand)},
    }


def _contoare_din_text_citire(text: str, id_contract_default: str | None) -> list[dict[str, Any]]:
    contoare: list[dict[str, Any]] = []
    pattern = re.compile(
        r"Contor\s+apa\s+(.+?)\s+Indexul\s+vechi\s+(-?\d+(?:[.,]\d+)?).*?Index\s+nou\s+(-?\d+(?:[.,]\d+)?).*?Data\s+index\s+vechi\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s+Data\s+index\s+nou\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s+Consum\s+(-?\d+(?:[.,]\d+)?)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(text or ""):
        data_obj = _data_din_text(match.group(5))
        contoare.append({
            "serie": _curata_text(match.group(1)),
            "index": _curata_text(match.group(3)),
            "index_vechi": _curata_text(match.group(2)),
            "index_nou": _curata_text(match.group(3)),
            "consum": _curata_text(match.group(6)),
            "data": data_obj.isoformat() if data_obj else None,
            "id_contract": id_contract_default,
            "sursa": "citire",
        })
    return contoare


def _extrage_dupa_pattern(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text or "", flags=re.I | re.S)
    return _curata_text(match.group(1)) if match else None


def _numar_contoare_cont(cont: ContUtilitate, contoare_cont: list[dict[str, Any]]) -> int:
    numar_configurat = _int_sigur((cont.date_brute or {}).get("numar_contoare"))
    if numar_configurat is not None:
        return numar_configurat
    serii = {str(c.get("serie") or "").strip() for c in contoare_cont if c.get("serie")}
    return len(serii)


def _rows_cu_header(tabel: list[list[str]]) -> list[dict[str, str]]:
    if len(tabel) < 2:
        return []
    header = [_normalizeaza_header(c) or f"col_{idx}" for idx, c in enumerate(tabel[0])]
    rows: list[dict[str, str]] = []
    for rand in tabel[1:]:
        item: dict[str, str] = {}
        for idx, valoare in enumerate(rand):
            key = header[idx] if idx < len(header) else f"col_{idx}"
            item[key] = valoare
        if any(item.values()):
            rows.append(item)
    return rows


def _normalizeaza_header(text: str) -> str:
    text = _normalizeaza_potrivire(text).lower()
    return re.sub(r"\s+", " ", text).strip()


def _primul(row: dict[str, str], *chei: str) -> str | None:
    chei_norm = {_normalizeaza_header(c) for c in chei}
    for key, value in row.items():
        key_norm = _normalizeaza_header(key)
        if key_norm in chei_norm or any(c in key_norm or key_norm in c for c in chei_norm):
            val = _curata_text(value)
            if val:
                return val
    return None


def _id_din_url(href: str, pagina: str) -> str | None:
    if pagina not in href:
        return None
    query = parse_qs(urlparse(href).query)
    valori = query.get("id")
    if valori and str(valori[0]).strip():
        return str(valori[0]).strip()
    match = re.search(r"[?&]id=([0-9A-Za-z_.-]+)", href)
    return match.group(1) if match else None


def _id_din_text_contract(text: str) -> str | None:
    match = re.search(r"contract\.php\?id=([0-9A-Za-z_.-]+)", text or "", flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"Cod\s+electronic\s+contract\s+([0-9A-Za-z_.-]+)", text or "", flags=re.I)
    return match.group(1).strip() if match else None


def _id_din_text_factura(text: str) -> str | None:
    match = re.search(r"invoice\.php\?id=([0-9A-Za-z_.-]+)", text or "", flags=re.I)
    return match.group(1) if match else None


def _id_factura_din_linkuri_text(text: str, linkuri: dict[str, str]) -> str | None:
    for id_factura in linkuri:
        if id_factura in text:
            return id_factura
    return None


def _nr_contract_din_text(text: str) -> str | None:
    match = re.search(r"\b(\d{3,}\s*/\s*\d{1,2}[./-]\d{1,2}[./-]\d{4})\b", text or "")
    return _curata_text(match.group(1)) if match else None


def _numar_factura_din_text(text: str) -> str | None:
    match = re.search(r"Nr\.\s*:?\s*([0-9A-Za-z_.-]+)\s*/\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})", text or "", flags=re.I)
    if match:
        return f"{match.group(1)} / {match.group(2)}"
    match = re.search(r"\b(?:Factura|Fact\.)\s+([A-Z0-9_.-]{3,})\b", text or "", flags=re.I)
    return match.group(1).strip() if match else None


def _id_contract_din_factura_row(row: dict[str, str], text: str, contract_by_id: dict[str, dict[str, Any]]) -> str | None:
    id_contract = _id_din_text_contract(text)
    if id_contract:
        return id_contract
    contract_text = _primul(row, "contract", "nr contract", "cod contract") or text
    norm = _normalizeaza_potrivire(contract_text)
    for cid, contract in contract_by_id.items():
        valori = [cid, contract.get("numar_contract"), contract.get("cod_beneficiar"), contract.get("adresa")]
        for val in valori:
            val_norm = _normalizeaza_potrivire(str(val or ""))
            if val_norm and (val_norm in norm or norm in val_norm):
                return cid
    return None


def _data_text_din_row(row: dict[str, str], text: str, chei: tuple[str, ...]) -> str | None:
    for cheie in chei:
        val = _primul(row, cheie)
        data = _data_din_text(val or "")
        if data:
            return data.isoformat()
    data = _data_din_text(text or "")
    return data.isoformat() if data else None


def _valoare_din_row(row: dict[str, str], chei: tuple[str, ...]) -> str | None:
    for cheie in chei:
        val = _primul(row, cheie)
        if _valoare_numerica(val) is not None:
            return val
    return None


def _valoare_numerica(valoare: Any) -> float | None:
    if valoare in (None, ""):
        return None
    text = _curata_text(valoare)
    text = re.sub(r"[^0-9,\.\-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _data_din_text(text: str) -> date | None:
    text = _curata_text(text or "")
    for pattern in (r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if len(match.group(1)) == 4:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
    return None


def _data_sigura(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    return _data_din_text(str(value or ""))


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if isinstance(value, date) else None


def _stare_factura(item: dict[str, Any], rest: float | None) -> str:
    text = _normalizeaza_potrivire(" ".join(str(v) for v in item.values()))
    if rest is not None and rest > 0:
        return "neplatita"
    if any(cuv in text for cuv in ("PLATA ACCEPTATA", "TRANZACTIE APROBATA", "PLATITA", "ACHITATA")):
        return "platita"
    if rest == 0:
        return "platita"
    return "necunoscuta"


def _ultima_dupa_data(items: list[dict[str, Any]], cheie: str) -> dict[str, Any] | None:
    def sort_key(item: dict[str, Any]) -> date:
        return _data_sigura(item.get(cheie)) or date.min
    return sorted(items, key=sort_key, reverse=True)[0] if items else None


def _numar_factura_afisat(factura: FacturaUtilitate) -> str:
    raw = factura.date_brute or {}
    text = str(raw.get("numar_factura") or factura.titlu or factura.id_factura or "").strip()
    return text or factura.id_factura


def _nume_cont_apa_oradea(adresa: str, nr_contract: str, id_contract: str) -> str:
    baza = adresa or "Loc consum"
    detalii = nr_contract or id_contract
    text = f"{baza} ({detalii})" if detalii and detalii not in baza else baza
    return re.sub(r"\s+", " ", text).strip()


def _int_sigur(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
