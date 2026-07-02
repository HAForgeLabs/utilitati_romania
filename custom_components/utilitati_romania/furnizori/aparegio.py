from __future__ import annotations

from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
import logging
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp

from ..const import FURNIZOR_APAREGIO
from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://aparegio.emsys.ro/CUSTOMER_PORTAL"
URL_LOGIN = f"{URL_BAZA}/login.jsp"
URL_WELCOME = f"{URL_BAZA}/welcome.jsf"
URL_TRANSMITERE = f"{URL_BAZA}/transmitere.jsf"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class EroareApiAparegio(Exception):
    pass


class EroareAutentificareAparegio(EroareApiAparegio):
    pass


class EroareConectareAparegio(EroareApiAparegio):
    pass


class EroareRaspunsAparegio(EroareApiAparegio):
    pass


class _ParserHtmlSimplu(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.links: list[dict[str, str]] = []
        self.inputs: dict[str, str] = {}
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []
        self._link_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        if tag == "input":
            name = attrs_dict.get("name") or attrs_dict.get("id")
            if name:
                self.inputs[name] = attrs_dict.get("value", "")
        elif tag == "a":
            self._link_stack.append({"href": attrs_dict.get("href", ""), "text": ""})
        elif tag == "table":
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
            link["text"] = _curata_text(link.get("text") or "")
            if link.get("href") or link.get("text"):
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


class ClientApiAparegio:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._autentificat = False
        self._ultima_pagina: str | None = None
        self._pagina_initiala: str = ""
        self._url_welcome: str | None = None

    def _headers(
        self,
        *,
        referer: str | None = None,
        accept: str | None = None,
        origin: bool = False,
        navigate: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": USER_AGENT,
            "Referer": referer or URL_LOGIN,
        }
        if origin:
            headers["Origin"] = URL_BAZA.rsplit("/", 1)[0]
        if navigate:
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Sec-Fetch-Dest"] = "document"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-Site"] = "same-origin"
        return headers

    async def async_login(self) -> None:
        try:
            async with self._sesiune.get(URL_LOGIN, headers=self._headers(navigate=True), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                pagina_login = await raspuns.text(errors="ignore")
                if raspuns.status >= 400:
                    raise EroareConectareAparegio(f"Aparegio a returnat HTTP {raspuns.status} la pagina de login")

            async with self._sesiune.post(
                URL_LOGIN,
                headers={
                    **self._headers(referer=URL_LOGIN, origin=True, navigate=True),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"email": self._utilizator, "pass": self._parola, "login": "Autentificare"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text(errors="ignore")
                locatie = raspuns.headers.get("Location")
                if raspuns.status not in {200, 302, 303}:
                    raise EroareAutentificareAparegio(f"Autentificare Aparegio eșuată: HTTP {raspuns.status}")
                if raspuns.status == 200 and _pare_pagina_login(text):
                    raise EroareAutentificareAparegio("Credentialele Aparegio nu au fost acceptate")

            url_dupa_login = urljoin(URL_LOGIN, locatie) if locatie else URL_WELCOME
            pagina_cont = await self.async_get_page(url_dupa_login, necesita_login=False)

            # Portalul EMSYS/ADF returneaza o pagina-stub daca nu primeste parametrii
            # pe care ii adauga JavaScript-ul browserului. Facem acelasi al doilea GET
            # vazut in HAR: _afrLoop + _afrWindowMode + Adf-Window-Id + media features.
            if _este_pagina_loopback_adf(pagina_cont) or not _are_stare_adf(pagina_cont):
                url_adf = _url_cu_parametri_adf(self._ultima_pagina or url_dupa_login, pagina_cont)
                _LOGGER.debug("[APAREGIO DIAG] adf_loopback_url=%s", _mascheaza_diag(url_adf))
                pagina_adf = await self.async_get_page(url_adf, necesita_login=False)
                _LOGGER.debug(
                    "[APAREGIO DIAG] adf_loopback_result: initial_len=%s adf_len=%s markers=%s js_stub=%s",
                    len(pagina_cont or ""),
                    len(pagina_adf or ""),
                    _markere_pagina(pagina_adf or ""),
                    _pare_pagina_js_neactiv(pagina_adf or ""),
                )
                # Paginile ADF reale pot contine in continuare textul <noscript>.
                # Nu le respingem doar din acest motiv; le pastram daca au stare ADF,
                # markeri de continut sau sunt semnificativ mai mari decat stub-ul initial.
                if pagina_adf and (
                    _are_stare_adf(pagina_adf)
                    or _contine_marker_cont(pagina_adf)
                    or _contine_marker_facturi(pagina_adf)
                    or len(pagina_adf) > len(pagina_cont) + 5000
                ):
                    pagina_cont = pagina_adf

            if _pare_pagina_login(pagina_cont):
                raise EroareAutentificareAparegio("Sesiunea Aparegio nu a fost creată corect")
            self._autentificat = True
            self._pagina_initiala = pagina_cont
            self._url_welcome = self._ultima_pagina or url_dupa_login
        except EroareApiAparegio:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareAparegio(f"Eroare de conectare la Aparegio: {err}") from err

    async def async_get_page(self, url: str, *, necesita_login: bool = True) -> str:
        if necesita_login and not self._autentificat:
            await self.async_login()
        try:
            async with self._sesiune.get(url, headers=self._headers(referer=self._ultima_pagina or URL_WELCOME, navigate=True), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                text = await raspuns.text(errors="ignore")
                if raspuns.status in {401, 403} or _pare_pagina_login(text):
                    if necesita_login:
                        self._autentificat = False
                        await self.async_login()
                        return await self.async_get_page(url, necesita_login=False)
                    raise EroareAutentificareAparegio("Sesiunea Aparegio a expirat")
                if raspuns.status >= 400:
                    raise EroareRaspunsAparegio(f"Aparegio a returnat HTTP {raspuns.status} pentru {url}")
                self._ultima_pagina = str(raspuns.url)
                return text
        except EroareApiAparegio:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareAparegio(f"Eroare de conectare la Aparegio pentru {url}: {err}") from err

    async def _async_post_adf(self, url: str, data: dict[str, str], *, referer: str | None = None) -> str:
        try:
            async with self._sesiune.post(
                url,
                headers={
                    **self._headers(referer=referer or self._ultima_pagina or self._url_welcome or URL_WELCOME, accept="*/*", origin=True),
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Adf-Rich-Message": "true",
                    "X-Requested-With": "XMLHttpRequest",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                },
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text(errors="ignore")
                if raspuns.status in {401, 403} or _pare_pagina_login(text):
                    raise EroareAutentificareAparegio("Sesiunea Aparegio a expirat la request ADF")
                if raspuns.status >= 400:
                    raise EroareRaspunsAparegio(f"Aparegio a returnat HTTP {raspuns.status} pentru request ADF")
                self._ultima_pagina = str(raspuns.url)
                return text
        except EroareApiAparegio:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareAparegio(f"Eroare de conectare la Aparegio pentru request ADF: {err}") from err

    async def _async_get_facturi_adf(self, pagina_welcome: str) -> str:
        stare = _extrage_stare_adf(pagina_welcome)
        if not stare.get("view_state"):
            return ""
        azi = date.today()
        inceput = azi - timedelta(days=365)
        form_id = stare.get("form") or "asdas"
        window_id = stare.get("window_id") or "w0"
        data = {
            "j_idt62": inceput.strftime("%d.%m.%Y"),
            "dataSfarsit": azi.strftime("%d.%m.%Y"),
            "j_idt68": "0",
            "j_idt74": "0",
            "org.apache.myfaces.trinidad.faces.FORM": form_id,
            "Adf-Window-Id": window_id,
            "javax.faces.ViewState": stare["view_state"],
            "oracle.adf.view.rich.DELTAS": "{j_idt88:facturiTbl={rows=50}}",
        }
        url = self._url_welcome or self._ultima_pagina or URL_WELCOME
        if "Adf-Window-Id=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}Adf-Window-Id={window_id}"
        return await self._async_post_adf(url, data, referer=self._url_welcome or URL_WELCOME)

    def _url_adf_curent(self, window_id: str) -> str:
        url = self._url_welcome or self._ultima_pagina or URL_WELCOME
        if "Adf-Window-Id=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}Adf-Window-Id={window_id}"
        return url

    async def _async_get_consum_adf(self, pagina_welcome: str) -> str:
        stare = _extrage_stare_adf(pagina_welcome)
        if not stare.get("view_state"):
            return ""
        azi = date.today()
        inceput = azi - timedelta(days=365)
        form_id = stare.get("form") or "asdas"
        window_id = stare.get("window_id") or "w0"
        # Click-ul real pe tabul "Istoric consum" pleaca din tabul curent
        # "Facturi in sold" si trimite campurile formularului de facturi, nu
        # campurile formularului de consum. Captura XHR din browser arata:
        # j_idt62, dataSfarsit, j_idt68, j_idt74 + event=j_idt99.
        data = {
            "j_idt62": inceput.strftime("%d.%m.%Y"),
            "dataSfarsit": azi.strftime("%d.%m.%Y"),
            "j_idt68": "0",
            "j_idt74": "0",
            "org.apache.myfaces.trinidad.faces.FORM": form_id,
            "Adf-Window-Id": window_id,
            "javax.faces.ViewState": stare["view_state"],
            "event": "j_idt99",
            "event.j_idt99": '<m xmlns="http://oracle.com/richClient/comm"><k v="expand"><b>1</b></k><k v="type"><s>disclosure</s></k></m>',
            "oracle.adf.view.rich.PROCESS": "admtab,j_idt99",
        }
        return await self._async_post_adf(self._url_adf_curent(window_id), data, referer=self._url_welcome or URL_WELCOME)

    async def _async_get_plati_adf(self, pagina_welcome: str) -> str:
        stare = _extrage_stare_adf(pagina_welcome)
        if not stare.get("view_state"):
            return ""
        azi = date.today()
        inceput = azi - timedelta(days=365)
        form_id = stare.get("form") or "asdas"
        window_id = stare.get("window_id") or "w0"
        # Click-ul real pe tabul "Istoric plati" foloseste acelasi formular
        # de facturi si event=j_idt140. Folosirea campurilor din tabul de plati
        # aici nu functioneaza, pentru ca tabul inca nu este incarcat.
        data = {
            "j_idt62": inceput.strftime("%d.%m.%Y"),
            "dataSfarsit": azi.strftime("%d.%m.%Y"),
            "j_idt68": "0",
            "j_idt74": "0",
            "org.apache.myfaces.trinidad.faces.FORM": form_id,
            "Adf-Window-Id": window_id,
            "javax.faces.ViewState": stare["view_state"],
            "event": "j_idt140",
            "event.j_idt140": '<m xmlns="http://oracle.com/richClient/comm"><k v="expand"><b>1</b></k><k v="type"><s>disclosure</s></k></m>',
            "oracle.adf.view.rich.PROCESS": "admtab,j_idt140",
        }
        return await self._async_post_adf(self._url_adf_curent(window_id), data, referer=self._url_welcome or URL_WELCOME)

    async def _async_get_transmitere_adf(self, pagina_welcome: str) -> str:
        stare = _extrage_stare_adf(pagina_welcome)
        if not stare.get("view_state"):
            return ""
        form_id = stare.get("form") or "asdas"
        window_id = stare.get("window_id") or "w0"
        data = {
            "org.apache.myfaces.trinidad.faces.FORM": form_id,
            "Adf-Window-Id": window_id,
            "javax.faces.ViewState": stare["view_state"],
            "event": "j_idt23:j_idt28",
            "event.j_idt23:j_idt28": '<m xmlns="http://oracle.com/richClient/comm"><k v="type"><s>action</s></k></m>',
        }
        return await self._async_post_adf(self._url_adf_curent(window_id), data, referer=self._url_welcome or URL_WELCOME)

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        pagina_welcome = self._pagina_initiala or await self.async_get_page(URL_WELCOME)
        pagina_facturi_adf = ""
        try:
            pagina_facturi_adf = await self._async_get_facturi_adf(pagina_welcome)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi tabelul ADF de facturi Aparegio la validare", exc_info=True)
        pagina_consum_adf = ""
        pagina_plati_adf = ""
        try:
            pagina_consum_adf = await self._async_get_consum_adf(pagina_welcome)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi istoricul de consum Aparegio la validare", exc_info=True)
        try:
            pagina_plati_adf = await self._async_get_plati_adf(pagina_welcome)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi istoricul de plati Aparegio la validare", exc_info=True)

        pagina_transmitere = ""
        try:
            pagina_transmitere = await self.async_get_page(URL_TRANSMITERE)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi pagina de transmitere index Aparegio la validare", exc_info=True)
        if not _contine_marker_cont(pagina_transmitere):
            try:
                pagina_transmitere_adf = await self._async_get_transmitere_adf(pagina_welcome)
                pagina_transmitere = pagina_transmitere + "\n" + pagina_transmitere_adf
            except EroareApiAparegio:
                _LOGGER.debug("Nu s-a putut naviga ADF la transmitere Aparegio la validare", exc_info=True)
        return {
            "welcome": pagina_welcome + "\n" + pagina_facturi_adf,
            "consum": pagina_consum_adf,
            "plati": pagina_plati_adf,
            "transmitere": pagina_transmitere,
        }

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        pagini: dict[str, str] = {}
        pagina_welcome = self._pagina_initiala or await self.async_get_page(URL_WELCOME)
        pagina_facturi_adf = ""
        try:
            pagina_facturi_adf = await self._async_get_facturi_adf(pagina_welcome)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi tabelul ADF de facturi Aparegio", exc_info=True)

        pagini["welcome"] = pagina_welcome + "\n" + pagina_facturi_adf
        try:
            pagini["consum"] = await self._async_get_consum_adf(pagina_welcome)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi istoricul de consum Aparegio", exc_info=True)
            pagini["consum"] = ""
        try:
            pagini["plati"] = await self._async_get_plati_adf(pagina_welcome)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi istoricul de plati Aparegio", exc_info=True)
            pagini["plati"] = ""
        try:
            pagini["transmitere"] = await self.async_get_page(URL_TRANSMITERE)
        except EroareApiAparegio:
            _LOGGER.debug("Nu s-a putut citi pagina de transmitere index Aparegio", exc_info=True)
            pagini["transmitere"] = ""

        if not _contine_marker_cont(pagini.get("transmitere") or ""):
            try:
                pagina_transmitere_adf = await self._async_get_transmitere_adf(pagina_welcome)
                pagini["transmitere"] = (pagini.get("transmitere") or "") + "\n" + pagina_transmitere_adf
            except EroareApiAparegio:
                _LOGGER.debug("Nu s-a putut naviga ADF la transmitere Aparegio", exc_info=True)

        conturi = _extrage_conturi(pagini)
        facturi = _extrage_facturi(pagini, conturi)
        plati = _extrage_plati(pagini, conturi)
        contoare = _extrage_contoare(pagini, conturi)

        _log_diag_aparegio(pagini, conturi, facturi, plati, contoare)
        return {"conturi": conturi, "facturi": facturi, "plati": plati, "contoare": contoare, "pagini": pagini}


class ClientFurnizorAparegio(ClientFurnizor):
    cheie_furnizor = FURNIZOR_APAREGIO
    nume_prietenos = "ApaRegio Gorj"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiAparegio(self.sesiune, self.utilizator, self.parola)
        try:
            rezultat = await api.async_validate_credentials()
        except EroareAutentificareAparegio as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareAparegio as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsAparegio as err:
            raise EroareParsare(str(err)) from err

        conturi = _extrage_conturi({"welcome": rezultat.get("welcome") or "", "transmitere": rezultat.get("transmitere") or ""})
        if conturi:
            return str(conturi[0].get("id_cont") or conturi[0].get("id_client") or self.utilizator).strip().lower()
        return self.utilizator.strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiAparegio(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificareAparegio as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareAparegio as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsAparegio as err:
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
            extra={"portal_url": URL_BAZA, "numar_conturi": len(conturi), "numar_facturi": len(facturi)},
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        conturi: list[ContUtilitate] = []
        vazute: set[str] = set()
        for item in date_brute.get("conturi") or []:
            id_cont = str(item.get("id_cont") or item.get("punct_consum") or item.get("id_client") or "").strip()
            if not id_cont or id_cont in vazute:
                continue
            vazute.add(id_cont)
            id_contract = str(item.get("id_contract") or item.get("contract") or id_cont).strip()
            adresa = _curata_text(item.get("adresa") or "") or None
            nume_client = _curata_text(item.get("nume_client") or "") or None
            nume = _nume_cont_aparegio(adresa, id_contract, id_cont)
            conturi.append(
                ContUtilitate(
                    id_cont=id_cont,
                    nume=nume,
                    tip_cont="apa",
                    id_contract=id_contract,
                    adresa=adresa,
                    stare="activ",
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute={**item, "nume_client": nume_client},
                )
            )

        if not conturi:
            id_client = self.utilizator.strip().lower()
            conturi.append(
                ContUtilitate(
                    id_cont=id_client,
                    nume="ApaRegio Gorj",
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
        for item in date_brute.get("facturi") or []:
            numar = str(item.get("numar_factura") or item.get("id_factura") or "").strip()
            id_factura = numar or _cheie_factura(item)
            cheie = _cheie_factura(id_factura or item)
            if not cheie or cheie in vazute:
                continue
            vazute.add(cheie)
            id_cont = self._gaseste_id_cont(item, conturi) or (cont_default.id_cont if cont_default else None)
            valoare = _valoare_numerica(item.get("valoare") or item.get("total") or item.get("suma"))
            rest_brut = item.get("rest_plata") if item.get("rest_plata") not in (None, "") else item.get("sold") if item.get("sold") not in (None, "") else item.get("de_plata")
            rest = _valoare_numerica(rest_brut)
            # In portalul ApaRegio, coloana "Rest de plata" poate fi goala pentru facturi istorice.
            # Gol nu inseamna ca factura este neachitata; soldul real este afisat separat ca 0,00.
            # Pentru aceste randuri tratam restul lipsa ca 0, ca sa nu insumam toate facturile vechi.
            rest_lipsa = rest_brut in (None, "")
            if rest_lipsa:
                rest = 0.0
            stare = _stare_factura(item, rest)
            raw = dict(item)
            raw["rest_plata"] = rest
            raw["rest_plata_lipsa"] = rest_lipsa
            facturi.append(
                FacturaUtilitate(
                    id_factura=str(id_factura or cheie),
                    titlu=str(item.get("titlu") or f"Factura {numar or cheie}").strip(),
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=_data_sigura(item.get("data_emitere") or item.get("data")),
                    data_scadenta=_data_sigura(item.get("data_scadenta") or item.get("scadenta")),
                    stare=stare,
                    categorie="consum",
                    id_cont=id_cont,
                    id_contract=str(item.get("id_contract") or item.get("contract") or (cont_default.id_contract if cont_default else "") or ""),
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=raw,
                )
            )
        facturi.sort(key=lambda factura: factura.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont(self, item: dict[str, Any], conturi: list[ContUtilitate]) -> str | None:
        valori = {_normalizeaza_potrivire(str(value)) for value in (item.get("id_cont"), item.get("punct_consum"), item.get("id_contract"), item.get("contract"), item.get("client"), item.get("adresa")) if value}
        for cont in conturi:
            raw = cont.date_brute or {}
            candidati = {_normalizeaza_potrivire(str(value)) for value in (cont.id_cont, cont.id_contract, cont.adresa, raw.get("id_client"), raw.get("punct_consum")) if value}
            if valori & candidati:
                return cont.id_cont
            for valoare in valori:
                if valoare and any(valoare in candidat or candidat in valoare for candidat in candidati if candidat):
                    return cont.id_cont
        return None

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate], facturi: list[FacturaUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        plati = [item for item in (date_brute.get("plati") or []) if isinstance(item, dict)]
        contoare = [item for item in (date_brute.get("contoare") or []) if isinstance(item, dict)]
        total_de_plata = 0.0

        for cont in conturi:
            facturi_cont = [factura for factura in facturi if factura.id_cont == cont.id_cont]
            plati_cont = [plata for plata in plati if self._gaseste_id_cont(plata, [cont]) == cont.id_cont]
            contoare_cont = [contor for contor in contoare if self._gaseste_id_cont(contor, [cont]) == cont.id_cont]
            neachitate = [factura for factura in facturi_cont if factura.stare in {"neplatita", "scadenta"}]
            sold = round(sum(float(factura.date_brute.get("rest_plata") if factura.date_brute.get("rest_plata") is not None else factura.valoare or 0) for factura in neachitate), 2)
            total_de_plata += max(sold, 0.0)
            ultima_factura = facturi_cont[0] if facturi_cont else None
            ultima_plata = _ultima_dupa_data(plati_cont, "data")
            ultim_contor = _ultima_dupa_data(contoare_cont, "data")
            citire = _date_citire_cont(cont, contoare_cont, date_brute)
            urmatoarea_neachitata = _urmatoarea_factura_neachitata(facturi_cont)

            consumuri.extend([
                ConsumUtilitate("de_plata", sold, "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("sold_curent", sold, "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("factura_restanta", "da" if sold > 0 else "nu", None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_facturi", len(facturi_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_facturi_neachitate", len(neachitate), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_plati", len(plati_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                    ConsumUtilitate("numar_contoare", _numar_contoare_unice(contoare_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("citire_index_permisa", "da" if citire.get("permisa") else "nu", None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=citire),
                ConsumUtilitate("perioada_citire", citire.get("perioada"), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=citire),
                ConsumUtilitate("zile_pana_citire_index", citire.get("zile_pana"), "zile", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=citire),
            ])
            if ultima_factura:
                # Pentru ApaRegio, portalul afiseaza deseori facturile istorice
                # cu rest de plata gol si sold 0. In acest caz nu exista o factura
                # neachitata, dar data scadentei ultimei facturi este totusi utila
                # si exista explicit in pagina de facturi.
                scadenta_afisata = (
                    urmatoarea_neachitata.data_scadenta
                    if urmatoarea_neachitata and urmatoarea_neachitata.data_scadenta
                    else ultima_factura.data_scadenta
                )
                sursa_scadenta = urmatoarea_neachitata.date_brute if urmatoarea_neachitata else ultima_factura.date_brute
                consumuri.extend([
                    ConsumUtilitate("valoare_ultima_factura", ultima_factura.valoare, "RON", perioada=_date_text(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("id_ultima_factura", _numar_factura_afisat(ultima_factura), None, perioada=_date_text(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("data_ultima_factura", _date_text(ultima_factura.data_emitere), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("urmatoarea_scadenta", _date_text(scadenta_afisata), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=sursa_scadenta),
                ])
            if ultima_plata:
                consumuri.extend([
                    ConsumUtilitate("data_ultima_plata", ultima_plata.get("data"), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                    ConsumUtilitate("valoare_ultima_plata", _valoare_numerica(ultima_plata.get("valoare")), "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                ])
            if ultim_contor:
                consumuri.append(ConsumUtilitate("index_contor", _valoare_numerica(ultim_contor.get("index_nou") or ultim_contor.get("index")), "m³", perioada=ultim_contor.get("data"), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultim_contor))

        consumuri.extend([
            ConsumUtilitate("de_plata", round(total_de_plata, 2), "RON", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("sold_curent", round(total_de_plata, 2), "RON", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_conturi", len(conturi), "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_facturi", len(facturi), "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_facturi_neachitate", sum(1 for factura in facturi if factura.stare in {"neplatita", "scadenta"}), "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
        ])
        return consumuri


def _parse_html(html_text: str) -> _ParserHtmlSimplu:
    parser = _ParserHtmlSimplu()
    try:
        parser.feed(html_text or "")
    except Exception:
        _LOGGER.debug("Parsare HTML Aparegio incompletă", exc_info=True)
    return parser


def _text_html(html_text: str) -> str:
    if not html_text:
        return ""
    text = unescape(str(html_text or ""))
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", " ", text)
    text = re.sub(r"(?i)</(?:td|th|tr|div|span|p|li|option|a|label|h\d)>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return _curata_text(unescape(text))


def _curata_text(value: Any) -> str:
    text = unescape(str(value or "").replace("\xa0", " "))
    return re.sub(r"\s+", " ", text).strip()


def _normalizeaza_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


def _normalizeaza_potrivire(value: Any) -> str:
    text = _normalizeaza_text(value)
    return re.sub(r"[^a-z0-9]+", "", text)


def _pare_pagina_login(text: str) -> bool:
    normalizat = _normalizeaza_text(_text_html(text) if "<" in (text or "") else text)
    if not normalizat:
        return False
    return "autentificare" in normalizat and "sunteti autentificat" not in normalizat and ("parola" in normalizat or "email" in normalizat)


def _url_cu_parametri_adf(url: str, pagina_loopback: str | None = None) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    loopback = _extrage_parametri_loopback_adf(pagina_loopback or "")
    query.setdefault("_afrLoop", loopback.get("afr_loop") or str(int(datetime.utcnow().timestamp() * 1_000_000)))
    query.setdefault("_afrWindowMode", "2")
    query.setdefault("Adf-Window-Id", query.get("Adf-Window-Id") or loopback.get("window_id") or "w0")
    query.setdefault("Font-Size", "16")
    query.setdefault("Media-Type", "screen")
    query.setdefault("Media-Feature-Width", "1366")
    query.setdefault("Media-Feature-Height", "768")
    query.setdefault("Media-Feature-Device-Width", "1920")
    query.setdefault("Media-Feature-Device-Height", "1080")
    query.setdefault("Media-Feature-Color", "8")
    query.setdefault("Media-Feature-Color-Index", "0")
    query.setdefault("Media-Feature-Monochrome", "0")
    query.setdefault("Media-Feature-Resolution", "96")
    query.setdefault("Media-Featured-Grid", "0")
    query.setdefault("Media-Feature-Scan", "0")
    query.setdefault("Media-Feature-Orientation", "portrait")
    return urlunparse(parsed._replace(query=urlencode(query)))



def _extrage_parametri_loopback_adf(html_text: str) -> dict[str, str]:
    """Extrage valorile generate de pagina-stub ADF.

    Portalul EMSYS trimite initial o pagina care contine doar JavaScript.
    Browserul executa `AdfLoopbackUtils.runLoopback(...)` si revine apoi cu
    query string-ul real. Valoarea `_afrLoop` nu este un timestamp simplu;
    trebuie reutilizata exact din pagina-stub, altfel serverul poate intoarce
    din nou aceeasi pagina fara continut.
    """
    if not html_text:
        return {}
    rezultat: dict[str, str] = {}
    loop_match = re.search(r"[\"']_afrLoop[\"']\s*,\s*[\"']([^\"']+)[\"']", html_text)
    if loop_match:
        rezultat["afr_loop"] = loop_match.group(1)

    # In apelul EMSYS observat, valoarea implicita pentru fereastra ADF vine
    # imediat dupa argumentul `_afrPage`, de forma: '_afrPage', '', 'w0'.
    window_match = re.search(
        r"[\"']_afrPage[\"']\s*,\s*[\"'][^\"']*[\"']\s*,\s*[\"']([^\"']+)[\"']",
        html_text,
    )
    if window_match:
        rezultat["window_id"] = window_match.group(1)
    return rezultat

def _pare_pagina_js_neactiv(html_text: str) -> bool:
    return _este_pagina_loopback_adf(html_text)


def _este_pagina_loopback_adf(html_text: str) -> bool:
    if not html_text:
        return False
    raw = _normalizeaza_text(html_text)
    text = _normalizeaza_text(_text_html(html_text))
    are_script_loopback = "adfloopbackutils.runloopback" in raw or "_noloopbackerror_" in raw
    mesaj_js = (
        ("utilizeaza javascript" in text and "javascript activata" in text)
        or ("uses javascript" in text and "javascript enabled" in text)
        or ("javascript enabled browser" in text)
    )
    if not mesaj_js and not are_script_loopback:
        return False
    # Daca exista deja continut real, nu este doar pagina-stub, chiar daca
    # noscript-ul ADF ramane in HTML.
    if _contine_marker_cont(html_text) or _contine_marker_facturi(html_text):
        return False
    return are_script_loopback or len(_text_html(html_text)) < 300


def _are_stare_adf(html_text: str) -> bool:
    if not html_text:
        return False
    # Atentie: pagina-stub ADF contine textul "Adf-Window-Id" in scriptul
    # runLoopback, dar aceea NU este pagina reala. Consideram stare ADF reala
    # doar daca exista ViewState/form Trinidad sau markeri efectivi de continut.
    normalized_raw = _normalizeaza_text(html_text)
    return (
        "javax.faces.viewstate" in normalized_raw
        or "org.apache.myfaces.trinidad.faces.form" in normalized_raw
        or _contine_marker_cont(html_text)
        or _contine_marker_facturi(html_text)
    )


def _extrage_stare_adf(html_text: str) -> dict[str, str]:
    parser = _parse_html(html_text or "")
    form_id = parser.inputs.get("org.apache.myfaces.trinidad.faces.FORM") or "asdas"
    window_id = parser.inputs.get("Adf-Window-Id") or _extrage_dupa_pattern(html_text or "", r"Adf-Window-Id=([A-Za-z0-9_-]+)") or "w0"
    view_state = parser.inputs.get("javax.faces.ViewState") or _extrage_dupa_pattern(html_text or "", r'name="javax\.faces\.ViewState"\s+value="([^"]+)"')
    return {"form": form_id, "window_id": window_id, "view_state": view_state or ""}


def _contine_marker_cont(html_text: str) -> bool:
    text = _normalizeaza_text(_text_html(html_text or ""))
    return "punct consum" in text and "contract" in text and "client" in text


def _contine_marker_facturi(html_text: str) -> bool:
    text = _normalizeaza_text(_text_html(html_text or ""))
    return ("facturi in sold" in text or "factura" in text) and "contract" in text


def _log_diag_aparegio(
    pagini: dict[str, str],
    conturi: list[dict[str, Any]],
    facturi: list[dict[str, Any]],
    plati: list[dict[str, Any]],
    contoare: list[dict[str, Any]],
) -> None:
    welcome = pagini.get("welcome") or ""
    consum = pagini.get("consum") or ""
    plati_html = pagini.get("plati") or ""
    transmitere = pagini.get("transmitere") or ""
    info = {
        "conturi": len(conturi),
        "facturi": len(facturi),
        "plati": len(plati),
        "contoare": len(contoare),
        "welcome_len": len(welcome),
        "consum_len": len(consum),
        "plati_len": len(plati_html),
        "transmitere_len": len(transmitere),
        "welcome_markers": _markere_pagina(welcome),
        "consum_markers": _markere_pagina(consum),
        "plati_markers": _markere_pagina(plati_html),
        "transmitere_markers": _markere_pagina(transmitere),
        "welcome_js_stub": _pare_pagina_js_neactiv(welcome),
        "transmitere_js_stub": _pare_pagina_js_neactiv(transmitere),
    }
    if conturi or facturi or plati or contoare:
        _LOGGER.debug("[APAREGIO DIAG] %s", _mascheaza_diag(str(info)))
        return
    snippet_welcome = _fragment_diag(welcome)
    snippet_transmitere = _fragment_diag(transmitere)
    _LOGGER.debug(
        "[APAREGIO DIAG] fara date parsate: %s welcome_snippet=%s transmitere_snippet=%s",
        _mascheaza_diag(str(info)),
        snippet_welcome,
        snippet_transmitere,
    )


def _markere_pagina(html_text: str) -> dict[str, bool]:
    text = _normalizeaza_text(_text_html(html_text or ""))
    return {
        "login": "autentificare" in text and "parola" in text,
        "facturi": "facturi" in text or "factura" in text,
        "facturi_tbl": "facturitbl" in _normalizeaza_potrivire(html_text),
        "transmitere": "transmitere index" in text,
        "client": "client" in text,
        "contract": "contract" in text,
        "punct_consum": "punct consum" in text,
        "view_state": "javax.faces.viewstate" in _normalizeaza_text(html_text),
    }


def _fragment_diag(html_text: str, limita: int = 700) -> str:
    text = _text_html(html_text or "")
    text = re.sub(r"(?i)(Sunteti autentificat ca:)\s+\S+", r"\1 ***", text)
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "***@***", text, flags=re.I)
    text = re.sub(r"\b\d{6,}\b", lambda m: m.group(0)[:2] + "***" + m.group(0)[-2:], text)
    return _curata_text(text[:limita])


def _mascheaza_diag(text: str) -> str:
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "***@***", text, flags=re.I)
    text = re.sub(r"\b\d{6,}\b", lambda m: m.group(0)[:2] + "***" + m.group(0)[-2:], text)
    return text


def _extrage_conturi(pagini: dict[str, str]) -> list[dict[str, Any]]:
    rezultate: list[dict[str, Any]] = []
    vazute: set[str] = set()
    for cheie_pagina in ("transmitere", "welcome"):
        text = _text_html(pagini.get(cheie_pagina) or "")
        for item in _conturi_din_text(text):
            cheie = str(item.get("id_cont") or item.get("punct_consum") or item.get("id_contract") or "")
            if cheie and cheie not in vazute:
                vazute.add(cheie)
                item["sursa"] = cheie_pagina
                rezultate.append(item)
    return rezultate


def _conturi_din_text(text: str) -> list[dict[str, Any]]:
    rezultate: list[dict[str, Any]] = []
    vazute: set[str] = set()

    def _adauga_cont(
        *,
        id_client: str | None,
        id_contract: str | None,
        punct: str | None = None,
        nume_client: str | None = None,
        adresa: str | None = None,
    ) -> None:
        client = _curata_text(id_client or "")
        contract = _curata_text(id_contract or "")
        punct_consum = _curata_text(punct or "")
        nume = _curata_text(nume_client or "")
        adresa_curata = _curata_adresa(adresa or "")
        id_cont = punct_consum or client or contract
        cheie = _normalizeaza_potrivire("|".join(value for value in (id_cont, contract, adresa_curata) if value))
        if not id_cont or not cheie or cheie in vazute:
            return
        vazute.add(cheie)
        rezultate.append({
            "id_client": client,
            "nume_client": nume,
            "id_contract": contract,
            "contract": contract,
            "punct_consum": punct_consum,
            "id_cont": id_cont,
            "adresa": adresa_curata,
        })

    # Varianta completa, folosita in pagina de transmitere index.
    pattern_complet = re.compile(
        r"Client\s+([0-9A-Za-z_.-]+)\s+(.+?)\s+Contract\s+([0-9A-Za-z_.-]+)\s+Punct\s+consum\s+([0-9A-Za-z_.-]+)\s+(.+?)(?=\s+(?:Client\s+[0-9]|Reseteaza|Executa|copyRight|OK\b|Anulare\b|$))",
        flags=re.I | re.S,
    )
    for match in pattern_complet.finditer(text or ""):
        _adauga_cont(
            id_client=match.group(1),
            nume_client=match.group(2),
            id_contract=match.group(3),
            punct=match.group(4),
            adresa=match.group(5),
        )

    # Pagina de facturi nu contine mereu "Punct consum". In formatul vazut in
    # browser avem doar Client + nume/adresa + Contract.
    pattern_facturi = re.compile(
        r"Client\s+(\d{5,})\s+(.{0,250}?)\s+Contract\s+(\d{5,})",
        flags=re.I | re.S,
    )
    for match in pattern_facturi.finditer(text or ""):
        fragment_client = _curata_text(match.group(2))
        nume_client = fragment_client
        adresa = ""
        paranteza = re.search(r"\(([^()]*)\)", fragment_client)
        if paranteza:
            adresa = paranteza.group(1)
            nume_client = _curata_text(fragment_client[: paranteza.start()])
        _adauga_cont(
            id_client=match.group(1),
            nume_client=nume_client,
            id_contract=match.group(3),
            adresa=adresa,
        )

    # Fallback din randurile de factura: Client Contract GJAAPA ...
    for match in re.finditer(
        r"\b(?P<client>\d{5,})\s+(?P<contract>\d{5,}(?:/[0-9\-]+)?)\s+GJAAPA\s+\d+\b",
        text or "",
        flags=re.I,
    ):
        _adauga_cont(id_client=match.group("client"), id_contract=match.group("contract"))

    return rezultate

def _curata_adresa(text: str) -> str:
    text = re.sub(r"\b(?:Reseteaza|Executa|copyRight|Emsys|OK|Anulare)\b.*$", "", text or "", flags=re.I).strip()
    return _curata_text(text)


def _extrage_facturi(pagini: dict[str, str], conturi: list[dict[str, Any]]) -> list[dict[str, Any]]:
    facturi: list[dict[str, Any]] = []
    vazute: set[str] = set()
    html_text = pagini.get("welcome") or ""
    parser = _parse_html(html_text)
    linkuri = _linkuri_pdf(parser)

    def _adauga(item: dict[str, Any]) -> None:
        cheie = _cheie_factura(item)
        if cheie and cheie not in vazute:
            vazute.add(cheie)
            facturi.append(item)

    for tabel in parser.tables:
        # In ADF/Trinidad antetul si corpul tabelului pot fi randate in
        # tabele HTML diferite. Din acest motiv incercam si randurile care
        # nu au antet explicit, daca arata ca randuri de factura.
        if not _pare_tabel_facturi(tabel) and not any(_pare_rand_factura_aparegio(rand) for rand in tabel):
            continue
        for item in _facturi_din_tabel(tabel, conturi, linkuri):
            _adauga(item)

    text = _text_html(html_text)
    for item in _facturi_aparegio_din_text(text, conturi):
        _adauga(item)

    if not facturi:
        for item in _facturi_din_text(text, conturi):
            _adauga(item)

    return facturi

def _pare_tabel_facturi(tabel: list[list[str]]) -> bool:
    text = _normalizeaza_text(" ".join(" ".join(rand) for rand in tabel[:3]))
    if not text:
        return False
    return "fact" in text and any(token in text for token in ("scad", "sold", "rest", "valoare", "suma", "total"))


def _facturi_din_tabel(tabel: list[list[str]], conturi: list[dict[str, Any]], linkuri: dict[str, str]) -> list[dict[str, Any]]:
    if not tabel:
        return []
    headers = [_normalizeaza_text(cell) for cell in tabel[0]]
    start = 1 if any("fact" in h or "scad" in h or "valoare" in h or "rest" in h for h in headers) else 0
    rezultate: list[dict[str, Any]] = []
    for rand in tabel[start:]:
        if not any(rand):
            continue
        item = _factura_din_rand(rand, headers if start == 1 else [], conturi)
        if item:
            numar = str(item.get("numar_factura") or item.get("id_factura") or "")
            item["url"] = linkuri.get(_cheie_factura(numar))
            item["raw_row"] = {f"col_{idx}": value for idx, value in enumerate(rand)}
            rezultate.append(item)
    return rezultate


def _factura_din_rand(rand: list[str], headers: list[str], conturi: list[dict[str, Any]]) -> dict[str, Any] | None:
    rand_curat = [_curata_text(cell) for cell in rand]
    valori = {_cheie_header(headers[idx] if idx < len(headers) else f"col_{idx}"): cell for idx, cell in enumerate(rand_curat)}
    text = _curata_text(" ".join(rand_curat))
    if not text:
        return None

    if _pare_rand_factura_aparegio(rand_curat):
        return _factura_din_rand_aparegio(rand_curat, conturi)

    if "fact" not in _normalizeaza_text(text) and not _are_numar_factura(text):
        return None

    numar = _primul(valori, "numar_factura", "factura", "document") or _numar_factura_din_text(text)
    if not numar:
        return None
    date_gasite = _date_din_text(text)
    data_emitere = _primul(valori, "data_emitere", "emitere", "data") or (date_gasite[0] if date_gasite else None)
    data_scadenta = _primul(valori, "data_scadenta", "scadenta") or (date_gasite[-1] if len(date_gasite) > 1 else None)
    valoare = _primul(valori, "valoare", "valoare_factura", "suma", "total") or _prima_valoare_bani(text)
    rest = _primul(valori, "rest", "rest_plata", "sold", "de_plata", "suma_scadenta") or _ultima_valoare_bani(text)
    id_contract = _primul(valori, "contract", "id_contract") or _contract_din_text(text) or _contract_default(conturi)
    id_client = _primul(valori, "client", "id_client") or _client_din_text(text)
    adresa = _primul(valori, "adresa", "punct_consum", "loc_consum") or _adresa_din_text(text)

    return {
        "id_factura": numar,
        "numar_factura": numar,
        "titlu": f"Factura {numar}",
        "data_emitere": data_emitere,
        "data_scadenta": data_scadenta,
        "valoare": valoare,
        "rest_plata": rest,
        "sold": rest,
        "id_contract": id_contract,
        "client": id_client,
        "adresa": adresa,
        "stare_text": text,
    }


def _pare_rand_factura_aparegio(rand: list[str]) -> bool:
    celule = [_curata_text(cell) for cell in rand]
    text = " ".join(celule)
    return bool(
        len(celule) >= 7
        and _date_din_text(text)
        and any(re.search(r"\bGJAAPA\s*\d+\b", cell, flags=re.I) for cell in celule)
        and any(_valoare_numerica(cell) is not None for cell in celule)
    )


def _factura_din_rand_aparegio(rand: list[str], conturi: list[dict[str, Any]]) -> dict[str, Any] | None:
    # Format observat in ADF:
    # [pdf, client, contract, factura, data_emitere, data_scadenta, valoare, rest]
    celule = [_curata_text(cell) for cell in rand]
    if len(celule) < 7:
        return None
    id_client = celule[1] if len(celule) > 1 else None
    id_contract = celule[2] if len(celule) > 2 else None
    numar = celule[3] if len(celule) > 3 else None
    data_emitere = celule[4] if len(celule) > 4 else None
    data_scadenta = celule[5] if len(celule) > 5 else None
    valoare = celule[6] if len(celule) > 6 else None
    rest = celule[7] if len(celule) > 7 and _curata_text(celule[7]) else None
    if not numar or not re.search(r"GJAAPA\s*\d+", numar, flags=re.I):
        return None
    return {
        "id_factura": numar,
        "numar_factura": numar,
        "titlu": f"Factura {numar}",
        "data_emitere": data_emitere,
        "data_scadenta": data_scadenta,
        "valoare": valoare,
        "rest_plata": rest,
        "sold": rest,
        "id_contract": id_contract or _contract_default(conturi),
        "client": id_client,
        "stare_text": " ".join(celule),
    }


def _facturi_aparegio_din_text(text: str, conturi: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extrage randurile GJAAPA din textul randat de pagina ADF.

    Tabelul Oracle ADF poate fi fragmentat in multe tabele HTML, dar textul
    final randat in browser pastreaza randurile de factura in format stabil:
    Client, Contract, Factura, Data emitere, Data scadenta, Valoare, Rest.
    """
    rezultate: list[dict[str, Any]] = []
    if not text:
        return rezultate

    text_curat = _curata_text(text)
    pattern = re.compile(
        r"\b(?P<client>\d{5,})\s+"
        r"(?P<contract>\d{5,}(?:/[0-9\-]+)?)\s+"
        r"(?P<factura>GJAAPA\s+\d+)\s+"
        r"(?P<emitere>\d{2}\.\d{2}\.\d{4})\s+"
        r"(?P<scadenta>\d{2}\.\d{2}\.\d{4})\s+"
        r"(?P<valoare>-?\d+(?:[.,]\d{1,2})?)"
        r"(?:\s+(?P<rest>-?\d+(?:[.,]\d{1,2})?))?"
        r"(?=\s+(?:\d{5,}\s+\d{5,}(?:/[0-9\-]+)?\s+GJAAPA|copyRight|$))",
        flags=re.I,
    )
    for match in pattern.finditer(text_curat):
        numar = _curata_text(match.group("factura"))
        rest = _curata_text(match.group("rest") or "")
        rezultate.append({
            "id_factura": numar,
            "numar_factura": numar,
            "titlu": f"Factura {numar}",
            "data_emitere": match.group("emitere"),
            "data_scadenta": match.group("scadenta"),
            "valoare": match.group("valoare"),
            "rest_plata": rest if rest else None,
            "sold": rest if rest else None,
            "id_contract": match.group("contract"),
            "contract": match.group("contract"),
            "client": match.group("client"),
            "id_client": match.group("client"),
            "stare_text": match.group(0),
        })
    return rezultate


def _facturi_din_text(text: str, conturi: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rezultate: list[dict[str, Any]] = []
    if not text:
        return rezultate
    pattern = re.compile(
        r"(?:Factura|Factur[ăa])\s*(?:nr\.?|num[ăa]r)?\s*[:#-]?\s*([0-9A-Za-z_.-]{4,})(.*?)(?=(?:Factura|Factur[ăa])\s*(?:nr\.?|num[ăa]r)?\s*[:#-]?\s*[0-9A-Za-z_.-]{4,}|$)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(text):
        fragment = _curata_text(match.group(0))
        numar = _curata_text(match.group(1))
        if not numar:
            continue
        date_gasite = _date_din_text(fragment)
        valori = _valori_bani(fragment)
        rezultate.append({
            "id_factura": numar,
            "numar_factura": numar,
            "titlu": f"Factura {numar}",
            "data_emitere": date_gasite[0] if date_gasite else None,
            "data_scadenta": date_gasite[-1] if len(date_gasite) > 1 else None,
            "valoare": valori[0] if valori else None,
            "rest_plata": valori[-1] if valori else None,
            "sold": valori[-1] if valori else None,
            "id_contract": _contract_din_text(fragment) or _contract_default(conturi),
            "stare_text": fragment,
        })
    return rezultate


def _extrage_plati(pagini: dict[str, str], conturi: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = _text_html(pagini.get("plati") or "")
    rezultate: list[dict[str, Any]] = []
    if not text:
        return rezultate

    # Raspunsurile ADF partiale nu includ mereu titlul tabului "Istoric plati";
    # uneori contin doar randurile tabelului. De aceea nu conditionam parserul de
    # prezenta titlului, ci cautam direct randurile de plata.
    vazute: set[str] = set()
    pattern = re.compile(
        r"(?P<data>\d{2}\.\d{2}\.\d{4})\s+"
        r"(?P<document>.+?)\s+"
        r"(?P<valoare>-?\d+(?:[.,]\d{1,2})?)\s+"
        r"(?P<canal>BANCA|TERTI|CARD|ONLINE|PORTAL|BT|TRANSILVAN|GHISEU|CASIERIE|OP)\s+"
        r"(?P<client>\d{5,})\s+"
        r"(?P<stare>[A-ZĂÂÎȘȚa-zăâîșț ]+?)"
        r"(?=\s+\d{2}\.\d{2}\.\d{4}|\s+Daca plata|\s+copyRight|$)",
        flags=re.I,
    )
    for match in pattern.finditer(_curata_text(text)):
        data_plata = _data_din_text(match.group("data"))
        item = {
            "data": _date_text(data_plata),
            "document": _curata_text(match.group("document")),
            "valoare": _curata_text(match.group("valoare")),
            "canal": _curata_text(match.group("canal")),
            "client": _curata_text(match.group("client")),
            "id_client": _curata_text(match.group("client")),
            "stare": _curata_text(match.group("stare")),
            "id_contract": _contract_default(conturi),
        }
        cheie = _normalizeaza_potrivire("|".join(str(item.get(key) or "") for key in ("data", "document", "valoare", "client")))
        if cheie and cheie not in vazute:
            vazute.add(cheie)
            rezultate.append(item)
    return rezultate


def _extrage_contoare(pagini: dict[str, str], conturi: list[dict[str, Any]]) -> list[dict[str, Any]]:
    texte = [_text_html(pagini.get("consum") or ""), _text_html(pagini.get("transmitere") or "")]
    rezultate: list[dict[str, Any]] = []
    vazute: set[str] = set()

    for text in texte:
        text_curat = _curata_text(text)
        if not text_curat:
            continue
        client = _extrage_dupa_pattern(text_curat, r"Client\*?\s+([0-9A-Za-z_.-]+)")
        contract = _extrage_dupa_pattern(text_curat, r"Contract\*?\s+([0-9A-Za-z_.-]+)") or _contract_default(conturi)
        punct = _extrage_dupa_pattern(text_curat, r"Punct\s+consum\*?\s+([0-9A-Za-z_.-]+)")
        cont_default = conturi[0] if conturi else {}
        client = client or str(cont_default.get("id_client") or cont_default.get("id_cont") or "") or None
        punct = punct or str(cont_default.get("punct_consum") or cont_default.get("id_cont") or client or "") or None

        pattern_consum = re.compile(
            r"(?P<serie>\d{5,})\s+"
            r"(?P<data>\d{2}\.\d{2}\.\d{4})\s+"
            r"(?P<index_vechi>-?\d+(?:[.,]\d{1,2})?)\s+"
            r"(?P<index_nou>-?\d+(?:[.,]\d{1,2})?)\s+"
            r"(?P<consum>-?\d+(?:[.,]\d{1,2})?)\s+"
            r"(?P<tip>[A-ZĂÂÎȘȚ]+)"
            r"(?:\s+(?P<factura>GJAAPA\s+\d+)\s+(?P<data_emitere>\d{2}\.\d{2}\.\d{4}))?"
            r"(?=\s+\d{5,}\s+\d{2}\.\d{2}\.\d{4}|\s+copyRight|$)",
            flags=re.I,
        )
        for match in pattern_consum.finditer(text_curat):
            data_consum = _data_din_text(match.group("data"))
            item = {
                "serie": _curata_text(match.group("serie")),
                "contor": _curata_text(match.group("serie")),
                "index": _curata_text(match.group("index_nou")),
                "index_vechi": _curata_text(match.group("index_vechi")),
                "index_nou": _curata_text(match.group("index_nou")),
                "consum": _curata_text(match.group("consum")),
                "tip_consum": _curata_text(match.group("tip")),
                "factura": _curata_text(match.group("factura") or ""),
                "data_emitere": _date_text(_data_din_text(match.group("data_emitere"))) if match.group("data_emitere") else None,
                "data": _date_text(data_consum),
                "id_client": client,
                "client": client,
                "id_contract": contract,
                "contract": contract,
                "punct_consum": punct,
                "id_cont": punct or client,
            }
            cheie = _normalizeaza_potrivire("|".join(str(item.get(key) or "") for key in ("serie", "data", "index_nou")))
            if cheie and cheie not in vazute:
                vazute.add(cheie)
                rezultate.append(item)

        if rezultate:
            continue
        for fragment in re.split(r"(?i)\bContor\b", text_curat):
            if not fragment or not re.search(r"Index", fragment, flags=re.I):
                continue
            serie = _extrage_dupa_pattern(fragment, r"(?:nr\.?|serie)?\s*[:#-]?\s*([0-9A-Za-z_.-]+)")
            index_nou = _extrage_dupa_pattern(fragment, r"Index\s+(?:nou|curent)?\s*[:#-]?\s*(-?\d+(?:[.,]\d+)?)")
            data = _date_din_text(fragment)
            item = {
                "serie": serie,
                "contor": serie,
                "index": index_nou,
                "index_nou": index_nou,
                "data": data[-1] if data else None,
                "id_contract": contract,
                "client": client,
                "id_client": client,
                "punct_consum": punct,
                "id_cont": punct or client,
                "raw_text": fragment[:1000],
            }
            cheie = _normalizeaza_potrivire("|".join(str(item.get(key) or "") for key in ("serie", "data", "index_nou")))
            if cheie and cheie not in vazute:
                vazute.add(cheie)
                rezultate.append(item)
    return rezultate


def _numar_contoare_unice(contoare: list[dict[str, Any]]) -> int:
    serii = {str(item.get("serie") or item.get("contor") or "").strip() for item in contoare if item.get("serie") or item.get("contor")}
    return len(serii) if serii else len(contoare)


def _date_citire_cont(cont: ContUtilitate, contoare: list[dict[str, Any]], date_brute: dict[str, Any]) -> dict[str, Any]:
    text = _text_html((date_brute.get("pagini") or {}).get("transmitere") or "")
    normalizat = _normalizeaza_text(text)
    azi = date.today()
    prima_zi_citire = 24
    permisa_calendar = azi.day >= prima_zi_citire
    zile_pana = 0 if permisa_calendar else prima_zi_citire - azi.day
    perioada = "Dupa data de 24 ale fiecarei luni"
    mesaj = None
    if "nu va aflati in perioada" in normalizat:
        mesaj_match = re.search(r"Nu\s+va\s+aflati\s+in\s+perioada.+?(?:lunii|luna|$)", text, flags=re.I | re.S)
        mesaj = _curata_text(mesaj_match.group(0)) if mesaj_match else "Nu va aflati in perioada de transmitere index"
        permisa = False
    elif "transmitere index" in normalizat and not _este_pagina_loopback_adf((date_brute.get("pagini") or {}).get("transmitere") or ""):
        permisa = permisa_calendar
    else:
        # Pagina de transmitere este greu de incarcat prin ADF; folosim regula
        # confirmata din portal: transmiterea indexului se face dupa data de 24.
        permisa = permisa_calendar
    return {"permisa": permisa, "mesaj": mesaj, "perioada": mesaj or perioada, "zile_pana": zile_pana, "numar_contoare": _numar_contoare_unice(contoare)}


def _linkuri_pdf(parser: _ParserHtmlSimplu) -> dict[str, str]:
    linkuri: dict[str, str] = {}
    for link in parser.links:
        href = link.get("href") or ""
        text = link.get("text") or ""
        if not href:
            continue
        if "pdf" not in _normalizeaza_text(href + " " + text) and "UtilFileServlet" not in href:
            continue
        cheie = _cheie_factura(text) or _cheie_factura(href)
        if cheie:
            linkuri[cheie] = urljoin(URL_BAZA + "/", href)
    return linkuri


def _cheie_header(value: str) -> str:
    text = _normalizeaza_text(value)
    if "scad" in text:
        return "data_scadenta"
    if "emit" in text:
        return "data_emitere"
    if "nr" in text and "fact" in text:
        return "numar_factura"
    if "numar" in text and "fact" in text:
        return "numar_factura"
    if "fact" in text:
        return "factura"
    if "rest" in text:
        return "rest_plata"
    if "sold" in text:
        return "sold"
    if "total" in text and "plata" in text:
        return "de_plata"
    if "valoare" in text:
        return "valoare"
    if "suma" in text:
        return "suma"
    if "contract" in text:
        return "contract"
    if "client" in text:
        return "client"
    if "punct" in text or "loc" in text:
        return "punct_consum"
    if "adresa" in text:
        return "adresa"
    if "data" == text or text.startswith("data "):
        return "data"
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_") or "coloana"


def _primul(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _are_numar_factura(text: str) -> bool:
    return bool(re.search(r"\b(?:nr|numar|factura|document)\b.*?\b[0-9A-Za-z_.-]{4,}\b", text or "", flags=re.I))


def _numar_factura_din_text(text: str) -> str | None:
    patterns = (
        r"(?:Factura|Factur[ăa]|Document|Nr\.?)\s*(?:nr\.?|num[ăa]r)?\s*[:#-]?\s*([A-Z]{0,5}[-_/]?[0-9][0-9A-Za-z_.-]{3,})",
        r"\b([A-Z]{2,5}[-_/]?[0-9]{4,})\b",
        r"\b([0-9]{5,})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            return _curata_text(match.group(1))
    return None


def _contract_din_text(text: str) -> str | None:
    return _extrage_dupa_pattern(text, r"Contract\s*[:#-]?\s*([0-9A-Za-z_.-]+)")


def _client_din_text(text: str) -> str | None:
    return _extrage_dupa_pattern(text, r"Client\s*[:#-]?\s*([0-9A-Za-z_.-]+)")


def _adresa_din_text(text: str) -> str | None:
    match = re.search(r"(?:Adres[ăa]|Punct\s+consum|Loc\s+consum)\s*[:#-]?\s*(.+?)(?:\s+(?:Factura|Factur[ăa]|Contract|Client|Data|Scaden[țt][ăa]|Valoare|Rest|Sold)\b|$)", text or "", flags=re.I | re.S)
    return _curata_adresa(match.group(1)) if match else None


def _contract_default(conturi: list[dict[str, Any]]) -> str | None:
    if len(conturi) == 1:
        return str(conturi[0].get("id_contract") or conturi[0].get("contract") or "").strip() or None
    return None


def _date_din_text(text: str) -> list[str]:
    rezultate: list[str] = []
    for match in re.finditer(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{4})\b", text or ""):
        data = _data_din_text(match.group(1))
        if data:
            iso = data.isoformat()
            if iso not in rezultate:
                rezultate.append(iso)
    return rezultate


def _data_din_text(value: Any) -> date | None:
    text = _curata_text(value)
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _data_sigura(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    return _data_din_text(value)


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _valori_bani(text: str) -> list[str]:
    rezultate: list[str] = []
    for match in re.finditer(r"(-?\d{1,3}(?:[ .]\d{3})*(?:[,.]\d{1,2})|-?\d+(?:[,.]\d{1,2})?)\s*(?:RON|Lei|LEI|lei)?", text or "", flags=re.I):
        raw = match.group(1)
        if not raw or len(raw) > 18:
            continue
        before = (text or "")[max(0, match.start() - 16):match.start()].lower()
        after = (text or "")[match.end():match.end() + 16].lower()
        if not any(token in before + after for token in ("ron", "lei", "valoare", "suma", "total", "rest", "sold", "plata", "factur")):
            continue
        rezultate.append(raw)
    return rezultate


def _prima_valoare_bani(text: str) -> str | None:
    valori = _valori_bani(text)
    return valori[0] if valori else None


def _ultima_valoare_bani(text: str) -> str | None:
    valori = _valori_bani(text)
    return valori[-1] if valori else None


def _valoare_numerica(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = _curata_text(value)
    if not text:
        return None
    text = re.sub(r"(?i)\b(ron|lei)\b", "", text).strip()
    text = text.replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _stare_factura(item: dict[str, Any], rest: float | None) -> str:
    status_text = _normalizeaza_text(" ".join(str(item.get(key) or "") for key in ("stare", "status", "stare_text")))
    if rest is not None:
        return "neplatita" if rest > 0 else "platita"
    if any(token in status_text for token in ("neachit", "neplat", "rest", "de plata", "scadent")):
        return "neplatita"
    if any(token in status_text for token in ("achitat", "platit", "stins")):
        return "platita"
    return "necunoscuta"



def _urmatoarea_factura_neachitata(facturi: list[FacturaUtilitate]) -> FacturaUtilitate | None:
    candidati = [factura for factura in facturi if factura.stare in {"neplatita", "scadenta"} and factura.data_scadenta]
    if not candidati:
        return None
    azi = date.today()
    viitoare = [factura for factura in candidati if factura.data_scadenta and factura.data_scadenta >= azi]
    return min(viitoare or candidati, key=lambda factura: factura.data_scadenta or date.max)

def _ultima_dupa_data(items: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    def sort_key(item: dict[str, Any]) -> date:
        return _data_din_text(item.get(key)) or date.min
    return max(items, key=sort_key) if items else None


def _numar_factura_afisat(factura: FacturaUtilitate) -> str:
    raw = factura.date_brute or {}
    return str(raw.get("numar_factura") or factura.id_factura or factura.titlu or "").strip()


def _cheie_factura(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("numar_factura") or value.get("id_factura") or value.get("titlu") or value.get("raw_text") or value.get("stare_text")
    text = _normalizeaza_potrivire(str(value or "")).upper()
    match = re.search(r"([A-Z]{2,5}[0-9]{3,}|[0-9]{5,})", text)
    return match.group(1) if match else text


def _extrage_dupa_pattern(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text or "", flags=re.I | re.S)
    return _curata_text(match.group(1)) if match else None


def _nume_cont_aparegio(adresa: str | None, id_contract: str | None, id_cont: str | None) -> str:
    baza = _curata_text(adresa or "") or "Loc consum"
    detalii = _curata_text(id_contract or id_cont or "")
    if detalii and detalii not in baza:
        return f"{baza} ({detalii})"
    return baza or detalii or "Loc consum"
