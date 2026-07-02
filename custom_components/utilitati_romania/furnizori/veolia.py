from __future__ import annotations

from datetime import date, datetime
import json
import logging
import re
from typing import Any

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://clienti-vrsi.veolia.ro"
URL_LOGIN = f"{URL_BAZA}/login-submit"
URL_SUMAR = f"{URL_BAZA}/sumar"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

ENDPOINTS = {
    "coduri_client": "/get-client-codes",
    "locatii": "/get-consumption-locations",
    "sumar_facturi": "/get-last-invoice-and-total-payments",
    "ultim_index": "/get-last-index",
    "istoric_index": "/get-index-history",
    "facturi": "/get-invoices",
    "facturi_neachitate": "/get-unpaid-invoices",
    "total_plati": "/get-total-payments",
    "plati": "/get-payments",
    "contracte": "/get-contracts",
}


class EroareApiVeolia(Exception):
    pass


class EroareAutentificareVeolia(EroareApiVeolia):
    pass


class EroareConectareVeolia(EroareApiVeolia):
    pass


class EroareRaspunsVeolia(EroareApiVeolia):
    pass


def _debug(etapa: str, **date: Any) -> None:
    """Diagnostic temporar pentru furnizor nou."""
    sigur: dict[str, Any] = {}
    for cheie, valoare in date.items():
        if cheie.lower() in {"password", "parola", "token", "email", "utilizator"}:
            continue
        if isinstance(valoare, str) and len(valoare) > 180:
            sigur[cheie] = f"{valoare[:180]}..."
        else:
            sigur[cheie] = valoare
    _LOGGER.debug("[VEOLIA DEBUG] %s: %s", etapa, json.dumps(sigur, ensure_ascii=False, default=str))


def _primul(*valori: Any) -> Any:
    for valoare in valori:
        if valoare not in (None, ""):
            return valoare
    return None


def _get(obj: Any, *chei: str) -> Any:
    if not isinstance(obj, dict):
        return None
    pentru_cautare = {str(k).lower(): v for k, v in obj.items()}
    for cheie in chei:
        if cheie in obj and obj[cheie] not in (None, ""):
            return obj[cheie]
        valoare = pentru_cautare.get(cheie.lower())
        if valoare not in (None, ""):
            return valoare
    return None


def _lista_din_raspuns(date: Any, *chei: str) -> list[Any]:
    if isinstance(date, list):
        return date
    if not isinstance(date, dict):
        return []
    continut = date.get("content")
    if isinstance(continut, list):
        return continut
    if isinstance(continut, dict):
        for cheie in chei:
            valoare = _get(continut, cheie)
            if isinstance(valoare, list):
                return valoare
        for valoare in continut.values():
            if isinstance(valoare, list):
                return valoare
    for cheie in chei:
        valoare = _get(date, cheie)
        if isinstance(valoare, list):
            return valoare
    return []


def _continut_din_raspuns(date: Any) -> Any:
    if isinstance(date, dict) and "content" in date:
        return date.get("content")
    return date


def _numar(valoare: Any) -> float | None:
    if valoare in (None, ""):
        return None
    if isinstance(valoare, (int, float)):
        return round(float(valoare), 2)
    text = str(valoare).strip()
    if not text:
        return None
    text = text.replace("Lei", "").replace("RON", "").replace("m3", "").replace("m³", "")
    text = text.replace("\xa0", " ").strip()
    text = re.sub(r"[^0-9,.-]", "", text)
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _data(valoare: Any) -> date | None:
    if valoare in (None, ""):
        return None
    if isinstance(valoare, date) and not isinstance(valoare, datetime):
        return valoare
    text = str(valoare).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _text(valoare: Any) -> str | None:
    if valoare in (None, ""):
        return None
    return re.sub(r"\s+", " ", str(valoare)).strip() or None




def _extrage_atribute_html(tag: str) -> dict[str, str]:
    return {
        match.group(1).lower(): match.group(2)
        for match in re.finditer(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["\']([^"\']*)["\']', tag)
    }


def _extrage_csrf_token(html: str) -> str | None:
    if not html:
        return None

    for tag in re.findall(r'<(?:input|meta)[^>]+>', html, flags=re.IGNORECASE):
        attrs = _extrage_atribute_html(tag)
        identificatori = {
            attrs.get("id", "").lower(),
            attrs.get("name", "").lower(),
            attrs.get("property", "").lower(),
        }
        if {"csrf", "csrf_token", "csrf-token", "_csrf", "_csrf_token"} & identificatori:
            token = attrs.get("value") or attrs.get("content")
            if token:
                return token

    sabloane = (
        r'csrf-token["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'csrf_token["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'csrf["\']?\s*[:=]\s*["\']([a-fA-F0-9]{32,})["\']',
        r'document\.getElementById\(["\']csrf_token["\']\)\.value[^;]*["\']([a-fA-F0-9]{32,})["\']',
    )
    for sablon in sabloane:
        match = re.search(sablon, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _rezumat_html_login(html: str) -> dict[str, Any]:
    titlu = None
    match_titlu = re.search(r'<title[^>]*>(.*?)</title>', html or "", flags=re.IGNORECASE | re.DOTALL)
    if match_titlu:
        titlu = re.sub(r"\s+", " ", match_titlu.group(1)).strip()

    campuri: list[str] = []
    for tag in re.findall(r'<input[^>]+>', html or "", flags=re.IGNORECASE):
        attrs = _extrage_atribute_html(tag)
        nume = attrs.get("name") or attrs.get("id")
        if nume and nume not in campuri:
            campuri.append(nume)

    return {
        "titlu": titlu,
        "campuri": campuri[:12],
        "are_form_login": bool(re.search(r'<form[^>]+(?:login|user-login|password)', html or "", re.IGNORECASE)),
        "are_csrf_text": "csrf" in (html or "").lower(),
        "are_challenge": any(text in (html or "").lower() for text in ("cloudflare", "challenge", "just a moment", "checking your browser")),
    }

def _stare_factura(item: dict[str, Any], rest: float | None = None) -> str:
    stare = _text(_get(item, "status", "state", "stare", "PaymentStatus", "InvoiceStatus", "Status")) or ""
    normal = stare.lower()
    if rest is not None and rest > 0:
        return "neplatita"
    if any(cuv in normal for cuv in ("achit", "plat", "paid")):
        return "platita"
    if any(cuv in normal for cuv in ("neachit", "unpaid", "rest")):
        return "neplatita"
    return "neplatita" if rest and rest > 0 else "platita"


def _cod_client(date: dict[str, Any]) -> str | None:
    continut = _continut_din_raspuns(date)
    if isinstance(continut, list) and continut:
        return str(continut[0])
    if isinstance(date, dict):
        return _text(_get(date, "selectedClientCode", "clientCode", "ClientCode", "cod_client"))
    return None


class ClientApiVeolia:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._autentificat = False
        self._csrf_token: str | None = None

    def _headers(self, *, referer: str | None = None, accept_html: bool = False) -> dict[str, str]:
        if accept_html:
            return {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "DNT": "1",
                "Pragma": "no-cache",
                "Referer": referer or f"{URL_BAZA}/login",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": USER_AGENT,
                "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }

        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "DNT": "1",
            "Origin": URL_BAZA,
            "Pragma": "no-cache",
            "Referer": referer or URL_SUMAR,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": USER_AGENT,
            "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        if self._csrf_token:
            headers["csrf-token"] = self._csrf_token
        return headers

    async def _actualizeaza_csrf(self, pagina: str, *, referer: str | None = None) -> str | None:
        url = f"{URL_BAZA}{pagina}"
        try:
            async with self._sesiune.get(
                url,
                headers=self._headers(referer=referer or url, accept_html=True),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text(errors="replace")
                token = _extrage_csrf_token(text)
                if token:
                    self._csrf_token = token
                _debug(
                    "csrf",
                    pagina=pagina,
                    status=raspuns.status,
                    lungime=len(text or ""),
                    token_gasit=bool(token),
                    token_lungime=len(token or ""),
                    **_rezumat_html_login(text),
                    fragment=text[:120] if text else "",
                )
                return token
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareVeolia(f"Eroare la citirea tokenului Veolia: {err}") from err

    async def _json_post(self, endpoint: str, payload: dict[str, Any] | None = None, *, referer: str | None = None) -> Any:
        url = endpoint if endpoint.startswith("http") else f"{URL_BAZA}{endpoint}"
        try:
            kwargs: dict[str, Any] = {
                "headers": self._headers(referer=referer),
                "timeout": aiohttp.ClientTimeout(total=30),
            }
            if payload is None:
                kwargs["data"] = b""
            else:
                kwargs["json"] = payload
            async with self._sesiune.post(url, **kwargs) as raspuns:
                text = await raspuns.text(errors="replace")
                tip_continut = raspuns.headers.get("Content-Type", "")
                _debug(
                    "POST",
                    url=url.replace(URL_BAZA, ""),
                    status=raspuns.status,
                    lungime=len(text or ""),
                    content_type=tip_continut,
                    are_csrf=bool(self._csrf_token),
                    fragment=text[:160] if text else "",
                )
                if raspuns.status in {401, 403}:
                    raise EroareAutentificareVeolia("Sesiunea Veolia nu este autentificată")
                if raspuns.status >= 400:
                    raise EroareConectareVeolia(f"Veolia a returnat HTTP {raspuns.status}")
                if not text:
                    return {"_http_status": raspuns.status}
                try:
                    return json.loads(text)
                except json.JSONDecodeError as err:
                    raise EroareRaspunsVeolia(f"Răspuns Veolia invalid pentru {endpoint}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareVeolia(f"Eroare de conectare la Veolia: {err}") from err

    async def async_login(self) -> None:
        for pagina, referer in (
            ("/login", f"{URL_BAZA}/contracte"),
            ("/login", f"{URL_BAZA}/login"),
            ("/", f"{URL_BAZA}/login"),
        ):
            await self._actualizeaza_csrf(pagina, referer=referer)
            if self._csrf_token:
                break
        raspuns = await self._json_post(
            "/login-submit",
            {"email": self._utilizator, "password": self._parola},
            referer=f"{URL_BAZA}/login",
        )
        succes = bool(_get(raspuns, "success"))
        cod = _get(raspuns, "code", "status")
        status_http = _get(raspuns, "_http_status")
        eroare = _get(raspuns, "error", "message")
        login_acceptat = succes or cod in (200, "200") or status_http in (200, 202, "200", "202")
        _debug(
            "login",
            success=succes,
            code=cod,
            status_http=status_http,
            acceptat=login_acceptat,
            are_eroare=bool(eroare),
        )
        if not login_acceptat:
            raise EroareAutentificareVeolia("Credentialele Veolia nu au fost acceptate")
        await self._actualizeaza_csrf("/sumar", referer=f"{URL_BAZA}/login")
        self._autentificat = True

    async def _asigura_login(self) -> None:
        if not self._autentificat:
            await self.async_login()

    async def async_get_all_data(self) -> dict[str, Any]:
        await self._asigura_login()
        coduri = await self._json_post(ENDPOINTS["coduri_client"], None, referer=URL_SUMAR)
        cod_client = _cod_client(coduri)
        if not cod_client:
            raise EroareRaspunsVeolia("Veolia nu a returnat codul de client")

        locatii = await self._json_post(ENDPOINTS["locatii"], {"clientCode": cod_client}, referer=URL_SUMAR)
        contracte = await self._json_post(ENDPOINTS["contracte"], {"clientCode": cod_client}, referer=f"{URL_BAZA}/contracte")
        sumar = await self._json_post(ENDPOINTS["sumar_facturi"], {"clientCode": cod_client}, referer=URL_SUMAR)
        facturi = await self._json_post(ENDPOINTS["facturi"], {"clientCode": cod_client, "year": str(date.today().year)}, referer=f"{URL_BAZA}/facturi")
        facturi_neachitate = await self._json_post(ENDPOINTS["facturi_neachitate"], {"clientCode": cod_client}, referer=f"{URL_BAZA}/facturi")
        plati = await self._json_post(ENDPOINTS["plati"], {"clientCode": cod_client}, referer=f"{URL_BAZA}/platile-mele")
        total_plati = await self._json_post(ENDPOINTS["total_plati"], {"clientCode": cod_client}, referer=f"{URL_BAZA}/facturi")

        locatii_lista = _lista_din_raspuns(locatii, "ConsumptionLocations", "ConsumptionPoints", "Locations", "items")
        contracte_lista = _lista_din_raspuns(contracte, "ContractInfo", "contracts")
        consumuri: list[Any] = []
        ultim_index: dict[str, Any] = {}
        for locatie in locatii_lista or contracte_lista:
            contor = _text(_primul(
                _get(locatie, "MeterSernr", "MeterSeries", "meterSernr", "serie_contor"),
                (_get(locatie, "MeterSeries") or [None])[0] if isinstance(_get(locatie, "MeterSeries"), list) else None,
            ))
            cod_loc = _text(_primul(_get(locatie, "ConsumptionPointCode", "consumptionPointCode", "cod_locatie"), _get(locatie, "Installation")))
            if not contor:
                continue
            try:
                ultim = await self._json_post(ENDPOINTS["ultim_index"], {"clientCode": cod_client, "meterSernr": contor}, referer=URL_SUMAR)
                ultim_index[contor] = ultim
            except EroareApiVeolia as err:
                _debug("ultim_index_eroare", contor=contor, mesaj=str(err))
            if cod_loc:
                try:
                    istoric = await self._json_post(
                        ENDPOINTS["istoric_index"],
                        {"clientCode": cod_client, "meterSernr": contor, "consumptionPointCode": cod_loc},
                        referer=f"{URL_BAZA}/index",
                    )
                    consumuri.extend(_lista_din_raspuns(istoric, "IndexHistory", "History", "items"))
                except EroareApiVeolia as err:
                    _debug("istoric_index_eroare", contor=contor, cod_locatie=cod_loc, mesaj=str(err))

        rezultat = {
            "cod_client": cod_client,
            "coduri": coduri,
            "locatii": locatii,
            "contracte": contracte,
            "sumar": sumar,
            "facturi": facturi,
            "facturi_neachitate": facturi_neachitate,
            "plati": plati,
            "total_plati": total_plati,
            "ultim_index": ultim_index,
            "istoric_index": consumuri,
        }
        _debug(
            "rezumat_date",
            cod_client=cod_client,
            locatii=len(locatii_lista),
            contracte=len(contracte_lista),
            consumuri=len(consumuri),
            facturi=len(_lista_din_raspuns(facturi, "Invoices", "PaidInvoices", "items")),
            neachitate=len(_lista_din_raspuns(facturi_neachitate, "UnpaidInvoices", "Invoices", "items")),
            plati=len(_lista_din_raspuns(plati, "Payments", "items")),
        )
        return rezultat

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        coduri = await self._json_post(ENDPOINTS["coduri_client"], None, referer=URL_SUMAR)
        return {"cod_client": _cod_client(coduri), "coduri": coduri}


class ClientFurnizorVeolia(ClientFurnizor):
    cheie_furnizor = "veolia"
    nume_prietenos = "Veolia"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiVeolia(self.sesiune, self.utilizator, self.parola)
        try:
            rezultat = await api.async_validate_credentials()
        except EroareAutentificareVeolia as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareVeolia as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsVeolia as err:
            raise EroareParsare(str(err)) from err
        return str(rezultat.get("cod_client") or self.utilizator.strip().lower())

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiVeolia(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificareVeolia as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareVeolia as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsVeolia as err:
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
            extra={"portal_url": URL_BAZA, "cod_client": date_brute.get("cod_client"), "numar_locatii": len(conturi)},
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        cod_client = str(date_brute.get("cod_client") or self.utilizator.strip().lower())
        locatii = _lista_din_raspuns(date_brute.get("locatii"), "ConsumptionLocations", "ConsumptionPoints", "Locations", "items")
        contracte = _lista_din_raspuns(date_brute.get("contracte"), "ContractInfo", "contracts")
        surse = locatii or contracte
        conturi: list[ContUtilitate] = []
        vazute: set[str] = set()
        for item in surse:
            if not isinstance(item, dict):
                continue
            serie = _text(_primul(
                _get(item, "MeterSernr", "meterSernr", "MeterNumber", "serie_contor"),
                (_get(item, "MeterSeries") or [None])[0] if isinstance(_get(item, "MeterSeries"), list) else None,
            ))
            cod_locatie = _text(_primul(_get(item, "ConsumptionPointCode", "consumptionPointCode"), _get(item, "Installation"), serie, cod_client))
            if not cod_locatie or cod_locatie in vazute:
                continue
            vazute.add(cod_locatie)
            adresa = _text(_primul(_get(item, "AddressConsumptionPoint"), _get(item, "ConsumptionPointAddress"), _get(item, "Address"), _get(item, "address")))
            contract = _text(_primul(_get(item, "ContractNumberWithAnb"), _get(item, "ContractNumber"), _get(item, "contractNumber")))
            nume = adresa or f"Cod client {cod_client}"
            conturi.append(
                ContUtilitate(
                    id_cont=cod_locatie,
                    nume=nume,
                    tip_cont="apa",
                    id_contract=contract or cod_client,
                    adresa=adresa,
                    stare=_text(_get(item, "Status", "stare")),
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute={**item, "cod_client": cod_client, "serie_contor": serie, "numar_contract": contract},
                )
            )
        if not conturi:
            conturi.append(
                ContUtilitate(
                    id_cont=cod_client,
                    nume=f"Cod client {cod_client}",
                    tip_cont="apa",
                    id_contract=cod_client,
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute={"cod_client": cod_client},
                )
            )
        return conturi

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        toate = []
        toate.extend(_lista_din_raspuns(date_brute.get("facturi_neachitate"), "UnpaidInvoices", "Invoices", "items"))
        toate.extend(_lista_din_raspuns(date_brute.get("facturi"), "Invoices", "PaidInvoices", "items"))
        sumar = _continut_din_raspuns(date_brute.get("sumar"))
        if isinstance(sumar, dict):
            ultima = _get(sumar, "LastInvoice", "lastInvoice", "invoice")
            if isinstance(ultima, dict):
                toate.append(ultima)
        cont_default = conturi[0] if conturi else None
        facturi: list[FacturaUtilitate] = []
        vazute: set[str] = set()
        for item in toate:
            if not isinstance(item, dict):
                continue
            nr = _text(_primul(_get(item, "InvoiceNumber", "invoiceNumber", "IDFactura", "InvoiceNo", "NumarFactura", "id"), _get(item, "DocumentNumber")))
            if not nr:
                continue
            if nr in vazute:
                continue
            vazute.add(nr)
            valoare = _numar(_primul(_get(item, "Amount", "amount", "InvoiceAmount", "TotalAmount", "SumaFactura", "Suma factură"), _get(item, "total")))
            rest = _numar(_primul(_get(item, "AmountToPay", "amountToPay", "RemainingAmount", "rest", "TotalToPay"), valoare))
            id_cont = self._gaseste_id_cont(item, conturi) or (cont_default.id_cont if cont_default else None)
            stare = _stare_factura(item, rest)
            raw = dict(item)
            raw["rest_plata"] = rest if stare == "neplatita" else 0.0
            raw["numar_factura"] = nr
            facturi.append(
                FacturaUtilitate(
                    id_factura=nr,
                    titlu=f"Factura {nr}",
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=_data(_primul(_get(item, "IssueDate", "issueDate", "DataEmitere", "data_emitere", "Data emitere"), _get(item, "date"))),
                    data_scadenta=_data(_primul(_get(item, "DueDate", "dueDate", "DataScadenta", "data_scadenta", "Data scadență"), _get(item, "due"))),
                    stare=stare,
                    categorie="apa_canal",
                    id_cont=id_cont,
                    id_contract=str(_primul(_get(item, "ContractNumber", "contractNumber"), id_cont) or ""),
                    tip_utilitate="apa",
                    tip_serviciu="apa_canal",
                    date_brute=raw,
                )
            )
        facturi.sort(key=lambda f: f.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont(self, item: dict[str, Any], conturi: list[ContUtilitate]) -> str | None:
        candidati_item = {
            _text(_get(item, "ConsumptionPointCode", "consumptionPointCode", "Installation")),
            _text(_get(item, "MeterSernr", "meterSernr", "MeterNumber")),
        }
        candidati_item = {x for x in candidati_item if x}
        for cont in conturi:
            raw = cont.date_brute or {}
            candidati_cont = {cont.id_cont, _text(raw.get("serie_contor")), _text(raw.get("Installation"))}
            if candidati_item & {x for x in candidati_cont if x}:
                return cont.id_cont
        return None

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate], facturi: list[FacturaUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        plati = [p for p in _lista_din_raspuns(date_brute.get("plati"), "Payments", "items") if isinstance(p, dict)]
        istoric = [c for c in date_brute.get("istoric_index", []) if isinstance(c, dict)]
        sumar = _continut_din_raspuns(date_brute.get("sumar"))
        total_plati = _continut_din_raspuns(date_brute.get("total_plati"))
        for cont in conturi:
            raw_cont = cont.date_brute or {}
            serie = _text(raw_cont.get("serie_contor"))
            facturi_cont = [f for f in facturi if f.id_cont == cont.id_cont] or facturi
            neachitate = [f for f in facturi_cont if f.stare in {"neplatita", "scadenta"}]
            sold = round(sum(float(f.date_brute.get("rest_plata") or 0.0) for f in neachitate), 2)
            ultima_factura = facturi_cont[0] if facturi_cont else None
            ultima_plata = self._ultima_plata(plati)
            citiri_cont = [c for c in istoric if not serie or str(_get(c, "MeterSernr", "meterSernr", "MeterNumber", "serie_contor") or serie) == serie]
            ultima_citire = self._ultima_citire(citiri_cont, date_brute.get("ultim_index", {}).get(serie) if serie else None)
            total_de_plata_portal = _numar(_primul(
                _get(sumar if isinstance(sumar, dict) else {}, "TotalToPay", "totalToPay", "TotalPayment", "totalPayment", "total_de_plata"),
                _get(total_plati if isinstance(total_plati, dict) else {}, "TotalToPay", "totalToPay", "total"),
            ))
            if total_de_plata_portal is not None and total_de_plata_portal > 0:
                sold = total_de_plata_portal
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
                "numar_plati": len(plati),
                "data_ultima_plata": _data(_primul(_get(ultima_plata or {}, "PaymentDate", "paymentDate", "DataPlata", "data"), _get(ultima_plata or {}, "date"))).isoformat() if ultima_plata and _data(_primul(_get(ultima_plata, "PaymentDate", "paymentDate", "DataPlata", "data"), _get(ultima_plata, "date"))) else None,
                "valoare_ultima_plata": _numar(_primul(_get(ultima_plata or {}, "Amount", "amount", "SumaAchitata", "suma"), _get(ultima_plata or {}, "PaidAmount"))) if ultima_plata else None,
                "numar_contoare": len({serie for serie in [serie] if serie}) or None,
                "index_contor": _numar(_primul(_get(ultima_citire or {}, "Index", "index", "NewIndex", "indexNou", "IndexNou"), _get(ultima_citire or {}, "LastIndex", "lastIndex"))),
                "ultim_consum": _numar(_primul(_get(ultima_citire or {}, "Quantity", "quantity", "Cantitate", "cantitate", "Consumption"), _get(ultima_citire or {}, "consumption"))),
                "cod_client": date_brute.get("cod_client"),
                "serie_contor": serie,
            }
            for cheie, valoare in valori.items():
                consumuri.append(ConsumUtilitate(cheie=cheie, valoare=valoare, unitate=_unitate(cheie), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"))
        return consumuri

    def _ultima_plata(self, plati: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not plati:
            return None
        return sorted(plati, key=lambda p: _data(_primul(_get(p, "PaymentDate", "paymentDate", "DataPlata", "data"), _get(p, "date"))) or date.min, reverse=True)[0]

    def _ultima_citire(self, citiri: list[dict[str, Any]], fallback: Any) -> dict[str, Any] | None:
        candidati = list(citiri)
        if isinstance(fallback, dict):
            continut = _continut_din_raspuns(fallback)
            if isinstance(continut, dict):
                candidati.append(continut)
            elif isinstance(continut, list):
                candidati.extend(x for x in continut if isinstance(x, dict))
        if not candidati:
            return None
        return sorted(candidati, key=lambda c: _data(_primul(_get(c, "Date", "date", "Data", "PeriodEnd", "periodEnd"), _get(c, "ReadDate"))) or date.min, reverse=True)[0]


def _unitate(cheie: str) -> str | None:
    if cheie in {"de_plata", "sold_curent", "valoare_ultima_factura", "valoare_ultima_plata"}:
        return "RON"
    if cheie in {"index_contor", "ultim_consum"}:
        return "m³"
    return None
