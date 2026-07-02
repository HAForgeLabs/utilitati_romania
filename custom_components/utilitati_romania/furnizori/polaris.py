from __future__ import annotations

from datetime import date, datetime
import json
import logging
import re
from typing import Any

import aiohttp

from ..const import FURNIZOR_POLARIS
from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://my.polaris.ro"
URL_LOGIN = f"{URL_BAZA}/Login.aspx"
URL_AUTENTIFICARE = f"{URL_BAZA}/Login.aspx/Autentificare"
URL_HOME = f"{URL_BAZA}/"
URL_FACTURI = f"{URL_BAZA}/InvoicesAndPayments.aspx"
URL_INIT = f"{URL_BAZA}/Default.aspx/Init"
URL_PUNCTE = f"{URL_BAZA}/InvoicesAndPayments.aspx/LoadPuncteDeLucru"
URL_SELECT_PUNCT = f"{URL_BAZA}/InvoicesAndPayments.aspx/SelectPunctLucru"
URL_SOLD = f"{URL_BAZA}/InvoicesAndPayments.aspx/LoadDataSold"
URL_FACTURI_DATE = f"{URL_BAZA}/InvoicesAndPayments.aspx/LoadDataFacturi"
URL_PLATI = f"{URL_BAZA}/InvoicesAndPayments.aspx/LoadDataPlati"
URL_FISA = f"{URL_BAZA}/InvoicesAndPayments.aspx/LoadDataFisa"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class EroareApiPolaris(Exception):
    pass


class EroareAutentificarePolaris(EroareApiPolaris):
    pass


class EroareConectarePolaris(EroareApiPolaris):
    pass


class EroareRaspunsPolaris(EroareApiPolaris):
    pass


class ClientApiPolaris:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._autentificat = False

    def _headers(self, *, referer: str | None = None, ajax: bool = False, origin: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01" if ajax else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": referer or URL_LOGIN,
            "User-Agent": USER_AGENT,
        }
        if ajax:
            headers.update({
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            })
        if origin:
            headers["Origin"] = URL_BAZA
        return headers

    async def async_login(self) -> None:
        try:
            async with self._sesiune.get(URL_LOGIN, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                text = await raspuns.text(errors="ignore")
                if raspuns.status >= 400:
                    raise EroareConectarePolaris(f"Polaris a returnat HTTP {raspuns.status} la pagina de login")

            login_ok = False
            mesaj_login = ""
            try:
                async with self._sesiune.post(
                    URL_AUTENTIFICARE,
                    headers=self._headers(referer=URL_LOGIN, ajax=True, origin=True),
                    json={"Email": self._utilizator, "Parola": self._parola, "Cod": "", "token": ""},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as raspuns:
                    login_text = await raspuns.text(errors="ignore")
                    mesaj_login = login_text[:300]
                    if raspuns.status >= 400:
                        raise EroareAutentificarePolaris(f"Autentificare Polaris eșuată: HTTP {raspuns.status}")
                    payload = _incarca_json(login_text)
                    login_ok = _raspuns_este_ok(payload)
                    mesaj_login = _mesaj_raspuns(payload) or mesaj_login
            except EroareAutentificarePolaris:
                raise
            except Exception as err:
                mesaj_login = str(err)

            async with self._sesiune.get(URL_HOME, headers=self._headers(referer=URL_LOGIN), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                await raspuns.text(errors="ignore")

            async with self._sesiune.post(URL_INIT, headers=self._headers(referer=URL_HOME, ajax=True, origin=True), json={}, timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                await raspuns.text(errors="ignore")

            async with self._sesiune.get(URL_FACTURI, headers=self._headers(referer=URL_HOME), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                pagina = await raspuns.text(errors="ignore")
                if raspuns.status >= 400:
                    raise EroareConectarePolaris(f"Polaris a returnat HTTP {raspuns.status} la pagina de facturi")

            if _pare_pagina_login(pagina):
                _LOGGER.debug(
                    "[POLARIS DIAG] autentificare_neconfirmata: login_ok=%s mesaj=%s pagina_facturi_len=%s",
                    login_ok,
                    _mascheaza(mesaj_login),
                    len(pagina or ""),
                )
                raise EroareAutentificarePolaris("Sesiunea Polaris nu a fost creată corect. Portalul folosește reCAPTCHA la autentificare.")

            _LOGGER.debug(
                "[POLARIS DIAG] login: login_ok=%s mesaj=%s pagina_facturi_len=%s",
                login_ok,
                _mascheaza(mesaj_login),
                len(pagina or ""),
            )
            self._autentificat = True
        except EroareApiPolaris:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectarePolaris(f"Eroare de conectare la Polaris: {err}") from err

    async def _post_json(self, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()
        try:
            async with self._sesiune.post(
                url,
                headers=self._headers(referer=URL_FACTURI, ajax=True, origin=True),
                json=payload or {},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text(errors="ignore")
                if raspuns.status in {401, 403} or _pare_pagina_login(text):
                    self._autentificat = False
                    await self.async_login()
                    return await self._post_json(url, payload)
                if raspuns.status >= 400:
                    raise EroareRaspunsPolaris(f"Polaris a returnat HTTP {raspuns.status} pentru {url}: {text[:300]}")
                data = _incarca_json(text)
                if not isinstance(data, dict):
                    raise EroareRaspunsPolaris(f"Răspuns Polaris invalid pentru {url}: {text[:300]}")
                return data
        except EroareApiPolaris:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectarePolaris(f"Eroare de conectare la Polaris pentru {url}: {err}") from err

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        puncte_raw = await self._post_json(URL_PUNCTE, {})
        puncte = _lista_din_raspuns(puncte_raw, "lista")
        date_puncte: list[dict[str, Any]] = []

        for punct in puncte:
            id_punct = _primul_text(punct, "ID", "Id", "id", "IdPunctDeLucru")
            if not id_punct:
                continue
            payload = {"IdPunctDeLucru": str(id_punct)}
            try:
                await self._post_json(URL_SELECT_PUNCT, payload)
            except Exception:
                _LOGGER.debug("Selectarea punctului Polaris %s a eșuat", id_punct, exc_info=True)
            sold_raw = await self._post_json(URL_SOLD, payload)
            facturi_raw = await self._post_json(URL_FACTURI_DATE, payload)
            plati_raw = await self._post_json(URL_PLATI, payload)
            fisa_raw = await self._post_json(URL_FISA, payload)
            date_puncte.append({
                "punct": punct,
                "sold": sold_raw.get("d") if isinstance(sold_raw.get("d"), dict) else sold_raw,
                "facturi": _lista_din_raspuns(facturi_raw, "Facturi"),
                "plati": _lista_din_raspuns(plati_raw, "Plati"),
                "fisa": _lista_din_raspuns(fisa_raw, "Fisa"),
                "fisa_conturi": _lista_din_raspuns(fisa_raw, "Conturi"),
            })

        _LOGGER.debug(
            "[POLARIS DIAG] date: puncte=%s facturi=%s plati=%s fisa=%s",
            len(date_puncte),
            sum(len(item.get("facturi") or []) for item in date_puncte),
            sum(len(item.get("plati") or []) for item in date_puncte),
            sum(len(item.get("fisa") or []) for item in date_puncte),
        )
        return {"puncte": date_puncte, "puncte_raw": puncte_raw}

    async def async_validate_credentials(self) -> dict[str, Any]:
        data = await self.async_get_all_data()
        if not data.get("puncte"):
            raise EroareRaspunsPolaris("Contul Polaris nu are puncte de lucru disponibile")
        return data


class ClientFurnizorPolaris(ClientFurnizor):
    cheie_furnizor = FURNIZOR_POLARIS
    nume_prietenos = "Polaris"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiPolaris(self.sesiune, self.utilizator, self.parola)
        try:
            data = await api.async_validate_credentials()
        except EroareAutentificarePolaris as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectarePolaris as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsPolaris as err:
            raise EroareParsare(str(err)) from err
        primul = (data.get("puncte") or [{}])[0].get("punct") or {}
        return str(_primul_text(primul, "ID", "Id", "id") or self.utilizator).strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiPolaris(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificarePolaris as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectarePolaris as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsPolaris as err:
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
        for item in date_brute.get("puncte") or []:
            punct = item.get("punct") or {}
            id_cont = str(_primul_text(punct, "ID", "Id", "id", "IdPunctDeLucru") or "").strip()
            if not id_cont:
                continue
            denumire = _curata_text(_primul_text(punct, "Denumire", "denumire", "nume") or f"Polaris {id_cont}")
            adresa = _extrage_adresa_din_denumire(denumire)
            conturi.append(
                ContUtilitate(
                    id_cont=id_cont,
                    nume=_nume_cont_polaris(denumire, id_cont),
                    tip_cont="salubritate",
                    id_contract=str(_gaseste_id_contract(item.get("facturi") or []) or id_cont),
                    adresa=adresa,
                    stare="activ",
                    tip_utilitate="salubritate",
                    tip_serviciu="salubritate",
                    date_brute={"punct": punct, "sold": item.get("sold") or {}},
                )
            )
        return conturi

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        conturi_map = {cont.id_cont: cont for cont in conturi}
        facturi: list[FacturaUtilitate] = []
        vazute: set[str] = set()
        for item in date_brute.get("puncte") or []:
            punct = item.get("punct") or {}
            id_cont = str(_primul_text(punct, "ID", "Id", "id", "IdPunctDeLucru") or "").strip()
            cont = conturi_map.get(id_cont)
            for factura in item.get("facturi") or []:
                id_intern = str(_primul_text(factura, "IdFactura", "id_factura") or "").strip()
                nr_document = str(_primul_text(factura, "NrDocument", "NumarDocument") or id_intern).strip()
                serie = str(_primul_text(factura, "SerieDocument", "Serie") or "").strip()
                id_afisabil = _numar_factura_polaris(serie, nr_document, id_intern)
                cheie = f"{id_cont}:{id_intern or id_afisabil or nr_document}"
                if not id_afisabil or cheie in vazute:
                    continue
                vazute.add(cheie)
                rest = _valoare_numerica(_primul_text(factura, "Rest", "Rest_Proxy", "DePlata"))
                de_plata = _valoare_numerica(_primul_text(factura, "DePlata"))
                rest_final = max(rest or 0.0, de_plata or 0.0)
                stare = "neplatita" if rest_final > 0 else "platita"
                titlu = f"Factura {id_afisabil}".strip()
                facturi.append(
                    FacturaUtilitate(
                        id_factura=id_afisabil,
                        titlu=titlu,
                        valoare=_valoare_numerica(_primul_text(factura, "Valoare", "Valoare_Proxy")),
                        moneda=str(_primul_text(factura, "MonedaCod") or "RON"),
                        data_emitere=_data_din_proxy(_primul_text(factura, "DataDocument_Proxy")) or _data_din_ms(_primul_text(factura, "DataDocument")),
                        data_scadenta=_data_din_proxy(_primul_text(factura, "DataScadenta_Proxy")) or _data_din_ms(_primul_text(factura, "DataScadenta")),
                        stare=stare,
                        categorie="salubritate",
                        id_cont=id_cont or None,
                        id_contract=str(_primul_text(factura, "IdContract") or (cont.id_contract if cont else id_cont) or ""),
                        tip_utilitate="salubritate",
                        tip_serviciu="salubritate",
                        date_brute={**factura, "rest_plata": rest_final, "id_factura_intern": id_intern, "numar_factura": id_afisabil},
                    )
                )
        facturi.sort(key=lambda factura: factura.data_emitere or date.min, reverse=True)
        return facturi

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate], facturi: list[FacturaUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        for cont in conturi:
            punct_data = next((item for item in date_brute.get("puncte") or [] if str(_primul_text(item.get("punct") or {}, "ID", "Id", "id")) == cont.id_cont), {})
            facturi_cont = [factura for factura in facturi if factura.id_cont == cont.id_cont]
            plati = [item for item in punct_data.get("plati") or [] if isinstance(item, dict)]
            fisa = [item for item in punct_data.get("fisa") or [] if isinstance(item, dict)]
            sold_raw = punct_data.get("sold") or {}
            sold = _valoare_numerica(sold_raw.get("Sold"))
            if sold is None:
                sold = round(sum(float(factura.date_brute.get("rest_plata") or 0.0) for factura in facturi_cont), 2)
            neachitate = [factura for factura in facturi_cont if factura.stare in {"neplatita", "scadenta"}]
            ultima_factura = facturi_cont[0] if facturi_cont else None
            ultima_plata = _ultima_plata(plati)
            urmatoarea = _prima_scadenta_neachitata(neachitate) or ultima_factura
            consumuri.extend([
                ConsumUtilitate("de_plata", sold, "RON", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=sold_raw),
                ConsumUtilitate("sold_curent", sold, "RON", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=sold_raw),
                ConsumUtilitate("factura_restanta", "da" if sold > 0 else "nu", None, id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=sold_raw),
                ConsumUtilitate("numar_facturi", len(facturi_cont), "buc", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute={"facturi": punct_data.get("facturi") or []}),
                ConsumUtilitate("numar_facturi_neachitate", len(neachitate), "buc", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute={"facturi": punct_data.get("facturi") or []}),
                ConsumUtilitate("numar_plati", len(plati), "buc", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute={"plati": plati}),
                ConsumUtilitate("numar_inregistrari_fisa", len(fisa), "buc", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute={"fisa": fisa}),
            ])
            if ultima_factura:
                consumuri.extend([
                    ConsumUtilitate("valoare_ultima_factura", ultima_factura.valoare, ultima_factura.moneda or "RON", perioada=_data_iso(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("id_ultima_factura", ultima_factura.id_factura, None, perioada=_data_iso(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("urmatoarea_scadenta", _data_iso(urmatoarea.data_scadenta) if urmatoarea else None, None, perioada=_data_iso(urmatoarea.data_scadenta) if urmatoarea else None, id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=urmatoarea.date_brute if urmatoarea else {}),
                ])
            if ultima_plata:
                consumuri.extend([
                    ConsumUtilitate("ultima_plata", _valoare_numerica(_primul_text(ultima_plata, "Suma", "Suma_Proxy")), str(_primul_text(ultima_plata, "MonedaCod") or "RON"), perioada=_data_iso(_data_din_proxy(_primul_text(ultima_plata, "DataDoc_Proxy")) or _data_din_ms(_primul_text(ultima_plata, "DataDoc"))), id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_plata),
                    ConsumUtilitate("last_payment", _valoare_numerica(_primul_text(ultima_plata, "Suma", "Suma_Proxy")), str(_primul_text(ultima_plata, "MonedaCod") or "RON"), perioada=_data_iso(_data_din_proxy(_primul_text(ultima_plata, "DataDoc_Proxy")) or _data_din_ms(_primul_text(ultima_plata, "DataDoc"))), id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_plata),
                ])
        return consumuri


def _incarca_json(text: str) -> Any:
    try:
        return json.loads(text or "{}")
    except Exception as err:
        raise EroareRaspunsPolaris(f"Răspuns JSON invalid de la Polaris: {(text or '')[:300]}") from err


def _raspuns_este_ok(data: Any) -> bool:
    if isinstance(data, dict):
        d = data.get("d", data)
        if isinstance(d, bool):
            return d
        if isinstance(d, dict):
            for key in ("EsteOK", "Success", "success", "ok"):
                if key in d:
                    return bool(d.get(key))
    return False


def _mesaj_raspuns(data: Any) -> str:
    if isinstance(data, dict):
        d = data.get("d", data)
        if isinstance(d, dict):
            return str(d.get("Mesaj") or d.get("Message") or d.get("message") or "")
        return str(d or "")
    return ""


def _lista_din_raspuns(data: dict[str, Any], key: str) -> list[Any]:
    d = data.get("d") if isinstance(data, dict) else None
    if isinstance(d, dict):
        valoare = d.get(key)
        if isinstance(valoare, list):
            return valoare
    valoare = data.get(key) if isinstance(data, dict) else None
    if isinstance(valoare, list):
        return valoare
    return []


def _pare_pagina_login(text: str) -> bool:
    text_l = (text or "").lower()
    return "login.aspx" in text_l and "autentificare" in text_l and "logout.aspx" not in text_l and "deconectare" not in text_l


def _primul_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return str(data[key])
    return None


def _curata_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _valoare_numerica(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip().replace("RON", "").replace("lei", "").strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _data_din_proxy(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _data_din_ms(value: Any) -> date | None:
    text = str(value or "")
    match = re.search(r"Date\((-?\d+)\)", text)
    if not match:
        return None
    try:
        ms = int(match.group(1))
        if ms < 0:
            return None
        return datetime.fromtimestamp(ms / 1000).date()
    except Exception:
        return None


def _data_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _extrage_adresa_din_denumire(denumire: str) -> str | None:
    text = _curata_text(denumire)
    if " - " in text:
        return text.split(" - ", 1)[1].strip() or None
    if "Pdl." in text:
        return text.split("Pdl.", 1)[1].strip() or None
    return None


def _nume_cont_polaris(denumire: str, id_cont: str) -> str:
    adresa = _extrage_adresa_din_denumire(denumire)
    if adresa:
        adresa = adresa.replace("Pdl.", "").strip()
        return f"Polaris - {adresa}"
    return f"Polaris - {id_cont}"



def _numar_factura_polaris(serie: str | None, nr_document: str | None, id_intern: str | None = None) -> str | None:
    serie_curata = _curata_text(serie)
    document_curat = _curata_text(nr_document)
    if serie_curata and document_curat:
        return f"{serie_curata} {document_curat}".strip()
    if document_curat:
        return document_curat
    id_curat = _curata_text(id_intern)
    return id_curat or None

def _gaseste_id_contract(facturi: list[dict[str, Any]]) -> str | None:
    for factura in facturi:
        value = _primul_text(factura, "IdContract", "id_contract")
        if value:
            return value
    return None


def _ultima_plata(plati: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not plati:
        return None
    def key(item: dict[str, Any]) -> date:
        return _data_din_proxy(_primul_text(item, "DataDoc_Proxy")) or _data_din_ms(_primul_text(item, "DataDoc")) or date.min
    return sorted(plati, key=key, reverse=True)[0]


def _prima_scadenta_neachitata(facturi: list[FacturaUtilitate]) -> FacturaUtilitate | None:
    if not facturi:
        return None
    return sorted(facturi, key=lambda factura: factura.data_scadenta or date.max)[0]


def _mascheaza(text: Any) -> str:
    value = str(text or "")
    if not value:
        return ""
    value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "***@***", value)
    value = re.sub(r"\b\d{5,}\b", "***", value)
    return value[:300]
