from __future__ import annotations

from datetime import date, datetime, timezone
import json
import logging
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://acilfov.emsys.ro/self_utilities"
URL_LOGIN = f"{URL_BAZA}/login"
URL_CONT = f"{URL_BAZA}/"
URL_REST = f"{URL_BAZA}/rest/self"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class EroareApiApaIlfov(Exception):
    pass


class EroareAutentificareApaIlfov(EroareApiApaIlfov):
    pass


class EroareConectareApaIlfov(EroareApiApaIlfov):
    pass


class EroareRaspunsApaIlfov(EroareApiApaIlfov):
    pass


class ClientApiApaIlfov:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._autentificat = False

    def _headers(self, *, referer: str | None = None, json_accept: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*" if json_accept else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": USER_AGENT,
            "Referer": referer or URL_CONT,
            "Origin": "https://acilfov.emsys.ro",
        }
        return headers

    async def async_login(self) -> None:
        try:
            async with self._sesiune.get(URL_CONT, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                await raspuns.text()

            async with self._sesiune.post(
                URL_LOGIN,
                headers={**self._headers(referer=URL_CONT), "Content-Type": "application/x-www-form-urlencoded"},
                data={"user": self._utilizator, "parola": self._parola, "cf-turnstile-response": ""},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status not in (200, 302, 303):
                    raise EroareAutentificareApaIlfov(f"Autentificare Apa Ilfov eșuată: HTTP {raspuns.status}")
                if raspuns.status == 200 and _pare_login_sau_cloudflare(text):
                    raise EroareAutentificareApaIlfov("Autentificarea Apa Ilfov nu a trecut de pagina de login / Cloudflare")

            sesiune = await self.async_post_json("infoSession", data={}, necesita_login=False)
            if not isinstance(sesiune, dict) or not sesiune.get("userName"):
                raise EroareAutentificareApaIlfov("Sesiunea Apa Ilfov nu a fost creată corect")
            self._autentificat = True
        except EroareApiApaIlfov:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareApaIlfov(f"Eroare de conectare la Apa Ilfov: {err}") from err
        except TimeoutError as err:
            raise EroareConectareApaIlfov("Timeout la Apa Ilfov") from err

    async def async_get_json(self, endpoint: str, *, necesita_login: bool = True) -> Any:
        if necesita_login and not self._autentificat:
            await self.async_login()
        url = endpoint if endpoint.startswith("http") else f"{URL_REST}/{endpoint.lstrip('/')}"
        try:
            async with self._sesiune.get(url, headers=self._headers(json_accept=True), timeout=aiohttp.ClientTimeout(total=30)) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403) or _pare_login_sau_cloudflare(text):
                    if necesita_login:
                        self._autentificat = False
                        await self.async_login()
                        return await self.async_get_json(endpoint, necesita_login=False)
                    raise EroareAutentificareApaIlfov("Sesiunea Apa Ilfov a expirat")
                if raspuns.status >= 400:
                    raise EroareRaspunsApaIlfov(f"Apa Ilfov a returnat HTTP {raspuns.status} pentru {endpoint}")
                return json.loads(text) if text.strip() else None
        except EroareApiApaIlfov:
            raise
        except json.JSONDecodeError as err:
            raise EroareRaspunsApaIlfov(f"Răspuns JSON invalid Apa Ilfov pentru {endpoint}") from err
        except aiohttp.ClientError as err:
            raise EroareConectareApaIlfov(f"Eroare de conectare la Apa Ilfov pentru {endpoint}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareApaIlfov(f"Timeout la Apa Ilfov pentru {endpoint}") from err

    async def async_post_json(self, endpoint: str, *, data: dict[str, Any], necesita_login: bool = True) -> Any:
        if necesita_login and not self._autentificat:
            await self.async_login()
        url = endpoint if endpoint.startswith("http") else f"{URL_REST}/{endpoint.lstrip('/')}"
        try:
            async with self._sesiune.post(
                url,
                headers={**self._headers(json_accept=True), "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403) or _pare_login_sau_cloudflare(text):
                    if necesita_login:
                        self._autentificat = False
                        await self.async_login()
                        return await self.async_post_json(endpoint, data=data, necesita_login=False)
                    raise EroareAutentificareApaIlfov("Sesiunea Apa Ilfov a expirat")
                if raspuns.status >= 400:
                    raise EroareRaspunsApaIlfov(f"Apa Ilfov a returnat HTTP {raspuns.status} pentru {endpoint}")
                return json.loads(text) if text.strip() else None
        except EroareApiApaIlfov:
            raise
        except json.JSONDecodeError as err:
            raise EroareRaspunsApaIlfov(f"Răspuns JSON invalid Apa Ilfov pentru {endpoint}") from err
        except aiohttp.ClientError as err:
            raise EroareConectareApaIlfov(f"Eroare de conectare la Apa Ilfov pentru {endpoint}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareApaIlfov(f"Timeout la Apa Ilfov pentru {endpoint}") from err

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        sesiune = await self.async_post_json("infoSession", data={})
        return sesiune if isinstance(sesiune, dict) else {}

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        contracte = await self.async_get_json("admcl/getListaContracte")
        if not isinstance(contracte, list):
            contracte = []

        facturi_raw = await self.async_post_json(
            "facturi/Facturis",
            data={"$qd": "false", "$action": "LOAD_RECORDS", "$locale": "en", "$ls": "false", "$to": "500", "$order": "DATA_DOC desc"},
        )
        plati_raw = await self.async_post_json(
            "plati/Platis",
            data={"$qd": "false", "$action": "LOAD_RECORDS", "$locale": "en", "$ls": "false", "$to": "500", "$order": "DATA_PLATA desc"},
        )
        transmitere_raw = await self.async_post_json(
            "transmitere/Transmiteres",
            data={"$qd": "false", "$action": "LOAD_RECORDS", "$locale": "en", "$ls": "false", "$to": "500"},
        )
        consum_raw = await self.async_post_json(
            "consum/Consums",
            data={"$qd": "false", "$action": "LOAD_RECORDS", "$locale": "en", "$ls": "false", "$to": "500", "$order": "DATA_NOU desc"},
        )

        solduri: dict[str, Any] = {}
        puncte_consum: dict[str, Any] = {}
        adrese_puncte: dict[str, str] = {}
        contoare_puncte: dict[str, Any] = {}
        for contract in contracte:
            cod_client = str(contract.get("codClient") or "").strip()
            nr_contract = str(contract.get("nrContract") or "").strip()
            if not cod_client or not nr_contract:
                continue
            cheie = _cheie_contract(cod_client, nr_contract)
            try:
                solduri[cheie] = await self.async_get_json(f"facturi/getSoldClient?codClient={quote(cod_client)}&nrContract={quote(nr_contract)}")
            except EroareApiApaIlfov:
                _LOGGER.debug("Nu s-a putut citi soldul Apa Ilfov pentru %s", cheie, exc_info=True)
            try:
                puncte = await self.async_get_json(f"transmitere/puncteConsums?codClient={quote(cod_client)}&nrContract={quote(nr_contract)}")
                puncte_consum[cheie] = puncte
                if isinstance(puncte, list):
                    for punct in puncte:
                        id_locatie = punct.get("idLocatie") if isinstance(punct, dict) else None
                        if id_locatie is None:
                            continue
                        adrese_puncte[str(id_locatie)] = await self.async_get_json(f"consum/getAdresaPunctConsum?idLocatie={quote(str(id_locatie))}")
                        contoare_puncte[str(id_locatie)] = await self.async_get_json(f"consum/getContoare?idLocatie={quote(str(id_locatie))}")
            except EroareApiApaIlfov:
                _LOGGER.debug("Nu s-au putut citi punctele de consum Apa Ilfov pentru %s", cheie, exc_info=True)

        rezultat = {
            "contracte": contracte,
            "facturi_raw": facturi_raw,
            "plati_raw": plati_raw,
            "transmitere_raw": transmitere_raw,
            "consum_raw": consum_raw,
            "solduri": solduri,
            "puncte_consum": puncte_consum,
            "adrese_puncte": adrese_puncte,
            "contoare_puncte": contoare_puncte,
        }
        _log_debug_apa_ilfov("date brute", _rezumat_debug(rezultat))
        return rezultat


class ClientFurnizorApaIlfov(ClientFurnizor):
    cheie_furnizor = "apa_ilfov"
    nume_prietenos = "Apă Ilfov"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiApaIlfov(self.sesiune, self.utilizator, self.parola)
        try:
            rezultat = await api.async_validate_credentials()
        except EroareAutentificareApaIlfov as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaIlfov as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaIlfov as err:
            raise EroareParsare(str(err)) from err
        return str(rezultat.get("userName") or self.utilizator).strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiApaIlfov(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificareApaIlfov as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaIlfov as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaIlfov as err:
            raise EroareParsare(str(err)) from err

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute, conturi)
        consumuri = self._mapeaza_consumuri(date_brute, conturi, facturi)
        instantaneu = InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={"portal_url": URL_BAZA, "numar_contracte": len(conturi), "numar_facturi": len(facturi)},
        )
        _log_debug_apa_ilfov("date mapate Home Assistant", {
            "conturi": len(conturi),
            "facturi": len(facturi),
            "consumuri": len(consumuri),
            "conturi_sample": [_safe_dataclass(c) for c in conturi[:3]],
            "facturi_sample": [_safe_dataclass(f) for f in facturi[:5]],
            "consumuri_sample": [_safe_dataclass(c) for c in consumuri[:20]],
        })
        return instantaneu

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        conturi: list[ContUtilitate] = []
        for contract in date_brute.get("contracte") or []:
            if not isinstance(contract, dict):
                continue
            cod_client = str(contract.get("codClient") or "").strip()
            nr_contract = str(contract.get("nrContract") or "").strip()
            id_cont = _cheie_contract(cod_client, nr_contract) or cod_client or nr_contract
            if not id_cont:
                continue
            adresa = str(contract.get("adrClient") or "").strip()
            nume = str(contract.get("denClient") or "").strip()
            conturi.append(ContUtilitate(
                id_cont=id_cont,
                nume=_nume_cont_apa_ilfov(adresa, nr_contract, cod_client),
                tip_cont="apa",
                id_contract=nr_contract,
                adresa=adresa,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute={**contract, "cod_client": cod_client, "nr_contract": nr_contract, "nume_titular": nume},
            ))
        if not conturi:
            conturi.append(ContUtilitate(
                id_cont=self.utilizator.strip().lower(),
                nume="Apă Ilfov",
                tip_cont="apa",
                id_contract=self.utilizator.strip().lower(),
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
            ))
        return conturi

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        cont_default = conturi[0] if len(conturi) == 1 else None
        vazute: set[str] = set()
        for row in _records_rows(date_brute.get("facturi_raw")):
            nr_contract_raw = str(row.get("contract") or "").strip()
            nr_contract = nr_contract_raw.split("/")[0].strip() if nr_contract_raw else ""
            cod_client = str(row.get("client") or "").strip()
            id_cont = _gaseste_cont(conturi, cod_client, nr_contract) or (cont_default.id_cont if cont_default else None)
            factura_text = str(row.get("factura") or "").strip()
            serie = str(row.get("serieDoc") or "").strip()
            nr_doc = str(row.get("nrDoc") or "").strip()
            numar_afisat = factura_text or " ".join(x for x in (serie, nr_doc) if x).strip()
            id_factura = str(row.get("idFactura") or numar_afisat).strip()
            if not id_factura or id_factura in vazute:
                continue
            vazute.add(id_factura)
            rest = _valoare_numerica(row.get("restDePlata"))
            valoare = _valoare_numerica(row.get("valoareFactura"))
            stare = "neplatita" if (rest or 0) > 0 else "platita"
            raw = dict(row)
            raw.update({"rest_plata": rest, "numar_factura": numar_afisat, "serie_numar": numar_afisat, "pdf_url": row.get("denFisier")})
            facturi.append(FacturaUtilitate(
                id_factura=id_factura,
                titlu=numar_afisat or f"Factura {id_factura}",
                valoare=valoare,
                moneda="RON",
                data_emitere=_data_sigura(row.get("dataEmitere")),
                data_scadenta=_data_sigura(row.get("dataScadenta")),
                stare=stare,
                categorie="consum",
                id_cont=id_cont,
                id_contract=nr_contract or id_cont,
                tip_utilitate="apa",
                tip_serviciu="apa_canal",
                date_brute=raw,
            ))
        facturi.sort(key=lambda f: f.data_emitere or date.min, reverse=True)
        return facturi

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate], facturi: list[FacturaUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        plati_rows = _records_rows(date_brute.get("plati_raw"))
        transmitere_rows = _records_rows(date_brute.get("transmitere_raw"))
        consum_rows = _records_rows(date_brute.get("consum_raw"))
        total_de_plata = 0.0
        total_facturi = 0
        total_facturi_neachitate = 0
        total_plati = 0

        for cont in conturi:
            raw_cont = cont.date_brute or {}
            cod_client = str(raw_cont.get("cod_client") or raw_cont.get("codClient") or "").strip()
            nr_contract = str(raw_cont.get("nr_contract") or raw_cont.get("nrContract") or "").strip()
            cheie = _cheie_contract(cod_client, nr_contract)
            facturi_cont = [f for f in facturi if f.id_cont == cont.id_cont]
            plati_cont = [p for p in plati_rows if not cod_client or str(p.get("codClient") or "").strip() == cod_client]
            transmitere_cont = [r for r in transmitere_rows if (not cod_client or str(r.get("codClient") or "").strip() == cod_client) and (not nr_contract or str(r.get("nrContract") or "").strip() == nr_contract)]
            consum_cont = [r for r in consum_rows if not transmitere_cont or str(r.get("contor") or "") in {str(t.get("contor") or "") for t in transmitere_cont}]
            neachitate = [f for f in facturi_cont if f.stare in {"neplatita", "scadenta"}]
            sold = _valoare_numerica((date_brute.get("solduri") or {}).get(cheie))
            if sold is None:
                sold = round(sum(float(f.date_brute.get("rest_plata") or 0) for f in neachitate), 2)
            total_de_plata += max(sold or 0, 0)
            total_facturi += len(facturi_cont)
            total_facturi_neachitate += len(neachitate)
            total_plati += len(plati_cont)

            ultima_factura = facturi_cont[0] if facturi_cont else None
            ultima_plata = _ultima_dupa_data(plati_cont, "dataPlata")
            ultim_consum = _ultima_dupa_data(consum_cont, "dataConsum")
            ultim_transmitere = _ultima_dupa_data(transmitere_cont, "dataUltima")
            index_val = None
            index_data = None
            if ultim_transmitere:
                index_val = _valoare_numerica(ultim_transmitere.get("indexNou") or ultim_transmitere.get("indexVechi"))
                index_data = _date_text(_data_sigura(ultim_transmitere.get("dataCitire") or ultim_transmitere.get("dataUltima")))
            if index_val is None and ultim_consum:
                index_val = _valoare_numerica(ultim_consum.get("indexNou"))
                index_data = _date_text(_data_sigura(ultim_consum.get("dataConsum")))

            consumuri.extend([
                ConsumUtilitate("de_plata", round(float(sold or 0), 2), "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("sold_curent", round(float(sold or 0), 2), "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("factura_restanta", "da" if (sold or 0) > 0 else "nu", None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_facturi", len(facturi_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_facturi_neachitate", len(neachitate), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_plati", len(plati_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
                ConsumUtilitate("numar_contoare", _numar_contoare(date_brute, cheie, transmitere_cont, consum_cont), "buc", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal"),
            ])
            if ultima_factura:
                consumuri.extend([
                    ConsumUtilitate("valoare_ultima_factura", ultima_factura.valoare, "RON", perioada=_date_text(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("id_ultima_factura", _numar_factura_afisat(ultima_factura), None, perioada=_date_text(ultima_factura.data_emitere), id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("data_ultima_factura", _date_text(ultima_factura.data_emitere), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate("urmatoarea_scadenta", _date_text(ultima_factura.data_scadenta), None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura.date_brute),
                ])
            if ultima_plata:
                data_plata = _date_text(_data_sigura(ultima_plata.get("dataPlata")))
                consumuri.extend([
                    ConsumUtilitate("data_ultima_plata", data_plata, None, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                    ConsumUtilitate("valoare_ultima_plata", _valoare_numerica(ultima_plata.get("valoarePlata")), "RON", id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                    ConsumUtilitate("ultima_plata", _valoare_numerica(ultima_plata.get("valoarePlata")), "RON", perioada=data_plata, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_plata),
                ])
            if index_val is not None:
                consumuri.append(ConsumUtilitate("index_contor", index_val, "m³", perioada=index_data, id_cont=cont.id_cont, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultim_transmitere or ultim_consum or {}))

        ultima_factura_global = facturi[0] if facturi else None
        consumuri.extend([
            ConsumUtilitate("de_plata", round(total_de_plata, 2), "RON", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("sold_curent", round(total_de_plata, 2), "RON", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("total_neachitat", round(total_de_plata, 2), "RON", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_conturi", len(conturi), "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_conturi_curent", 0, "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_conturi_gaz", 0, "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("numar_facturi", total_facturi, "buc", tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("tipuri_servicii", "apa_canal", None, tip_utilitate="apa", tip_serviciu="apa_canal"),
            ConsumUtilitate("este_prosumator", "nu", None, tip_utilitate="apa", tip_serviciu="apa_canal"),
        ])
        if ultima_factura_global:
            consumuri.extend([
                ConsumUtilitate("id_ultima_factura", _numar_factura_afisat(ultima_factura_global), None, perioada=_date_text(ultima_factura_global.data_emitere), tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura_global.date_brute),
                ConsumUtilitate("valoare_ultima_factura", ultima_factura_global.valoare, "RON", perioada=_date_text(ultima_factura_global.data_emitere), tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura_global.date_brute),
                ConsumUtilitate("urmatoarea_scadenta", _date_text(ultima_factura_global.data_scadenta), None, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute=ultima_factura_global.date_brute),
            ])
        return consumuri


def _records_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows: list[dict[str, Any]] = []
    for record in payload.get("records") or []:
        if isinstance(record, dict) and isinstance(record.get("row"), dict):
            rows.append(record["row"])
    return rows


def _cheie_contract(cod_client: str | None, nr_contract: str | None) -> str:
    cod = str(cod_client or "").strip()
    nr = str(nr_contract or "").strip()
    return f"{cod}_{nr}" if cod and nr else cod or nr


def _gaseste_cont(conturi: list[ContUtilitate], cod_client: str | None, nr_contract: str | None) -> str | None:
    cheie = _cheie_contract(cod_client, nr_contract)
    for cont in conturi:
        raw = cont.date_brute or {}
        if cont.id_cont == cheie:
            return cont.id_cont
        if str(raw.get("cod_client") or raw.get("codClient") or "").strip() == str(cod_client or "").strip() and str(raw.get("nr_contract") or raw.get("nrContract") or "").strip() == str(nr_contract or "").strip():
            return cont.id_cont
    return None


def _data_sigura(value: Any) -> date | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    match = re.search(r"/Date\((-?\d+)\)/", text)
    if match:
        try:
            return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc).date()
        except (ValueError, OSError, OverflowError):
            return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    return None


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _valoare_numerica(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            match = re.search(r"-?\d+(?:[.,]\d+)?", value.replace(" ", ""))
            if not match:
                return None
            value = match.group(0).replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return None


def _ultima_dupa_data(items: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    if not items:
        return None
    return sorted(items, key=lambda item: _data_sigura(item.get(key)) or date.min, reverse=True)[0]


def _numar_contoare(date_brute: dict[str, Any], cheie: str, transmitere: list[dict[str, Any]], consum: list[dict[str, Any]]) -> int:
    for puncte in (date_brute.get("puncte_consum") or {}).get(cheie) or []:
        if not isinstance(puncte, dict):
            continue
        id_locatie = str(puncte.get("idLocatie") or "")
        contoare = (date_brute.get("contoare_puncte") or {}).get(id_locatie)
        if isinstance(contoare, list) and contoare:
            return len(contoare)
    serii = {str(r.get("contor") or "").strip() for r in transmitere + consum if str(r.get("contor") or "").strip()}
    return len(serii)


def _numar_factura_afisat(factura: FacturaUtilitate) -> str:
    raw = factura.date_brute or {}
    for key in ("serie_numar", "numar_factura", "factura"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return str(factura.titlu or factura.id_factura or "").strip()


def _nume_cont_apa_ilfov(adresa: str, nr_contract: str, cod_client: str) -> str:
    parti = [p for p in (adresa.strip(), f"contract {nr_contract}" if nr_contract else "", f"client {cod_client}" if cod_client else "") if p]
    return "Apă Ilfov - " + " (".join(parti[:1]) + (f" ({nr_contract})" if nr_contract else "")


def _pare_login_sau_cloudflare(text: str) -> bool:
    t = (text or "").lower()
    return "cf-turnstile" in t or "cloudflare" in t or "autentificare" in t and "parola" in t


def _safe_dataclass(obj: Any) -> str:
    return repr(obj)[:1200]


def _rezumat_debug(date_brute: dict[str, Any]) -> dict[str, Any]:
    return {
        "contracte": len(date_brute.get("contracte") or []),
        "facturi": len(_records_rows(date_brute.get("facturi_raw"))),
        "plati": len(_records_rows(date_brute.get("plati_raw"))),
        "transmitere": len(_records_rows(date_brute.get("transmitere_raw"))),
        "consum": len(_records_rows(date_brute.get("consum_raw"))),
        "contracte_sample": (date_brute.get("contracte") or [])[:3],
        "facturi_sample": _records_rows(date_brute.get("facturi_raw"))[:3],
        "plati_sample": _records_rows(date_brute.get("plati_raw"))[:3],
        "transmitere_sample": _records_rows(date_brute.get("transmitere_raw"))[:3],
        "consum_sample": _records_rows(date_brute.get("consum_raw"))[:3],
        "solduri": date_brute.get("solduri") or {},
        "puncte_consum": date_brute.get("puncte_consum") or {},
        "adrese_puncte": date_brute.get("adrese_puncte") or {},
        "contoare_puncte": date_brute.get("contoare_puncte") or {},
    }


def _log_debug_apa_ilfov(label: str, payload: Any) -> None:
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        text = str(payload)
    _LOGGER.warning("Diagnostic Apa Ilfov: %s: %s", label, text[:12000])
