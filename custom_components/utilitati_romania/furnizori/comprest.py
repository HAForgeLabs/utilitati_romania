from __future__ import annotations

from datetime import date, datetime, timedelta
from html import unescape
import re
import time
from typing import Any
from urllib.parse import urljoin

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor


URL_BAZA = "https://client.comprest.ro"
URL_LOGIN = f"{URL_BAZA}/"
URL_PROCESARE_LOGIN = f"{URL_BAZA}/ro/process/"
URL_DASHBOARD = f"{URL_BAZA}/ro/contulmeu/dashboard/"
URL_CONTRACTE = f"{URL_BAZA}/ro/contulmeu/contracte-pool.json"
URL_FACTURI = f"{URL_BAZA}/ro/contulmeu/facturi-pool.json"
URL_INCASARI = f"{URL_BAZA}/ro/contulmeu/incasari-pool.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class EroareApiComprest(Exception):
    pass


class EroareAutentificareComprest(EroareApiComprest):
    pass


class EroareConectareComprest(EroareApiComprest):
    pass


class EroareRaspunsComprest(EroareApiComprest):
    pass


class ClientApiComprest:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._autentificat = False

    def _headers(self, *, ajax: bool = False, referer: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01" if ajax else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": USER_AGENT,
            "Referer": referer or URL_LOGIN,
        }
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    async def async_login(self) -> None:
        try:
            async with self._sesiune.get(
                URL_LOGIN,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                await raspuns.text()
                if raspuns.status >= 400:
                    raise EroareConectareComprest(f"Comprest a returnat HTTP {raspuns.status} la pagina de login")

            async with self._sesiune.post(
                URL_PROCESARE_LOGIN,
                headers={
                    **self._headers(referer=URL_LOGIN),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": URL_BAZA,
                },
                data={
                    "form_use": "1",
                    "sur": self._utilizator,
                    "wpd": self._parola,
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status not in (200, 302, 303):
                    raise EroareAutentificareComprest(f"Autentificare Comprest eșuată: HTTP {raspuns.status}")
                locatie = raspuns.headers.get("Location", "")
                if raspuns.status in (302, 303) and _pare_redirect_login_esuat(locatie):
                    raise EroareAutentificareComprest("Credentialele Comprest nu au fost acceptate")
                if raspuns.status == 200 and _pare_pagina_login(text):
                    raise EroareAutentificareComprest("Credentialele Comprest nu au fost acceptate")

            async with self._sesiune.get(
                URL_DASHBOARD,
                headers=self._headers(referer=URL_LOGIN),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    raise EroareAutentificareComprest("Sesiunea Comprest nu a fost creată corect")
                if raspuns.status >= 400:
                    raise EroareConectareComprest(f"Comprest a returnat HTTP {raspuns.status} la dashboard")
                if _pare_pagina_login(text):
                    raise EroareAutentificareComprest("Credentialele Comprest nu au fost acceptate")

            self._autentificat = True
        except EroareApiComprest:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareComprest(f"Eroare de conectare la Comprest: {err}") from err
        except TimeoutError as err:
            raise EroareConectareComprest("Timeout la Comprest") from err

    async def _get_json(self, url: str, *, params: dict[str, Any], referer: str) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        try:
            async with self._sesiune.get(
                url,
                params={**params, "_": int(time.time() * 1000)},
                headers=self._headers(ajax=True, referer=referer),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    self._autentificat = False
                    await self.async_login()
                    return await self._get_json(url, params=params, referer=referer)
                if raspuns.status >= 400:
                    raise EroareRaspunsComprest(f"Comprest a returnat HTTP {raspuns.status} pentru {url}: {text[:300]}")
                try:
                    json_data = await raspuns.json(content_type=None)
                except Exception as err:
                    raise EroareRaspunsComprest(f"Răspuns JSON invalid de la Comprest pentru {url}: {text[:300]}") from err
                if isinstance(json_data, dict):
                    return json_data
                if isinstance(json_data, list):
                    return {"rows": json_data, "total": len(json_data)}
                return {}
        except EroareApiComprest:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareComprest(f"Eroare de conectare la Comprest pentru {url}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareComprest(f"Timeout la Comprest pentru {url}") from err

    async def async_get_contracte(self) -> list[dict[str, Any]]:
        start, end = _interval_larg_contracte()
        raspuns = await self._get_json(
            URL_CONTRACTE,
            params={
                "range": f"{start} - {end}",
                "searchText": "",
                "sortName": "a.data_start",
                "sortOrder": "desc",
                "limit": 100,
                "offset": 0,
            },
            referer=f"{URL_BAZA}/ro/contulmeu/contracte-lista/",
        )
        return _extrage_lista(raspuns)

    async def async_get_facturi(self, id_contract: str | int | None = 0) -> list[dict[str, Any]]:
        start, end = _interval_larg_documente()
        params = {
            "range": f"{start} - {end}",
            "ctr_id": id_contract or 0,
            "searchText": "",
            "sortName": "a.id",
            "sortOrder": "desc",
            "limit": 100,
            "offset": 0,
        }
        raspuns = await self._get_json(
            URL_FACTURI,
            params=params,
            referer=f"{URL_BAZA}/ro/contulmeu/facturi-lista-{id_contract}/" if id_contract else f"{URL_BAZA}/ro/contulmeu/facturi-lista/",
        )
        return _extrage_lista(raspuns)

    async def async_get_incasari(self, id_factura: str | int | None = 0) -> list[dict[str, Any]]:
        start, end = _interval_larg_documente()
        raspuns = await self._get_json(
            URL_INCASARI,
            params={
                "range": f"{start} - {end}",
                "fact_id": id_factura or 0,
                "searchText": "",
                "sortName": "a.data_doc",
                "sortOrder": "desc",
                "limit": 100,
                "offset": 0,
            },
            referer=f"{URL_BAZA}/ro/contulmeu/incasari-lista/",
        )
        return _extrage_lista(raspuns)

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        contracte = await self.async_get_contracte()
        facturi = await self.async_get_facturi(0)
        return {"contracte": contracte, "facturi": facturi}

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()

        contracte = await self.async_get_contracte()
        facturi: list[dict[str, Any]] = []
        vazute_facturi: set[str] = set()

        for contract in contracte:
            id_contract = _id_contract(contract)
            if not id_contract:
                continue

            facturi_contract = await self.async_get_facturi(id_contract)
            for factura in facturi_contract:
                cheie = str(
                    _primul_text(
                        factura,
                        "id",
                        "fact_id",
                        "id_factura",
                        "nr_doc",
                        "numar",
                        "serie_numar",
                        adanc=True,
                    )
                    or _id_din_html(factura, "factura")
                    or factura
                )
                if cheie in vazute_facturi:
                    continue
                vazute_facturi.add(cheie)
                factura["_comprest_internal_ctr_id"] = id_contract
                factura.setdefault("ctr_id", id_contract)
                facturi.append(factura)

        if not facturi:
            facturi = await self.async_get_facturi(0)

        incasari = await self.async_get_incasari(0)
        return {"contracte": contracte, "facturi": facturi, "incasari": incasari}



class ClientFurnizorComprest(ClientFurnizor):
    cheie_furnizor = "comprest"
    nume_prietenos = "Comprest"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiComprest(self.sesiune, self.utilizator, self.parola)
        try:
            rezultat = await api.async_validate_credentials()
        except EroareAutentificareComprest as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareComprest as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsComprest as err:
            raise EroareParsare(str(err)) from err

        contracte = rezultat.get("contracte") or []
        contract = contracte[0] if contracte else {}
        unic = _id_contract(contract) or _primul_text(contract, "client_id", "cod_client", "cod", adanc=True) or self.utilizator
        return str(unic)

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiComprest(self.sesiune, self.utilizator, self.parola)
        try:
            date_brute = await api.async_get_all_data()
        except EroareAutentificareComprest as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareComprest as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsComprest as err:
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
            extra=self._construieste_extra(date_brute, facturi),
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        conturi: list[ContUtilitate] = []
        vazute: set[str] = set()
        for contract in date_brute.get("contracte", []) or []:
            if not isinstance(contract, dict):
                continue
            id_contract = _id_contract(contract)
            if not id_contract or id_contract in vazute:
                continue
            vazute.add(id_contract)
            adresa = _adresa_din_obiect(contract)
            nr_contract = _numar_contract_afisat(contract)
            cod_client = _curata_html(_primul_text(contract, "cod_client", "client_code", "cod", adanc=True))
            nume = _primul_text(contract, "denumire", "nume", "client", "beneficiar", "titular", "nume_client", adanc=True)
            date_contract = dict(contract)
            date_contract["_comprest_internal_ctr_id"] = id_contract
            if nr_contract:
                date_contract["numar_contract_afisat"] = nr_contract
            if cod_client:
                date_contract["cod_client_detectat"] = cod_client
            conturi.append(
                ContUtilitate(
                    id_cont=id_contract,
                    nume=_nume_cont_comprest(nume, adresa, nr_contract, cod_client, id_contract),
                    tip_cont="salubritate",
                    id_contract=id_contract,
                    adresa=adresa,
                    stare=_curata_html(_primul_text(contract, "stare", "status", "activ", adanc=True)),
                    tip_utilitate="salubritate",
                    tip_serviciu="salubritate",
                    date_brute=date_contract,
                )
            )

        if not conturi:
            conturi.append(
                ContUtilitate(
                    id_cont=self.utilizator.strip().lower(),
                    nume="Comprest",
                    tip_cont="salubritate",
                    tip_utilitate="salubritate",
                    tip_serviciu="salubritate",
                    date_brute={},
                )
            )
        return conturi

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        cont_principal = conturi[0] if conturi else None
        for factura in date_brute.get("facturi", []) or []:
            if not isinstance(factura, dict):
                continue
            id_factura = _id_factura(factura)
            if not id_factura:
                continue
            id_contract = str(factura.get("_comprest_internal_ctr_id") or "").strip() or _id_contract(factura) or _primul_text(factura, "ctr_id", "contract_id", "contract", adanc=True)
            id_cont = self._gaseste_id_cont(id_contract, factura, conturi) or (cont_principal.id_cont if cont_principal else None)
            valoare = _valoare_factura(factura)
            rest_plata = _rest_plata(factura, valoare)
            status = _deduce_stare_factura(factura, rest_plata)
            raw = dict(factura)
            raw["rest_plata"] = rest_plata
            raw["pdf_url"] = _url_factura(id_factura, factura)
            raw["numar_factura_afisat"] = _numar_factura_afisat(factura, id_factura)
            facturi.append(
                FacturaUtilitate(
                    id_factura=id_factura,
                    titlu=_titlu_factura(factura, id_factura),
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=_data_sigura(_primul_valoare(factura, "data_doc", "data", "data_emitere", "data_emiterii", "data_factura", "data_facturare", "issue_date", "date", "created_at", "createdAt")),
                    data_scadenta=_data_sigura(_primul_valoare(factura, "data_scadenta", "scadenta", "due_date", "dueDate", "termen", "termen_plata")),
                    stare=status,
                    categorie="consum",
                    id_cont=id_cont,
                    id_contract=id_contract or id_cont,
                    tip_utilitate="salubritate",
                    tip_serviciu="salubritate",
                    date_brute=raw,
                )
            )
        facturi.sort(key=lambda item: item.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont(self, id_contract: str | None, factura: dict[str, Any], conturi: list[ContUtilitate]) -> str | None:
        valori = {str(id_contract or "").strip()}
        for cheie in (
            "_comprest_internal_ctr_id",
            "ctr_id",
            "contract_id",
            "contract",
            "id_contract",
            "numar_contract",
            "nr_contract",
            "nr_ctr",
            "nr_ctr_data",
            "cod_client",
            "client_code",
            "cod",
        ):
            valoare = _primul_valoare(factura, cheie)
            if valoare not in (None, ""):
                valori.add(str(valoare).strip())
        nr_contract_factura = _numar_contract_afisat(factura)
        if nr_contract_factura:
            valori.add(nr_contract_factura)
        valori.discard("")

        for cont in conturi:
            raw = cont.date_brute or {}
            candidati = {cont.id_cont, cont.id_contract or ""}
            for cheie in (
                "id",
                "_comprest_internal_ctr_id",
                "ctr_id",
                "contract_id",
                "contract",
                "id_contract",
                "numar_contract",
                "nr_contract",
                "nr_ctr",
                "nr_ctr_data",
                "cod_client",
                "client_code",
                "cod",
            ):
                valoare = _primul_valoare(raw, cheie)
                if valoare not in (None, ""):
                    candidati.add(str(valoare).strip())
            nr_contract_cont = _numar_contract_afisat(raw)
            if nr_contract_cont:
                candidati.add(nr_contract_cont)
            candidati.discard("")
            if valori & candidati:
                return cont.id_cont
        return None

    def _mapeaza_consumuri(
        self,
        date_brute: dict[str, Any],
        conturi: list[ContUtilitate],
        facturi: list[FacturaUtilitate],
    ) -> list[ConsumUtilitate]:
        total_neachitat = sum(
            float(f.date_brute.get("rest_plata") if f.date_brute.get("rest_plata") is not None else f.valoare or 0)
            for f in facturi
            if f.stare in {"neplatita", "scadenta"}
        )
        consumuri = [
            ConsumUtilitate(cheie="sold_curent", valoare=round(total_neachitat, 2), unitate="RON", tip_utilitate="salubritate", tip_serviciu="salubritate"),
            ConsumUtilitate(cheie="numar_contracte", valoare=float(len(conturi)), unitate="buc", tip_utilitate="salubritate", tip_serviciu="salubritate"),
            ConsumUtilitate(cheie="numar_facturi", valoare=float(len(facturi)), unitate="buc", tip_utilitate="salubritate", tip_serviciu="salubritate"),
            ConsumUtilitate(cheie="numar_incasari", valoare=float(len(date_brute.get("incasari", []) or [])), unitate="buc", tip_utilitate="salubritate", tip_serviciu="salubritate"),
        ]

        incasari = [item for item in (date_brute.get("incasari", []) or []) if isinstance(item, dict)]
        ultima_incasare = _ultima_inregistrare_dupa_data(incasari)
        if ultima_incasare:
            consumuri.append(ConsumUtilitate(cheie="data_ultima_plata", valoare=_data_text(ultima_incasare), unitate=None, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_incasare))
            consumuri.append(ConsumUtilitate(cheie="valoare_ultima_plata", valoare=_float_sigur(_primul_valoare(ultima_incasare, "valoare", "suma", "total", "amount", "incasat", "valoare_doc")), unitate="RON", tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_incasare))

        ultima_factura_generala = facturi[0] if facturi else None
        if ultima_factura_generala is not None:
            consumuri.extend([
                ConsumUtilitate(cheie="id_ultima_factura", valoare=_numar_factura_afisat(ultima_factura_generala.date_brute or {}, ultima_factura_generala.id_factura), unitate=None, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura_generala.date_brute),
                ConsumUtilitate(cheie="valoare_ultima_factura", valoare=ultima_factura_generala.valoare, unitate="RON", tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura_generala.date_brute),
            ])

        for cont in conturi:
            facturi_cont = [f for f in facturi if f.id_cont == cont.id_cont]
            facturi_active = [f for f in facturi_cont if f.stare in {"neplatita", "scadenta"}]
            total_cont = round(sum(float(f.date_brute.get("rest_plata") if f.date_brute.get("rest_plata") is not None else f.valoare or 0) for f in facturi_active), 2)
            ultima_factura = facturi_cont[0] if facturi_cont else None
            scadenta_urmatoare = _prima_scadenta_activa(facturi_active)
            incasari_cont = [item for item in incasari if self._gaseste_id_cont(None, item, conturi) == cont.id_cont]
            ultima_plata_cont = _ultima_inregistrare_dupa_data(incasari_cont)

            consumuri.extend([
                ConsumUtilitate(cheie="de_plata", valoare=max(total_cont, 0.0), unitate="RON", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate"),
                ConsumUtilitate(cheie="sold_curent", valoare=total_cont, unitate="RON", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate"),
                ConsumUtilitate(cheie="sold_factura", valoare=total_cont, unitate="RON", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate"),
                ConsumUtilitate(cheie="numar_facturi", valoare=len(facturi_cont), unitate="buc", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate"),
                ConsumUtilitate(cheie="numar_facturi_neachitate", valoare=len(facturi_active), unitate="buc", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate"),
                ConsumUtilitate(cheie="factura_restanta", valoare="da" if total_cont > 0 else "nu", unitate=None, id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate"),
                ConsumUtilitate(cheie="numar_plati", valoare=len(incasari_cont), unitate="buc", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate"),
            ])

            if ultima_factura is not None:
                consumuri.extend([
                    ConsumUtilitate(cheie="id_ultima_factura", valoare=_numar_factura_afisat(ultima_factura.date_brute or {}, ultima_factura.id_factura), unitate=None, id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate(cheie="valoare_ultima_factura", valoare=ultima_factura.valoare, unitate="RON", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate(cheie="data_ultima_factura", valoare=ultima_factura.data_emitere.isoformat() if ultima_factura.data_emitere else None, unitate=None, id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura.date_brute),
                    ConsumUtilitate(cheie="urmatoarea_scadenta", valoare=scadenta_urmatoare.isoformat() if scadenta_urmatoare else (ultima_factura.data_scadenta.isoformat() if ultima_factura.data_scadenta else None), unitate=None, id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_factura.date_brute),
                ])

            if ultima_plata_cont:
                consumuri.extend([
                    ConsumUtilitate(cheie="data_ultima_plata", valoare=_data_text(ultima_plata_cont), unitate=None, id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_plata_cont),
                    ConsumUtilitate(cheie="valoare_ultima_plata", valoare=_float_sigur(_primul_valoare(ultima_plata_cont, "valoare", "suma", "total", "amount", "incasat", "valoare_doc")), unitate="RON", id_cont=cont.id_cont, tip_utilitate="salubritate", tip_serviciu="salubritate", date_brute=ultima_plata_cont),
                ])

        return consumuri

    def _construieste_extra(self, date_brute: dict[str, Any], facturi: list[FacturaUtilitate]) -> dict[str, Any]:
        return {
            "sumar": {
                "numar_contracte": len(date_brute.get("contracte", []) or []),
                "numar_facturi": len(facturi),
                "numar_facturi_neachitate": sum(1 for f in facturi if f.stare in {"neplatita", "scadenta"}),
                "ultima_factura_id": _numar_factura_afisat(facturi[0].date_brute or {}, facturi[0].id_factura) if facturi else None,
                "ultima_factura_scadenta": facturi[0].data_scadenta.isoformat() if facturi and facturi[0].data_scadenta else None,
                "ultima_factura_valoare": facturi[0].valoare if facturi else None,
            },
            "date_brute": {
                "contracte_count": len(date_brute.get("contracte", []) or []),
                "facturi_count": len(date_brute.get("facturi", []) or []),
                "incasari_count": len(date_brute.get("incasari", []) or []),
            },
        }


def _numar_contract_afisat(obiect: dict[str, Any]) -> str | None:
    for key in ("nr_ctr_data", "nr_ctr", "numar_contract", "nr_contract", "contract", "contract_no"):
        value = _curata_html(_primul_text(obiect, key, adanc=False))
        if value:
            match = re.search(r"\b(\d{3,})\b", value)
            return match.group(1) if match else value
    text = _text_total(obiect)
    # In tabelul Comprest numarul contractului apare de obicei ca "23292/01-03-2022".
    match = re.search(r"\b(\d{3,})/\d{2}-\d{2}-\d{4}\b", text)
    if match:
        return match.group(1)
    return None


def _nume_cont_comprest(nume: str | None, adresa: str | None, nr_contract: str | None, cod_client: str | None, id_contract: str) -> str:
    baza = _curata_html(adresa) or _curata_html(nume) or "Loc consum"
    detalii = nr_contract or cod_client or id_contract
    if detalii and detalii not in baza:
        return f"{baza} ({detalii})"
    return baza


def _prima_scadenta_activa(facturi: list[FacturaUtilitate]) -> date | None:
    scadente = [f.data_scadenta for f in facturi if f.data_scadenta is not None]
    if not scadente:
        return None
    azi = date.today()
    viitoare = [scadenta for scadenta in scadente if scadenta >= azi]
    return min(viitoare) if viitoare else max(scadente)


def _interval_larg_contracte() -> tuple[str, str]:
    end = date.today() + timedelta(days=30)
    return "2001-01-01", end.isoformat()


def _interval_larg_documente() -> tuple[str, str]:
    end = date.today() + timedelta(days=30)
    start = date.today() - timedelta(days=365 * 6)
    return start.isoformat(), end.isoformat()


def _extrage_lista(raspuns: Any) -> list[dict[str, Any]]:
    if isinstance(raspuns, list):
        return [item for item in raspuns if isinstance(item, dict)]
    if not isinstance(raspuns, dict):
        return []
    for key in ("rows", "data", "items", "results", "records", "docs", "contracte", "facturi", "incasari"):
        value = raspuns.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extrage_lista(value)
            if nested:
                return nested
    return []


def _normalizeaza_cheie(value: str) -> str:
    text = _normalizeaza_text(_curata_html(value) or value)
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _primul_valoare(obiect: dict[str, Any], *chei: str) -> Any:
    for cheie in chei:
        if cheie in obiect and obiect.get(cheie) not in (None, ""):
            return obiect.get(cheie)

    chei_normalizate = {_normalizeaza_cheie(cheie) for cheie in chei}
    for cheie_obiect, valoare in obiect.items():
        if valoare in (None, ""):
            continue
        cheie_norm = _normalizeaza_cheie(str(cheie_obiect))
        if cheie_norm in chei_normalizate:
            return valoare
        # Portalurile Bootstrap table pot trimite coloane cu prefixul aliasului SQL
        # de tipul "a.valoare", "ctr.nr_ctr" sau "a.data_facturare".
        # Pentru maparea internă ne interesează partea semantică de după prefix.
        if any(cheie_norm.endswith(f"_{cheie_cautata}") for cheie_cautata in chei_normalizate):
            return valoare

    return None


def _primul_text(obiect: dict[str, Any], *chei: str, adanc: bool = False) -> str | None:
    value = _primul_valoare(obiect, *chei)
    if value not in (None, ""):
        return str(value).strip()
    if adanc:
        for item in obiect.values():
            if isinstance(item, dict):
                found = _primul_text(item, *chei, adanc=True)
                if found:
                    return found
            elif isinstance(item, list):
                for subitem in item:
                    if isinstance(subitem, dict):
                        found = _primul_text(subitem, *chei, adanc=True)
                        if found:
                            return found
    return None


def _curata_html(valoare: Any) -> str | None:
    if valoare in (None, ""):
        return None
    text = unescape(str(valoare))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _text_total(obiect: dict[str, Any]) -> str:
    return " ".join(_curata_html(value) or "" for value in obiect.values()).strip()


def _id_din_html(obiect: dict[str, Any], tip: str) -> str | None:
    text = " ".join(str(value) for value in obiect.values())
    patterns = []
    if tip == "factura":
        patterns.extend([
            r"factura-(\d+)",
            r"facturi?/descarca/(\d+)",
            r"fact_id[=/](\d+)",
            r"id_factura[=/](\d+)",
        ])
    else:
        patterns.extend([
            r"facturi-lista-(\d+)",
            r"contract-(\d+)",
            r"ctr_id[=/](\d+)",
            r"id_contract[=/](\d+)",
        ])
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _id_contract(obiect: dict[str, Any]) -> str | None:
    din_link = _id_din_html(obiect, "contract")
    if din_link:
        return din_link

    direct = _primul_text(
        obiect,
        "ctr_id",
        "contract_id",
        "id_contract",
        "contract_internal_id",
        "id",
        adanc=False,
    )
    if direct:
        return direct

    return _primul_text(obiect, "numar_contract", "nr_contract", "nr_ctr_data", "contract", adanc=False)


def _id_factura(obiect: dict[str, Any]) -> str | None:
    din_link = _id_din_html(obiect, "factura")
    if din_link:
        return din_link

    direct = _primul_text(
        obiect,
        "fact_id",
        "id_factura",
        "invoice_id",
        "document_id",
        "id",
        adanc=False,
    )
    if direct:
        return direct

    return _primul_text(obiect, "factura", "serienr", "numar", "nr_doc", "numar_document", "serie_numar", adanc=False)


def _adresa_din_obiect(obiect: dict[str, Any]) -> str | None:
    direct = _primul_text(obiect, "adresa", "address", "loc_consum", "punct_consum", "locatie", "imobil", adanc=True)
    if direct:
        return _curata_html(direct)
    parti = []
    for key in ("localitate", "oras", "city", "strada", "street", "numar", "nr", "bloc", "scara", "apartament"):
        value = obiect.get(key)
        if value not in (None, ""):
            parti.append(_curata_html(value) or str(value).strip())
    return ", ".join(parti) or None


def _float_sigur(valoare: Any) -> float | None:
    if valoare in (None, "", "null"):
        return None
    if isinstance(valoare, bool):
        return None
    try:
        if isinstance(valoare, str):
            text = _curata_html(valoare) or valoare
            text = text.replace("RON", "").replace("Lei", "").replace("lei", "").replace(" ", "")
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", ".")
            match = re.search(r"-?\d+(?:\.\d+)?", text)
            if not match:
                return None
            valoare = match.group(0)
        return float(valoare)
    except (TypeError, ValueError):
        return None


def _data_sigura(valoare: Any) -> date | None:
    if not valoare:
        return None
    if isinstance(valoare, date) and not isinstance(valoare, datetime):
        return valoare
    text = _curata_html(valoare) or str(valoare).strip()
    match = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}[./-]\d{2}[./-]\d{4})", text)
    if match:
        text = match.group(1)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _valoare_factura(factura: dict[str, Any]) -> float | None:
    for key in (
        "total",
        "total_doc",
        "valoare_totala",
        "total_cu_tva",
        "valoare_cu_tva",
        "amount",
        "debit",
    ):
        value = _float_sigur(_primul_valoare(factura, key))
        if value is not None:
            return value

    valoare_fara_tva = _float_sigur(_primul_valoare(factura, "valoare_servicii", "valoare_serviciu", "valoare", "valoare_doc", "suma"))
    tva = _float_sigur(_primul_valoare(factura, "tva", "valoare_tva"))
    if valoare_fara_tva is not None and tva is not None:
        return round(valoare_fara_tva + tva, 2)
    if valoare_fara_tva is not None:
        return valoare_fara_tva

    return _float_sigur(_primul_valoare(factura, "sold", "sold_value", "rest_plata"))


def _rest_plata(factura: dict[str, Any], valoare: float | None) -> float | None:
    for key in ("rest", "rest_plata", "sold", "sold_value", "sold_curent", "de_plata", "neachitat", "remaining", "unpaid_amount"):
        value = _float_sigur(_primul_valoare(factura, key))
        if value is not None:
            return value
    status = _normalizeaza_text(_text_total(factura))
    if any(token in status for token in ("achitat", "platit", "platita", "stins")):
        return 0.0
    if any(token in status for token in ("neachitat", "neplatit", "neplatita", "de plata", "restant", "scadent")):
        return valoare
    return None


def _numar_factura_afisat(factura: dict[str, Any], fallback: str | None = None) -> str | None:
    numar = _curata_numar_factura(
        _curata_html(
            _primul_text(
                factura,
                "serienr",
                "factura",
                "serie_numar",
                "numar_document",
                "nr_doc",
                "document",
                adanc=False,
            )
        )
    )
    if numar:
        return numar

    serie = _curata_html(_primul_text(factura, "serie", adanc=False))
    numar_simplu = _curata_html(_primul_text(factura, "numar", adanc=False))

    parti = [item for item in (serie, numar_simplu) if item]
    if parti:
        return _curata_numar_factura(" ".join(parti))

    return _curata_numar_factura(fallback)


def _curata_numar_factura(valoare: Any) -> str | None:
    text = _curata_html(valoare)
    if not text:
        return None

    # Comprest trimite de obicei valoarea ca "COMBV 4025703 / 2026-05-31".
    # Pentru afișare păstrăm doar seria și numărul, fără data facturii.
    text = re.sub(r"\s*/\s*\d{4}-\d{2}-\d{2}\s*$", "", text).strip()
    text = re.sub(r"\s*/\s*\d{2}[.-]\d{2}[.-]\d{4}\s*$", "", text).strip()
    return text or None


def _titlu_factura(factura: dict[str, Any], id_factura: str) -> str:
    numar = _numar_factura_afisat(factura, id_factura) or id_factura
    return f"Factura {numar}"


def _url_factura(id_factura: str, factura: dict[str, Any]) -> str:
    text = " ".join(str(value) for value in factura.values())
    match = re.search(r"(?:href=\"|href='|)(/ro/contulmeu/factura-\d+)", text, re.IGNORECASE)
    if match:
        return urljoin(URL_BAZA, match.group(1))
    return f"{URL_BAZA}/ro/contulmeu/factura-{id_factura}"


def _data_text(obiect: dict[str, Any]) -> str | None:
    data = _data_sigura(_primul_valoare(obiect, "data_doc", "data", "date", "created_at", "createdAt", "paid_at", "paidAt"))
    return data.isoformat() if data else None


def _ultima_inregistrare_dupa_data(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [item for item in items if isinstance(item, dict)]
    if not valid:
        return None
    return sorted(valid, key=lambda item: _data_sigura(_primul_valoare(item, "data_doc", "data", "date", "created_at", "createdAt", "paid_at", "paidAt")) or date.min, reverse=True)[0]


def _deduce_stare_factura(factura: dict[str, Any], rest_plata: float | None) -> str:
    status = _normalizeaza_text(_text_total(factura))
    scadenta = _data_sigura(_primul_valoare(factura, "data_scadenta", "scadenta", "due_date", "dueDate", "termen", "termen_plata"))

    if rest_plata is not None:
        if rest_plata > 0:
            return "scadenta" if scadenta and scadenta < date.today() else "neplatita"
        return "platita"

    if any(token in status for token in ("neachitat", "neplatit", "neplatita", "de plata", "restant", "scadent")):
        return "scadenta" if scadenta and scadenta < date.today() else "neplatita"
    if any(token in status for token in ("achitat", "platit", "platita", "stins")):
        return "platita"
    return "necunoscuta"


def _normalizeaza_text(value: str) -> str:
    text = value.lower()
    replacements = {
        "ă": "a",
        "â": "a",
        "î": "i",
        "ș": "s",
        "ş": "s",
        "ț": "t",
        "ţ": "t",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return re.sub(r"\s+", " ", text).strip()


def _pare_pagina_login(text: str) -> bool:
    normalizat = _normalizeaza_text(_curata_html(text) or text)
    if not normalizat:
        return False
    return "contul meu" not in normalizat and any(token in normalizat for token in ("autentific", "parola", "wpd", "form_use"))


def _pare_redirect_login_esuat(locatie: str) -> bool:
    locatie = locatie.lower().strip()
    return bool(locatie and ("login" in locatie or "eroare" in locatie or "error" in locatie))
