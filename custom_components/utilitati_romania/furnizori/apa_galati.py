from __future__ import annotations

from datetime import date, datetime
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

URL_BAZA = "https://portal.apa-canal.ro"
URL_LOGIN = f"{URL_BAZA}/app/"
URL_MENIU = f"{URL_BAZA}/amc/"
URL_CONTRACTE = f"{URL_BAZA}/grct/"
URL_LOCATII = f"{URL_BAZA}/grlc/"
URL_CONSUMURI = f"{URL_BAZA}/grcr/"
URL_FACTURI = f"{URL_BAZA}/grfr/"
URL_PLATI = f"{URL_BAZA}/grnc/"
URL_CLIENT = f"{URL_BAZA}/grcl/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

# Portalul are uneori lanțul certificatului incomplet în mediul Home Assistant,
# deși se deschide normal în browser. Dezactivăm verificarea strictă doar
# pentru acest furnizor, ca să păstrăm restul sesiunii neschimbate.
APA_GALATI_SSL_VERIFY = False

# Scriptcase cere un script_case_init pentru fiecare aplicație deschisă. Valorile
# nu identifică date personale; sunt folosite doar ca inițializare a paginii.
INIT_MODULE = {
    "contracte": "4253",
    "locatii": "4018",
    "consumuri": "6530",
    "facturi": "2549",
    "plati": "4788",
    "client": "8730",
}

MENU_ITEM_MODULE = {
    "contracte": "item_5",
    "locatii": "item_6",
    "consumuri": "item_7",
    "facturi": "item_8",
    "plati": "item_9",
    "client": "item_4",
}

URL_FORM_LOGIN_SCRIPTCASE = (
    f"{URL_MENIU}amc_form_php.php?sc_item_menu=item_3"
    "&sc_apl_menu=app&sc_apl_link=%2F&sc_usa_grupo="
)


def _debug_login(etapa: str, **date: Any) -> None:
    """Diagnostic temporar pentru fluxul Apă Canal Galați."""
    try:
        _LOGGER.debug("[APA GALATI DIAG LOGIN] %s: %s", etapa, json.dumps(date, ensure_ascii=False, default=str))
    except Exception:
        _LOGGER.debug("[APA GALATI DIAG LOGIN] %s: %s", etapa, date)



def _fragment_log(text: str, limita: int = 220) -> str:
    """Returnează un fragment scurt, curățat, util pentru diagnostic.

    Nu este folosit pentru date personale, ci pentru răspunsuri tehnice Scriptcase.
    """
    curat = re.sub(r"\s+", " ", _curata_text(text or "")).strip()
    return curat[:limita]

def _rezumat_text(text: str) -> dict[str, Any]:
    normal = _curata_text(text)
    return {
        "lungime": len(text or ""),
        "pare_login": _pare_login(text or ""),
        "are_amc": "amc" in (text or "").lower(),
        "are_meniu": "meniu" in normal.lower(),
        "are_client": "client" in normal.lower(),
        "are_eroare": "eroare" in normal.lower() or "error" in normal.lower(),
    }


def _rezumat_cookies(sesiune: aiohttp.ClientSession) -> list[str]:
    rezultat: list[str] = []
    for cookie in sesiune.cookie_jar:
        if cookie.key:
            rezultat.append(cookie.key)
    return sorted(set(rezultat))

async def _citeste_text_raspuns(raspuns: aiohttp.ClientResponse) -> str:
    """Citește răspunsurile portalului Apă Canal Galați cu fallback de encoding.

    Portalul Scriptcase poate returna pagini HTML în encoding Windows-1250 / ISO-8859-2,
    dar fără charset corect în header. aiohttp încearcă implicit UTF-8 și poate arunca
    UnicodeDecodeError. Citim bytes și decodăm controlat, ca să nu pice integrarea.
    """
    continut = await raspuns.read()
    if not continut:
        return ""

    encodari: list[str] = []
    charset = raspuns.charset
    if charset:
        encodari.append(charset)
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


def _debug_date(etapa: str, **date: Any) -> None:
    """Diagnostic temporar pentru datele Apă Canal Galați."""
    try:
        _LOGGER.debug("[APA GALATI DIAG DATA] %s: %s", etapa, json.dumps(date, ensure_ascii=False, default=str))
    except Exception:
        _LOGGER.debug("[APA GALATI DIAG DATA] %s: %s", etapa, date)



def _campuri_scriptcase(html_text: str, limita: int = 24) -> list[str]:
    campuri: set[str] = set()
    for ascuns, camp, _index in re.findall(
        r'<span[^>]+id=["\']id_sc_field_(Hidden_)?([a-zA-Z0-9_]+)_(\d+)["\']',
        html_text or "",
        re.I | re.S,
    ):
        campuri.add(("hidden_" if ascuns else "") + camp)
    return sorted(campuri)[:limita]


def _numara_spanuri_scriptcase(html_text: str) -> int:
    return len(re.findall(r'id_sc_field_', html_text or "", re.I))


class EroareApiApaGalati(Exception):
    pass


class EroareAutentificareApaGalati(EroareApiApaGalati):
    pass


class EroareConectareApaGalati(EroareApiApaGalati):
    pass


class EroareRaspunsApaGalati(EroareApiApaGalati):
    pass


class ClientApiApaGalati:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._autentificat = False
        self._script_case_session: str | None = None
        # Scriptcase generează dinamic script_case_init la deschiderea paginii /app/.
        # Nu folosim o valoare hardcodată, pentru că se schimbă de la o sesiune la alta.
        self._script_case_init: str = "1"

    def _headers(self, *, referer: str | None = None, ajax: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": USER_AGENT,
            "Referer": referer or URL_LOGIN,
            "Origin": URL_BAZA,
        }
        if ajax:
            headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"})
        return headers

    async def _deschide_aplicatia_login_scriptcase(self) -> str:
        """Deschide aplicația Scriptcase de login prin formularul meniului.

        În browser, /app/ nu este accesată ca prim pas izolat, ci prin
        /amc/amc_form_php.php?sc_item_menu=item_3..., care setează corect
        nmgp_parms și script_case_session. Dacă sărim peste acest pas,
        validarea AJAX poate întoarce eroare, iar meniul de după login rămâne
        doar un shell gol.
        """
        try:
            async with self._sesiune.get(
                URL_FORM_LOGIN_SCRIPTCASE,
                headers=self._headers(referer=URL_MENIU),
                ssl=APA_GALATI_SSL_VERIFY,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                formular = await _citeste_text_raspuns(raspuns)
                campuri = _extrage_campuri_formular_scriptcase(formular)
                action = _extrage_form_action(formular) or URL_LOGIN
                url_action = urljoin(URL_BAZA, action)
                if campuri.get("script_case_session"):
                    self._script_case_session = campuri.get("script_case_session")
                if campuri.get("script_case_init"):
                    # Pentru formularul de meniu valoarea poate fi 1; init-ul real
                    # al aplicației login rămâne în nmgp_parms și este 1907.
                    init_din_parms = _init_din_nmgp_parms(campuri.get("nmgp_parms") or "")
                    self._script_case_init = init_din_parms or self._script_case_init or "1907"
                _debug_login(
                    "GET formular login Scriptcase",
                    status=raspuns.status,
                    lungime=len(formular or ""),
                    action=url_action.replace(URL_BAZA, ""),
                    are_campuri=bool(campuri),
                    campuri=sorted(campuri.keys())[:8],
                    script_case_init=self._script_case_init,
                    are_script_case_session=bool(self._script_case_session),
                    cookies=_rezumat_cookies(self._sesiune),
                )
                if raspuns.status >= 400 or not campuri:
                    return ""
        except Exception as err:
            _debug_login("GET formular login Scriptcase exceptie", tip=type(err).__name__, mesaj=str(err)[:160])
            return ""

        try:
            async with self._sesiune.post(
                url_action,
                headers={**self._headers(referer=URL_FORM_LOGIN_SCRIPTCASE), "Content-Type": "application/x-www-form-urlencoded"},
                data=campuri,
                ssl=APA_GALATI_SSL_VERIFY,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await _citeste_text_raspuns(raspuns)
                _debug_login(
                    "POST formular login Scriptcase",
                    status=raspuns.status,
                    url_final=str(raspuns.url),
                    script_case_init=self._script_case_init,
                    are_script_case_session=bool(self._script_case_session),
                    cookies=_rezumat_cookies(self._sesiune),
                    **_rezumat_text(text),
                )
                if raspuns.status >= 400:
                    return ""
                return text
        except Exception as err:
            _debug_login("POST formular login Scriptcase exceptie", tip=type(err).__name__, mesaj=str(err)[:160])
            return ""

    async def async_login(self) -> None:
        try:
            _debug_login(
                "pornire",
                are_utilizator=bool(self._utilizator),
                lungime_utilizator=len(self._utilizator or ""),
                are_parola=bool(self._parola),
                lungime_parola=len(self._parola or ""),
            )
            # Fluxul real din browser începe cu GET direct pe /app/, iar pagina
            # returnată conține script_case_init-ul dinamic și csrf_token-ul.
            # Nu pornim loginul din amc_form_php înainte de autentificare, fiindcă
            # acolo putem obține un init greșit/vechi și rămânem cu sesiune parțială.
            pagina_login = ""
            try:
                async with self._sesiune.get(
                        URL_LOGIN,
                        headers=self._headers(),
                        ssl=APA_GALATI_SSL_VERIFY,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as raspuns:
                        pagina_login = await _citeste_text_raspuns(raspuns)
                        _debug_login(
                            "GET login",
                            status=raspuns.status,
                            url_final=str(raspuns.url),
                            cookies=_rezumat_cookies(self._sesiune),
                            **_rezumat_text(pagina_login),
                        )
                        if raspuns.status >= 400:
                            pagina_login = ""
            except (aiohttp.ClientError, TimeoutError) as err:
                _debug_login("GET login exceptie", tip=type(err).__name__, mesaj=str(err)[:160], cookies=_rezumat_cookies(self._sesiune))

            if not pagina_login:
                # Unele instanțe Scriptcase nu răspund stabil la GET direct pe /app/.
                # Browserul deschide aplicația de login prin POST din meniul Scriptcase,
                # așa că încercăm același bootstrap înainte să declarăm conexiunea eșuată.
                bootstrap_data = {
                    "nmgp_parms": f"script_case_init?#?{self._script_case_init or '1'}?#?script_case_session?#?{self._script_case_session or ''}",
                    "script_case_init": self._script_case_init or "1907",
                    "script_case_session": self._script_case_session or "",
                    "nm_apl_menu": "amc",
                }
                try:
                    async with self._sesiune.post(
                        URL_LOGIN,
                        headers={**self._headers(referer=URL_MENIU), "Content-Type": "application/x-www-form-urlencoded"},
                        data=bootstrap_data,
                        ssl=APA_GALATI_SSL_VERIFY,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as raspuns:
                        pagina_login = await _citeste_text_raspuns(raspuns)
                        _debug_login(
                            "POST bootstrap login",
                            status=raspuns.status,
                            url_final=str(raspuns.url),
                            cookies=_rezumat_cookies(self._sesiune),
                            **_rezumat_text(pagina_login),
                        )
                        if raspuns.status >= 400:
                            raise EroareConectareApaGalati(f"Apa Canal Galați a returnat HTTP {raspuns.status} la login")
                except (aiohttp.ClientError, TimeoutError) as err:
                    _debug_login("POST bootstrap login exceptie", tip=type(err).__name__, mesaj=str(err)[:160], cookies=_rezumat_cookies(self._sesiune))
                    raise EroareConectareApaGalati(f"Eroare de conectare la Apa Canal Galați: {err}") from err

            if not pagina_login:
                raise EroareConectareApaGalati("Apa Canal Galați nu a returnat pagina de login")

            # Extragem datele Scriptcase direct din formularul real de login.
            # În unele răspunsuri, valoarea poate fi în input hidden cu atributul
            # value înainte de name, caz în care regexul simplu nu o prinde.
            campuri_login = _extrage_campuri_formular_scriptcase(pagina_login)
            init_din_formular = (
                campuri_login.get("script_case_init")
                or _init_din_nmgp_parms(campuri_login.get("nmgp_parms") or "")
            )
            sesiune_din_formular = campuri_login.get("script_case_session")
            csrf_din_formular = campuri_login.get("csrf_token")

            sesiune_din_pagina = sesiune_din_formular or _extrage_script_case_session(pagina_login)
            if sesiune_din_pagina:
                self._script_case_session = sesiune_din_pagina
            elif not self._script_case_session:
                self._script_case_session = self._cookie_scriptcase_session()

            init_din_pagina = init_din_formular or _extrage_script_case_init(pagina_login)
            if init_din_pagina and init_din_pagina != "1":
                self._script_case_init = init_din_pagina
            elif not self._script_case_init:
                self._script_case_init = "1"

            csrf = csrf_din_formular or _extrage_csrf(pagina_login)
            _debug_login(
                "date login extrase",
                script_case_init=self._script_case_init,
                init_din_formular=init_din_formular,
                init_din_pagina=init_din_pagina,
                campuri_login=sorted(campuri_login.keys())[:12],
                are_script_case_session=bool(self._script_case_session),
                lungime_script_case_session=len(self._script_case_session or ""),
                are_csrf=bool(csrf),
                lungime_csrf=len(csrf or ""),
            )

            # Validările AJAX sunt folosite de aplicația Scriptcase înainte de submit.
            # Nu sunt suficiente pentru autentificare, dar ajută la menținerea aceluiași flux.
            await self._post_login_ajax("ajax_app_validate_pswd", [self._parola, self._script_case_init])
            await self._post_login_ajax("ajax_app_validate_login", [self._utilizator, self._script_case_init])
            if csrf:
                await self._post_login_ajax(
                    "ajax_app_submit_form",
                    [
                        self._utilizator,
                        self._parola,
                        "1",
                        "",
                        "alterar",
                        "",
                        "",
                        "",
                        self._script_case_init,
                        csrf,
                        "OK",
                    ],
                )
            else:
                _debug_login("ajax submit omis", motiv="csrf_lipsa")

            # În browser, submitul final către /app/ apare cu status 0, dar este totuși
            # inițiat înainte de deschiderea meniului /amc/. Îl executăm și noi, însă nu
            # folosim pagina returnată drept criteriu de succes, pentru că Scriptcase poate
            # întoarce din nou pagina de login chiar dacă sesiunea a fost modificată.
            data_finala: list[tuple[str, str]] = [
                ("nm_form_submit", "1"),
                ("nmgp_idioma_novo", ""),
                ("nmgp_schema_f", ""),
                ("nmgp_url_saida", ""),
                ("bok", "OK"),
                ("nmgp_opcao", "alterar"),
                ("nmgp_ancora", ""),
                ("nmgp_num_form", ""),
                ("nmgp_parms", ""),
                ("script_case_init", self._script_case_init or "1"),
                ("script_case_session", self._script_case_session or ""),
                ("NM_cancel_return_new", ""),
            ]
            if csrf:
                data_finala.append(("csrf_token", csrf))
            data_finala.extend((("login", self._utilizator), ("pswd", self._parola)))
            try:
                async with self._sesiune.post(
                    URL_LOGIN,
                    headers={
                        **self._headers(referer=URL_LOGIN),
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Upgrade-Insecure-Requests": "1",
                    },
                    data=data_finala,
                    allow_redirects=False,
                    ssl=APA_GALATI_SSL_VERIFY,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as raspuns:
                    text_final = await _citeste_text_raspuns(raspuns)
                    _debug_login(
                        "POST login final executat",
                        status=raspuns.status,
                        url_final=str(raspuns.url),
                        cookies=_rezumat_cookies(self._sesiune),
                        fragment=_fragment_log(text_final),
                        **_rezumat_text(text_final),
                    )
                    # Nu suprascriem sesiunea validă cu valori goale din pagina de login.
                    self._script_case_session = _extrage_script_case_session(text_final) or self._script_case_session or self._cookie_scriptcase_session()
            except Exception as err:
                # Un timeout aici poate fi acceptabil, pentru că browserul abandonează
                # navigarea finală; verificarea reală se face imediat prin /amc/.
                _debug_login("POST login final exceptie tolerata", tip=type(err).__name__, mesaj=str(err)[:160])

            pagina_meniu = await self._deschide_meniu(necesita_login=False)
            _debug_login(
                "verificare meniu dupa login",
                autentificat=not _pare_login(pagina_meniu),
                script_case_init=self._script_case_init,
                are_script_case_session=bool(self._script_case_session),
                **_rezumat_text(pagina_meniu),
            )
            meniu_real = _meniu_autentificat(pagina_meniu)
            _debug_login("validare meniu real", meniu_real=meniu_real, lungime=len(pagina_meniu or ""))
            if _pare_login(pagina_meniu):
                raise EroareAutentificareApaGalati("Credentialele Apa Canal Galați nu au fost acceptate")
            if not meniu_real:
                _debug_login(
                    "meniu shell respins",
                    motiv="Autentificarea nu a produs meniul real; modulele ar intoarce doar shell-uri goale",
                    lungime=len(pagina_meniu or ""),
                )
                raise EroareAutentificareApaGalati("Apa Canal Galați nu a returnat meniul real după autentificare")
            self._autentificat = True
        except EroareApiApaGalati:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareApaGalati(f"Eroare de conectare la Apa Canal Galați: {err}") from err
        except TimeoutError as err:
            raise EroareConectareApaGalati("Timeout la Apa Canal Galați") from err

    async def _post_login_ajax(self, actiune: str, argumente: list[str]) -> str:
        payload: list[tuple[str, str]] = [("rs", actiune), ("rst", ""), ("rsrnd", str(int(datetime.now().timestamp() * 1000)))]
        payload.extend(("rsargs[]", str(arg)) for arg in argumente)
        try:
            async with self._sesiune.post(
                URL_LOGIN,
                headers=self._headers(referer=URL_LOGIN, ajax=True),
                data=payload,
                ssl=APA_GALATI_SSL_VERIFY,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as raspuns:
                text = await _citeste_text_raspuns(raspuns)
                _debug_login(
                    f"AJAX {actiune}",
                    status=raspuns.status,
                    lungime=len(text or ""),
                    are_error="error" in (text or "").lower() or "erro" in (text or "").lower(),
                    are_login="login" in (text or "").lower(),
                    are_csrf="csrf" in (text or "").lower(),
                    fragment=_fragment_log(text) if actiune == "ajax_app_submit_form" else "",
                    cookies=_rezumat_cookies(self._sesiune),
                )
                return text
        except Exception as err:
            _debug_login(f"AJAX {actiune} exceptie", tip=type(err).__name__, mesaj=str(err)[:160])
            _LOGGER.debug("Validarea AJAX Apa Canal Galați %s nu a putut fi executată", actiune, exc_info=True)
            return ""

    def _cookie_scriptcase_session(self) -> str | None:
        for cookie in self._sesiune.cookie_jar:
            if "script" in cookie.key.lower() or "session" in cookie.key.lower() or cookie.key.upper().startswith("PHP"):
                if cookie.value:
                    return cookie.value
        return None

    async def _deschide_meniu(self, *, necesita_login: bool = True) -> str:
        if necesita_login and not self._autentificat:
            await self.async_login()
        data = {
            "nmgp_parms": "",
            "nmgp_url_saida": "/app/",
            "script_case_init": self._script_case_init or "1",
            "script_case_session": self._script_case_session or "",
        }
        async with self._sesiune.post(
            URL_MENIU,
            headers={**self._headers(referer=URL_LOGIN), "Content-Type": "application/x-www-form-urlencoded"},
            data=data,
            ssl=APA_GALATI_SSL_VERIFY,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as raspuns:
            text = await _citeste_text_raspuns(raspuns)
            _debug_login(
                "POST meniu",
                status=raspuns.status,
                url_final=str(raspuns.url),
                script_case_init=self._script_case_init,
                are_script_case_session=bool(self._script_case_session),
                cookies=_rezumat_cookies(self._sesiune),
                **_rezumat_text(text),
            )
            if raspuns.status >= 400:
                raise EroareConectareApaGalati(f"Apa Canal Galați a returnat HTTP {raspuns.status} la meniu")
            self._script_case_session = _extrage_script_case_session(text) or self._script_case_session
            return text

    async def _pregateste_modul(self, cheie: str, init: str) -> str:
        """Deschide aplicația intermediară Scriptcase pentru modulul cerut.

        În browser, accesul la un grid nu se face direct prin POST /grfr/ sau /grlc/.
        Fluxul corect este:
        1. GET /amc/amc_form_php.php?...sc_apl_menu=spc...
        2. POST /spc/ cu nm_run_menu și script_case_init-ul modulului
        3. POST /modul/ cu nmgp_url_saida=/spc/

        Fără pasul /spc/, portalul returnează doar un shell scurt, fără tabele.
        """
        item = MENU_ITEM_MODULE.get(cheie)
        if not item:
            return init

        url_form = f"{URL_MENIU}amc_form_php.php?sc_item_menu={item}&sc_apl_menu=spc&sc_apl_link=%2F&sc_usa_grupo="
        try:
            async with self._sesiune.get(
                url_form,
                headers=self._headers(referer=URL_MENIU),
                ssl=APA_GALATI_SSL_VERIFY,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as raspuns:
                text = await _citeste_text_raspuns(raspuns)
                campuri_form = _extrage_campuri_formular_scriptcase(text)
                init_form = _init_din_nmgp_parms(campuri_form.get("nmgp_parms") or "") or campuri_form.get("script_case_init")
                sesiune_form = campuri_form.get("script_case_session")
                if init_form and init_form != "1":
                    init = init_form
                if sesiune_form:
                    self._script_case_session = sesiune_form
                _debug_date(
                    "pregatire_modul_form",
                    cheie=cheie,
                    init=init,
                    init_form=init_form,
                    status=raspuns.status,
                    lungime=len(text or ""),
                    pare_login=_pare_login(text or ""),
                    campuri=sorted(campuri_form.keys())[:8],
                    are_script_case_init="script_case_init" in (text or ""),
                )
        except Exception as err:
            _debug_date("pregatire_modul_form_exceptie", cheie=cheie, tip=type(err).__name__, mesaj=str(err)[:160])

        nmgp_parms = (
            f"nm_run_menu?#?1?@?nm_apl_menu?#?amc?@?"
            f"script_case_init?#?{init}?@?"
            f"script_case_session?#?{self._script_case_session or ''}"
        )
        data_spc = {
            "nmgp_parms": nmgp_parms,
            "script_case_init": init,
            "script_case_session": self._script_case_session or "",
            "nm_apl_menu": "amc",
        }
        try:
            async with self._sesiune.post(
                f"{URL_BAZA}/spc/",
                headers={**self._headers(referer=URL_MENIU), "Content-Type": "application/x-www-form-urlencoded"},
                data=data_spc,
                ssl=APA_GALATI_SSL_VERIFY,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as raspuns:
                text = await _citeste_text_raspuns(raspuns)
                _debug_date(
                    "pregatire_modul_spc",
                    cheie=cheie,
                    init=init,
                    status=raspuns.status,
                    lungime=len(text or ""),
                    pare_login=_pare_login(text or ""),
                    are_script_case_init="script_case_init" in (text or ""),
                )
        except Exception as err:
            _debug_date("pregatire_modul_spc_exceptie", cheie=cheie, tip=type(err).__name__, mesaj=str(err)[:160])
        return init

    async def _pagina_modul(self, url: str, init: str, *, cheie: str) -> str:
        if not self._autentificat:
            await self.async_login()
        init = await self._pregateste_modul(cheie, init)
        data = {
            "nmgp_parms": "",
            "nmgp_url_saida": "/spc/",
            "script_case_init": init,
            "script_case_session": self._script_case_session or "",
        }
        try:
            async with self._sesiune.post(
                url,
                headers={**self._headers(referer=f"{URL_BAZA}/spc/"), "Content-Type": "application/x-www-form-urlencoded"},
                data=data,
                ssl=APA_GALATI_SSL_VERIFY,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await _citeste_text_raspuns(raspuns)
                _LOGGER.debug(
                    "[APA GALATI DEBUG] Modul %s: status=%s, lungime=%s, pare_login=%s, spanuri=%s",
                    cheie,
                    raspuns.status,
                    len(text or ""),
                    _pare_login(text),
                    _numara_spanuri_scriptcase(text),
                )
                if raspuns.status in (401, 403) or _pare_login(text):
                    self._autentificat = False
                    await self.async_login()
                    return await self._pagina_modul(url, init, cheie=cheie)
                if raspuns.status >= 400:
                    raise EroareRaspunsApaGalati(f"Apa Canal Galați a returnat HTTP {raspuns.status} pentru {cheie}")
                return text
        except EroareApiApaGalati:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareApaGalati(f"Eroare de conectare la Apa Canal Galați pentru {cheie}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareApaGalati(f"Timeout la Apa Canal Galați pentru {cheie}") from err

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        pagina_client = await self._pagina_modul(URL_CLIENT, INIT_MODULE["client"], cheie="client")
        return {"client": pagina_client}

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        pagini = {
            "contracte": await self._pagina_modul(URL_CONTRACTE, INIT_MODULE["contracte"], cheie="contracte"),
            "locatii": await self._pagina_modul(URL_LOCATII, INIT_MODULE["locatii"], cheie="locatii"),
            "consumuri": await self._pagina_modul(URL_CONSUMURI, INIT_MODULE["consumuri"], cheie="consumuri"),
            "facturi": await self._pagina_modul(URL_FACTURI, INIT_MODULE["facturi"], cheie="facturi"),
            "plati": await self._pagina_modul(URL_PLATI, INIT_MODULE["plati"], cheie="plati"),
            "client": await self._pagina_modul(URL_CLIENT, INIT_MODULE["client"], cheie="client"),
        }
        for cheie, html_text in pagini.items():
            _debug_date(
                "pagina",
                cheie=cheie,
                lungime=len(html_text or ""),
                pare_login=_pare_login(html_text or ""),
                spanuri=_numara_spanuri_scriptcase(html_text or ""),
                campuri=_campuri_scriptcase(html_text or ""),
            )
        date = {
            "client": _extrage_client(pagini["client"]),
            "contracte": _extrage_contracte(pagini["contracte"]),
            "locatii": _extrage_locatii(pagini["locatii"]),
            "consumuri": _extrage_consumuri(pagini["consumuri"]),
            "facturi": _extrage_facturi(pagini["facturi"]),
            "plati": _extrage_plati(pagini["plati"]),
            "pagini": pagini,
        }
        _LOGGER.debug(
            "[APA GALATI DEBUG] Date extrase: client=%s, contracte=%s, locatii=%s, consumuri=%s, facturi=%s, plati=%s",
            bool(date["client"]),
            len(date["contracte"]),
            len(date["locatii"]),
            len(date["consumuri"]),
            len(date["facturi"]),
            len(date["plati"]),
        )
        _debug_date(
            "rezultat_parser",
            client=bool(date["client"]),
            contracte=len(date["contracte"]),
            locatii=len(date["locatii"]),
            consumuri=len(date["consumuri"]),
            facturi=len(date["facturi"]),
            plati=len(date["plati"]),
            primul_contract=bool(date["contracte"][:1]),
            prima_locatie=bool(date["locatii"][:1]),
            prima_factura=bool(date["facturi"][:1]),
            prima_plata=bool(date["plati"][:1]),
        )
        return date


class ClientFurnizorApaGalati(ClientFurnizor):
    cheie_furnizor = "apa_galati"
    nume_prietenos = "Apă Canal Galați"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiApaGalati(self.sesiune, self.utilizator, self.parola)
        try:
            rezultat = await api.async_validate_credentials()
        except EroareAutentificareApaGalati as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaGalati as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaGalati as err:
            raise EroareParsare(str(err)) from err
        client = _extrage_client(rezultat.get("client") or "")
        return str(client.get("cod_id") or client.get("cod_client") or self.utilizator.strip().lower())

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiApaGalati(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificareApaGalati as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaGalati as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaGalati as err:
            raise EroareParsare(str(err)) from err

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute, conturi)
        consumuri = self._mapeaza_consumuri(date_brute, conturi, facturi)
        try:
            _LOGGER.debug(
                "[APA GALATI DIAG MAP] rezultat: %s",
                json.dumps(
                    {
                        "conturi": len(conturi),
                        "facturi": len(facturi),
                        "consumuri": len(consumuri),
                        "conturi_preview": [
                            {
                                "id_cont": getattr(cont, "id_cont", None),
                                "nume": getattr(cont, "nume", None),
                                "adresa": getattr(cont, "adresa", None),
                                "id_contract": getattr(cont, "id_contract", None),
                            }
                            for cont in conturi[:5]
                        ],
                        "facturi_preview": [
                            {
                                "id_factura": getattr(factura, "id_factura", None),
                                "id_cont": getattr(factura, "id_cont", None),
                                "valoare": getattr(factura, "valoare", None),
                                "emitere": getattr(factura, "data_emitere", None),
                                "scadenta": getattr(factura, "data_scadenta", None),
                                "stare": getattr(factura, "stare", None),
                            }
                            for factura in facturi[:5]
                        ],
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        except Exception:
            pass
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={"portal_url": URL_BAZA, "numar_locatii": len(conturi), "numar_facturi": len(facturi)},
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        client = date_brute.get("client") or {}
        contracte = date_brute.get("contracte") or []
        contract_default = contracte[0] if contracte else {}
        conturi: list[ContUtilitate] = []
        vazute: set[str] = set()
        for locatie in date_brute.get("locatii", []) or []:
            id_cont = str(locatie.get("cod_locatie") or "").strip()
            if not id_cont or id_cont in vazute:
                continue
            vazute.add(id_cont)
            nume = str(locatie.get("den_locatie") or client.get("nume") or id_cont).strip()
            adresa = _adresa_din_facturi(id_cont, date_brute.get("facturi") or [], nume_locatie=nume)
            raw = {**contract_default, **client, **locatie, "adresa_factura": adresa}
            conturi.append(
                ContUtilitate(
                    id_cont=id_cont,
                    nume=nume,
                    tip_cont="apa",
                    id_contract=str(contract_default.get("nr_contract") or id_cont),
                    adresa=adresa or nume,
                    stare=str(contract_default.get("stare") or locatie.get("stare") or "").strip() or None,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=raw,
                )
            )
        if not conturi:
            id_client = str(client.get("cod_id") or client.get("cod_client") or self.utilizator.strip().lower())
            conturi.append(
                ContUtilitate(
                    id_cont=id_client,
                    nume=str(client.get("nume") or "Apă Canal Galați"),
                    tip_cont="apa",
                    id_contract=str(contract_default.get("nr_contract") or id_client),
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute={**client, **contract_default},
                )
            )
        return conturi

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        cont_default = conturi[0] if conturi else None
        plati = [p for p in (date_brute.get("plati") or []) if isinstance(p, dict)]
        facturi: list[FacturaUtilitate] = []
        vazute: set[str] = set()
        for item in date_brute.get("facturi", []) or []:
            nr_doc = str(item.get("nr_doc_raw") or item.get("nr_doc") or "").strip()
            serie = str(item.get("serie_doc") or "").strip()
            id_factura = _curata_text(f"{serie} {nr_doc}".strip()) or nr_doc
            if not id_factura:
                continue
            cheie = _slug_simplu(id_factura)
            if cheie in vazute:
                continue
            vazute.add(cheie)
            id_cont = self._gaseste_id_cont(item, conturi)
            if not id_cont and cont_default and len(conturi) == 1:
                id_cont = cont_default.id_cont
            valoare = _numar(item.get("total_factura") or item.get("total_de_plata"))
            rest = _numar(item.get("suma_sold"))
            total_de_plata = _numar(item.get("total_de_plata"))
            stare = _stare_factura(item, rest, plati)
            raw = dict(item)
            plata_potrivita = _plata_potrivita_factura(item, plati) if plati else None
            if stare == "platita":
                raw["rest_plata"] = 0.0
            elif rest is not None and rest > 0:
                raw["rest_plata"] = rest
            elif total_de_plata is not None and total_de_plata > 0:
                raw["rest_plata"] = total_de_plata
            else:
                raw["rest_plata"] = valoare or 0.0
            raw["numar_factura"] = id_factura
            raw["plata_potrivita"] = bool(plata_potrivita)
            if plata_potrivita:
                raw["data_plata_potrivita"] = plata_potrivita.get("data")
                raw["valoare_plata_potrivita"] = plata_potrivita.get("suma")
            facturi.append(
                FacturaUtilitate(
                    id_factura=id_factura,
                    titlu=f"Factura {id_factura}",
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=_data(item.get("data_doc_iso") or item.get("data_doc")),
                    data_scadenta=_data(item.get("data_scadenta")),
                    stare=stare,
                    categorie="apa_canal",
                    id_cont=id_cont,
                    id_contract=str(item.get("cod_locatie") or id_cont or ""),
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=raw,
                )
            )
        facturi.sort(key=lambda f: f.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont(self, item: dict[str, Any], conturi: list[ContUtilitate]) -> str | None:
        cod = _slug_simplu(str(item.get("cod_locatie") or ""))
        parti_item = [
            item.get("den_locatie"),
            item.get("den_localitate"),
            item.get("adresa"),
            item.get("loc_consum"),
            item.get("punct_consum"),
        ]
        text_item = _slug_simplu(" ".join(str(parte or "") for parte in parti_item))
        adresa_item = _slug_simplu(str(item.get("adresa") or ""))

        for cont in conturi:
            raw = cont.date_brute or {}
            candidati = {
                _slug_simplu(cont.id_cont),
                _slug_simplu(cont.nume),
                _slug_simplu(cont.adresa or ""),
                _slug_simplu(str(raw.get("den_locatie") or "")),
                _slug_simplu(str(raw.get("adresa_factura") or "")),
                _slug_simplu(str(raw.get("adresa") or "")),
            }
            candidati = {c for c in candidati if c}
            if cod and cod in candidati:
                return cont.id_cont
            for candidat in candidati:
                if not candidat:
                    continue
                if text_item and (text_item in candidat or candidat in text_item):
                    return cont.id_cont
                if adresa_item and (adresa_item in candidat or candidat in adresa_item):
                    return cont.id_cont
        return None

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate], facturi: list[FacturaUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        plati = [p for p in (date_brute.get("plati") or []) if isinstance(p, dict)]
        citiri = [c for c in (date_brute.get("consumuri") or []) if isinstance(c, dict)]
        for cont in conturi:
            facturi_cont = [f for f in facturi if f.id_cont == cont.id_cont]
            plati_cont = [p for p in plati if self._gaseste_id_cont(p, [cont]) == cont.id_cont]
            if not plati_cont and len(conturi) == 1:
                plati_cont = plati
            citiri_cont = [c for c in citiri if self._gaseste_id_cont(c, [cont]) == cont.id_cont]
            neachitate = [f for f in facturi_cont if f.stare in {"neplatita", "scadenta"}]
            sold = round(sum(float(f.date_brute.get("rest_plata") or 0.0) for f in neachitate), 2)
            ultima_factura = facturi_cont[0] if facturi_cont else None
            ultima_plata = _ultima_dupa_data(plati_cont, "data")
            ultima_citire = _ultima_dupa_data(citiri_cont, "data_index_nou")

            valori = {
                "de_plata": sold,
                "sold_curent": sold,
                "valoare_ultima_factura": ultima_factura.valoare if ultima_factura else None,
                "id_ultima_factura": ultima_factura.id_factura if ultima_factura else None,
                "data_ultima_factura": ultima_factura.data_emitere.isoformat() if ultima_factura and ultima_factura.data_emitere else None,
                "urmatoarea_scadenta": ultima_factura.data_scadenta.isoformat() if ultima_factura and ultima_factura.data_scadenta else None,
                "factura_restanta": "da" if neachitate else "nu",
                "numar_facturi": len(facturi_cont),
                "numar_facturi_neachitate": len(neachitate),
                "numar_plati": len(plati_cont),
                "data_ultima_plata": ultima_plata.get("data") if ultima_plata else None,
                "valoare_ultima_plata": _numar(ultima_plata.get("suma")) if ultima_plata else None,
                "numar_contoare": len({c.get("serie_contor") for c in citiri_cont if c.get("serie_contor")}) or None,
                "index_contor": _numar(ultima_citire.get("index_nou")) if ultima_citire else None,
                "ultim_consum": _numar(ultima_citire.get("cantitate")) if ultima_citire else None,
                "cod_client": (date_brute.get("client") or {}).get("cod_client"),
                "nume_client": (date_brute.get("client") or {}).get("nume"),
            }
            for cheie, valoare in valori.items():
                consumuri.append(
                    ConsumUtilitate(
                        cheie=cheie,
                        valoare=valoare,
                        unitate=_unitate_consum(cheie),
                        id_cont=cont.id_cont,
                        tip_utilitate="apa",
                        tip_serviciu="apa_canal",
                    )
                )
        return consumuri


def _unitate_consum(cheie: str) -> str | None:
    if cheie in {"de_plata", "sold_curent", "valoare_ultima_factura", "valoare_ultima_plata"}:
        return "RON"
    if cheie in {"index_contor", "ultim_consum"}:
        return "m³"
    return None



def _extrage_form_action(text: str) -> str | None:
    match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', text or "", re.I)
    if match:
        return unescape(match.group(1))
    return None


def _extrage_campuri_formular_scriptcase(text: str) -> dict[str, str]:
    campuri: dict[str, str] = {}
    for input_tag in re.findall(r'<input[^>]+>', text or "", re.I | re.S):
        name_match = re.search(r'name=["\']?([^"\'\s>]+)', input_tag, re.I)
        if not name_match:
            continue
        value_match = re.search(r'value=["\']([^"\']*)["\']', input_tag, re.I | re.S)
        if not value_match:
            value_match = re.search(r'value=([^\s>]+)', input_tag, re.I | re.S)
        campuri[unescape(name_match.group(1))] = unescape(value_match.group(1) if value_match else "")
    return campuri


def _init_din_nmgp_parms(nmgp_parms: str) -> str | None:
    text = unescape(nmgp_parms or "")
    match = re.search(r'script_case_init\?#[\?]?(\d+)', text)
    if match:
        return match.group(1)
    match = re.search(r'script_case_init[^0-9]+(\d+)', text)
    if match:
        return match.group(1)
    return None

def _extrage_script_case_session(text: str) -> str | None:
    for pattern in (
        r"script_case_session[=:'\"\s]+([a-zA-Z0-9]+)",
        r"script_case_session%3F%23%3F([a-zA-Z0-9]+)",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _extrage_script_case_init(text: str) -> str | None:
    for pattern in (
        r"script_case_init[=:'\"\s]+(\d+)",
        r"script_case_init%3F%23%3F(\d+)",
        r"script_case_init%3D(\d+)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return None


def _extrage_csrf(text: str) -> str | None:
    for pattern in (
        r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']',
        r'csrf_token["\']?\s*[:=]\s*["\']([^"\']+)',
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return unescape(match.group(1))
    return None


def _pare_login(text: str) -> bool:
    normal = _curata_text(text).lower()
    return "login" in normal and "pswd" in text.lower() and "portal apa canal" in normal


def _meniu_autentificat(text: str) -> bool:
    html = text or ""
    normal = _curata_text(html).lower()
    return (
        "amc_form_php.php" in html
        or "sc_item_menu" in html
        or ("contracte" in normal and "locații" in normal and "facturi" in normal)
    )


def _spanuri_dupa_camp(html_text: str) -> dict[str, dict[int, str]]:
    rezultat: dict[str, dict[int, str]] = {}
    pattern = re.compile(
        r'<span[^>]+id=["\']id_sc_field_(Hidden_)?([a-zA-Z0-9_]+)_(\d+)["\'][^>]*>(.*?)</span>',
        re.I | re.S,
    )
    for ascuns, camp, index, continut in pattern.findall(html_text):
        cheie = f"hidden_{camp}" if ascuns else camp
        rezultat.setdefault(cheie, {})[int(index)] = _curata_text(_fara_taguri(continut))
    return rezultat


def _randuri_din_spanuri(html_text: str, campuri: list[str], *, prefera_vizibil: bool = False) -> list[dict[str, str]]:
    spanuri = _spanuri_dupa_camp(html_text)
    indecsi: set[int] = set()
    for camp in campuri:
        indecsi.update(spanuri.get(camp, {}).keys())
        indecsi.update(spanuri.get(f"hidden_{camp}", {}).keys())
    randuri: list[dict[str, str]] = []
    for index in sorted(indecsi):
        rand: dict[str, str] = {}
        for camp in campuri:
            valoare_vizibila = spanuri.get(camp, {}).get(index)
            valoare_ascunsa = spanuri.get(f"hidden_{camp}", {}).get(index)
            if prefera_vizibil:
                valoare = valoare_vizibila
                if valoare in (None, ""):
                    valoare = valoare_ascunsa or ""
            else:
                valoare = valoare_ascunsa
                if valoare in (None, ""):
                    valoare = valoare_vizibila or ""
            rand[camp] = valoare
        if any(rand.values()):
            randuri.append(rand)
    return randuri


def _extrage_client(html_text: str) -> dict[str, Any]:
    randuri = _randuri_din_spanuri(html_text, ["name", "login", "cod_client", "email", "tel_fix", "tel_mob"])
    if not randuri:
        return {}
    rand = randuri[0]
    return {
        "nume": rand.get("name"),
        "cod_id": rand.get("login"),
        "cod_client": rand.get("cod_client"),
        "email": rand.get("email"),
        "telefon_fix": rand.get("tel_fix"),
        "telefon_mobil": rand.get("tel_mob"),
    }


def _extrage_contracte(html_text: str) -> list[dict[str, Any]]:
    return [
        {
            "nr_contract": r.get("clienti_contracte_nr_contract"),
            "stare": r.get("clienti_contracte_stare"),
            "data_inceput": r.get("clienti_contracte_data_inc"),
            "data_sfarsit": r.get("clienti_contracte_data_sf"),
        }
        for r in _randuri_din_spanuri(
            html_text,
            ["clienti_contracte_nr_contract", "clienti_contracte_stare", "clienti_contracte_data_inc", "clienti_contracte_data_sf"],
        )
    ]


def _extrage_locatii(html_text: str) -> list[dict[str, Any]]:
    return [
        {
            "cod_locatie": r.get("clienti_locatii_cod_locatie"),
            "den_locatie": r.get("clienti_locatii_den_locatie"),
            "data_inceput": r.get("clienti_locatii_data_inc_lo"),
            "data_sfarsit": r.get("clienti_locatii_data_sf_lo"),
        }
        for r in _randuri_din_spanuri(
            html_text,
            ["clienti_locatii_cod_locatie", "clienti_locatii_den_locatie", "clienti_locatii_data_inc_lo", "clienti_locatii_data_sf_lo"],
        )
    ]


def _extrage_consumuri(html_text: str) -> list[dict[str, Any]]:
    rezultat: list[dict[str, Any]] = []
    for r in _randuri_din_spanuri(
        html_text,
        [
            "clienti_citiri_cod_locatie",
            "clienti_citiri_den_locatie",
            "clienti_citiri_serie_contor",
            "clienti_citiri_index_vechi",
            "clienti_citiri_data_vechi",
            "clienti_citiri_index_nou",
            "clienti_citiri_data_nou",
            "clienti_citiri_q",
        ],
    ):
        rezultat.append(
            {
                "cod_locatie": r.get("clienti_citiri_cod_locatie"),
                "den_locatie": r.get("clienti_citiri_den_locatie"),
                "serie_contor": r.get("clienti_citiri_serie_contor"),
                "index_vechi": r.get("clienti_citiri_index_vechi"),
                "data_index_vechi": r.get("clienti_citiri_data_vechi"),
                "index_nou": r.get("clienti_citiri_index_nou"),
                "data_index_nou": r.get("clienti_citiri_data_nou"),
                "cantitate": r.get("clienti_citiri_q"),
            }
        )
    return rezultat


def _extrage_facturi(html_text: str) -> list[dict[str, Any]]:
    """Extrage facturile din grila Scriptcase.

    Pentru Apa Canal Galati statusul real este in coloana suma_platita,
    afisata ca bifa in HTML. Pastram si HTML-ul brut al celulei pentru ca
    textul vizibil poate fi pierdut in unele variante de raspuns Scriptcase.
    """
    randuri_html = re.findall(
        r'<tr\b[^>]*id=["\']SC_ancor\d+["\'][^>]*>(.*?)</tr>',
        html_text or "",
        re.I | re.S,
    )
    rezultat: list[dict[str, Any]] = []
    for rand_html in randuri_html:
        if "id_sc_field_serie_doc_" not in rand_html or "id_sc_field_nr_doc_" not in rand_html:
            continue

        def camp(camp_name: str, *, prefera_vizibil: bool = True) -> str:
            return _camp_scriptcase_din_rand(rand_html, camp_name, prefera_vizibil=prefera_vizibil)

        serie = camp("serie_doc")
        nr_doc_afisat = camp("nr_doc")
        nr_doc_hidden = camp("nr_doc", prefera_vizibil=False)
        nr_doc_curat = (nr_doc_hidden or nr_doc_afisat or "").replace(".", "").strip()
        if not serie and not nr_doc_curat:
            continue

        stare_html = _camp_html_scriptcase_din_rand(rand_html, "suma_platita")
        stare_text = camp("suma_platita")
        platita_portal = _camp_plata_confirmata(stare_text, stare_html)

        rezultat.append(
            {
                "den_localitate": camp("den_localitate"),
                "adresa": camp("adresa"),
                "serie_doc": serie,
                "nr_doc": nr_doc_afisat or nr_doc_curat,
                "nr_doc_raw": nr_doc_curat,
                "data_doc": camp("data_doc"),
                "data_doc_iso": camp("data_doc", prefera_vizibil=False) if _este_data_iso(camp("data_doc", prefera_vizibil=False)) else None,
                "total_factura": camp("total_val_doc_b"),
                "suma_sold": camp("suma_sold"),
                "data_sold": camp("data_sold"),
                "total_de_plata": camp("total_suma"),
                "data_scadenta": camp("data_scad"),
                "data_gratie": camp("data_gratie"),
                "stare_factura": stare_text,
                "stare_factura_html": stare_html[:240],
                "platita_portal": platita_portal,
            }
        )

    if rezultat:
        return rezultat

    # Fallback pentru raspunsuri Scriptcase care nu includ randuri TR clasice.
    for r in _randuri_din_spanuri(
        html_text,
        [
            "den_localitate",
            "adresa",
            "serie_doc",
            "nr_doc",
            "data_doc",
            "total_val_doc_b",
            "suma_sold",
            "data_sold",
            "total_suma",
            "data_scad",
            "data_gratie",
            "suma_platita",
        ],
        prefera_vizibil=True,
    ):
        stare_text = r.get("suma_platita")
        rezultat.append(
            {
                "den_localitate": r.get("den_localitate"),
                "adresa": r.get("adresa"),
                "serie_doc": r.get("serie_doc"),
                "nr_doc": r.get("nr_doc"),
                "nr_doc_raw": r.get("nr_doc", "").replace(".", ""),
                "data_doc": r.get("data_doc"),
                "data_doc_iso": r.get("data_doc") if _este_data_iso(r.get("data_doc")) else None,
                "total_factura": r.get("total_val_doc_b"),
                "suma_sold": r.get("suma_sold"),
                "data_sold": r.get("data_sold"),
                "total_de_plata": r.get("total_suma"),
                "data_scadenta": r.get("data_scad"),
                "data_gratie": r.get("data_gratie"),
                "stare_factura": stare_text,
                "stare_factura_html": stare_text or "",
                "platita_portal": _camp_plata_confirmata(stare_text, stare_text),
            }
        )
    return rezultat


def _camp_html_scriptcase_din_rand(rand_html: str, camp: str) -> str:
    match = re.search(
        rf'<span[^>]+id=["\']id_sc_field_(?:Hidden_)?{re.escape(camp)}_\d+["\'][^>]*>(.*?)</span>',
        rand_html or "",
        re.I | re.S,
    )
    return match.group(1) if match else ""


def _camp_scriptcase_din_rand(rand_html: str, camp: str, *, prefera_vizibil: bool = True) -> str:
    valori: dict[str, str] = {"vizibil": "", "ascuns": ""}
    pattern = re.compile(
        rf'<span[^>]+id=["\']id_sc_field_(Hidden_)?{re.escape(camp)}_\d+["\'][^>]*>(.*?)</span>',
        re.I | re.S,
    )
    for ascuns, continut in pattern.findall(rand_html or ""):
        cheie = "ascuns" if ascuns else "vizibil"
        valori[cheie] = _curata_text(_fara_taguri(continut))
    if prefera_vizibil:
        return valori["vizibil"] or valori["ascuns"] or ""
    return valori["ascuns"] or valori["vizibil"] or ""


def _camp_plata_confirmata(text: Any, html_text: Any = None) -> bool:
    combinat = f"{text or ''} {html_text or ''}".lower()
    return any(marcaj in combinat for marcaj in ("✔", "✓", "platit", "achitat", "#00c000", "color:#00c000", "color: #00c000"))


def _extrage_plati(html_text: str) -> list[dict[str, Any]]:
    return [
        {
            "serie": r.get("serie_doc_inc"),
            "numar": r.get("nr_doc_inc"),
            "data": r.get("data_doc_inc"),
            "suma": r.get("suma_t"),
        }
        for r in _randuri_din_spanuri(html_text, ["serie_doc_inc", "nr_doc_inc", "data_doc_inc", "suma_t"])
    ]


def _adresa_din_facturi(id_cont: str, facturi: list[dict[str, Any]], *, nume_locatie: str | None = None) -> str | None:
    tinta = _slug_simplu(nume_locatie or "")
    for factura in facturi:
        cod_factura = str(factura.get("cod_locatie") or "").strip()
        adresa = _curata_text(f"{factura.get('den_localitate') or ''}, {factura.get('adresa') or ''}").strip(", ")
        if cod_factura and cod_factura == str(id_cont).strip():
            return adresa or None
        if tinta:
            adresa_norm = _slug_simplu(adresa)
            if adresa_norm and (adresa_norm in tinta or tinta in adresa_norm):
                return adresa or None
    return None

def _plata_potrivita_factura(item: dict[str, Any], plati: list[dict[str, Any]]) -> dict[str, Any] | None:
    valoare_factura = _numar(item.get("total_factura") or item.get("total_de_plata"))
    data_emitere = _data(item.get("data_doc_iso") or item.get("data_doc"))
    if valoare_factura is None or valoare_factura <= 0:
        return None

    potriviri: list[tuple[date, dict[str, Any]]] = []
    for plata in plati:
        if not isinstance(plata, dict):
            continue
        valoare_plata = _numar(plata.get("suma"))
        data_plata = _data(plata.get("data"))
        if valoare_plata is None or data_plata is None:
            continue
        if abs(float(valoare_plata) - float(valoare_factura)) > 0.02:
            continue
        if data_emitere and data_plata < data_emitere:
            continue
        potriviri.append((data_plata, plata))

    if not potriviri:
        return None
    potriviri.sort(key=lambda item_plata: item_plata[0])
    return potriviri[0][1]


def _stare_factura(item: dict[str, Any], rest: float | None, plati: list[dict[str, Any]] | None = None) -> str:
    scadenta = _data(item.get("data_scadenta"))
    azi = date.today()

    if item.get("platita_portal") or _camp_plata_confirmata(item.get("stare_factura"), item.get("stare_factura_html")):
        return "platita"

    # Fallback de siguranta pentru raspunsuri in care Scriptcase nu returneaza
    # bifa ca text. Folosim istoricul de plati doar ca supliment, nu ca sursa
    # principala.
    if plati is not None and _plata_potrivita_factura(item, plati):
        return "platita"

    return "scadenta" if scadenta and scadenta < azi else "neplatita"


def _ultima_dupa_data(items: list[dict[str, Any]], camp_data: str) -> dict[str, Any] | None:
    if not items:
        return None
    return max(items, key=lambda item: _data(item.get(camp_data)) or date.min)


def _data(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _este_data_iso(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "").strip()))


def _numar(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("\xa0", " ")
    text = re.sub(r"[^0-9,.-]", "", text)
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


def _fara_taguri(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def _curata_text(text: Any) -> str:
    text = unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    return text


def _slug_simplu(text: Any) -> str:
    text = _curata_text(text).lower()
    text = re.sub(r"[^a-z0-9ăâîșţț]+", "_", text)
    return text.strip("_")
