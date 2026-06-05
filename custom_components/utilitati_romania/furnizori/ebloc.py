from __future__ import annotations

from dataclasses import dataclass
import asyncio
from datetime import date, datetime
import logging
import re
import time
from typing import Any

import aiohttp
from aiohttp import ClientSession, ClientTimeout

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_EBLOC = "https://www.e-bloc.ro"
URL_API_EBLOC = "https://www.e-bloc.ro/ajax"
TIMEOUT_EBLOC = ClientTimeout(total=30)

ANTETE_WEB_EBLOC = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
}


class EroareApiEbloc(Exception):
    pass


class EroareAutentificareEbloc(EroareApiEbloc):
    pass


class EroareConectareEbloc(EroareApiEbloc):
    pass


class EroareRaspunsEbloc(EroareApiEbloc):
    pass


@dataclass(slots=True)
class SesiuneEbloc:
    id_sesiune: str
    id_utilizator: str


@dataclass(slots=True)
class PlataEbloc:
    id_plata: str
    data_plata: date | None
    valoare: float | None
    descriere: str | None
    date_brute: dict[str, Any]


@dataclass(slots=True)
class ListaPlataEbloc:
    luna: str | None
    valoare: float | None
    sold_curent: float | None
    nr_persoane: int | None
    date_brute: dict[str, Any]


@dataclass(slots=True)
class ContorEbloc:
    id_contor: str
    nume: str
    index_precedent: float | None
    index_curent: float | None
    consum: float | None
    unitate: str | None
    citire_permisa: bool | None
    perioada_citire: str | None
    date_brute: dict[str, Any]


class ClientApiEbloc:
    def __init__(self, sesiune: ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator
        self._parola = parola
        self._id_sesiune: str | None = None
        self._id_utilizator: str | None = None
        self._asociatii: list[dict[str, Any]] = []
        self._apartamente: dict[str, list[dict[str, Any]]] = {}
        self._drepturi: dict[str, bool] = {}
        self._luna_curenta: str | None = None
        self._date_info: dict[str, Any] = {}

    async def async_login(self) -> SesiuneEbloc:
        try:
            async with self._sesiune.post(
                f"{URL_EBLOC}/index.php?profil=0",
                data={"pUser": self._utilizator, "pPass": self._parola},
                headers={
                    "User-Agent": ANTETE_WEB_EBLOC["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": URL_EBLOC,
                    "Referer": f"{URL_EBLOC}/",
                },
                timeout=TIMEOUT_EBLOC,
                allow_redirects=True,
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    raise EroareAutentificareEbloc("Autentificare e-bloc.ro respinsă")
                if raspuns.status >= 400:
                    raise EroareConectareEbloc(f"HTTP {raspuns.status} la autentificarea e-bloc.ro")
                if "pUser" in text and "pPass" in text and "iesire" not in text.lower() and "logout" not in text.lower():
                    raise EroareAutentificareEbloc("Datele de autentificare e-bloc.ro par să fie invalide")
        except EroareApiEbloc:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareEbloc(f"Eroare de conectare la e-bloc.ro: {err}") from err

        self._id_sesiune = "web"
        self._id_utilizator = self._utilizator.lower()
        return SesiuneEbloc(id_sesiune="web", id_utilizator=self._id_utilizator)

    async def _asigura_sesiune(self) -> None:
        if not self._id_sesiune:
            await self.async_login()

    async def _cerere_web(
        self,
        endpoint: str,
        parametri: dict[str, Any],
    ) -> dict[str, Any]:
        await self._asigura_sesiune()
        try:
            async with self._sesiune.post(
                f"{URL_API_EBLOC}/{endpoint}",
                data={k: v for k, v in parametri.items() if v is not None},
                headers={
                    "User-Agent": ANTETE_WEB_EBLOC["User-Agent"],
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Origin": URL_EBLOC,
                    "Referer": f"{URL_EBLOC}/index.php?page=10",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=TIMEOUT_EBLOC,
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    raise EroareAutentificareEbloc(f"Sesiune web e-bloc.ro invalidă pentru {endpoint}")
                if raspuns.status >= 400:
                    raise EroareConectareEbloc(f"HTTP {raspuns.status} pentru {endpoint}: {text[:300]}")
                try:
                    data = await raspuns.json(content_type=None)
                except Exception as err:
                    raise EroareRaspunsEbloc(f"Răspuns JSON invalid pentru {endpoint}: {text[:300]}") from err
        except EroareApiEbloc:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareEbloc(f"Eroare de conectare la {endpoint}: {err}") from err

        return data if isinstance(data, dict) else {"data": data}

    async def _pagina_web(self, cale: str, *, referer: str | None = None) -> str:
        await self._asigura_sesiune()
        url = cale if cale.startswith("http") else f"{URL_EBLOC}/{cale.lstrip('/')}"
        try:
            async with self._sesiune.get(
                url,
                headers={
                    "User-Agent": ANTETE_WEB_EBLOC["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": referer or URL_EBLOC,
                },
                timeout=TIMEOUT_EBLOC,
                allow_redirects=True,
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    raise EroareAutentificareEbloc(f"Sesiune web e-bloc.ro invalidă pentru pagina {cale}")
                if raspuns.status >= 400:
                    raise EroareConectareEbloc(f"HTTP {raspuns.status} pentru pagina {cale}")
                return text
        except EroareApiEbloc:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareEbloc(f"Eroare de conectare la pagina {cale}: {err}") from err

    async def async_descopera_conturi(self) -> dict[str, Any]:
        await self._asigura_sesiune()

        self._date_info = {"sursa": "portal_web"}
        self._drepturi = {
            "acasa": True,
            "plati": True,
            "facturi": True,
            "avizier": True,
            "index": True,
            "contact": True,
        }

        asociatii: list[dict[str, Any]] = []
        apartamente: dict[str, list[dict[str, Any]]] = {}

        for cale in (
            f"index.php?page=19&t={int(time.time())}",
            f"index.php?page=10&t={int(time.time())}",
            "index.php?page=11",
        ):
            pagina = await self._obtine_pagina_web_sigur(cale)
            gasite_asoc, gasite_ap = _extrage_conturi_din_pagina_web(pagina)
            asociatii = _imbina_liste_dupa_id(asociatii, gasite_asoc, "id_asoc")
            for id_asociatie, lista in gasite_ap.items():
                apartamente[id_asociatie] = _imbina_liste_dupa_id(apartamente.get(id_asociatie, []), lista, "id_ap")
            if asociatii and any(apartamente.values()):
                break

        for asociatie in asociatii:
            id_asociatie = str(asociatie.get("id_asoc") or asociatie.get("id") or "").strip()
            if not id_asociatie:
                continue

            info_asoc = await self._obtine_web_sigur("AjaxGetInfoAsoc.php", {"pIdAsoc": id_asociatie})
            info_principal = _prima_intrare_dict(info_asoc)
            if info_principal:
                asociatie.update({k: v for k, v in info_principal.items() if v not in (None, "")})

            data_ap = await self._obtine_web_sigur("AjaxGetHomeAp.php", {"pIdAsoc": id_asociatie})
            lista_ap = _extrage_apartamente_din_web(data_ap, id_asociatie)
            if lista_ap:
                apartamente[id_asociatie] = _imbina_liste_dupa_id(apartamente.get(id_asociatie, []), lista_ap, "id_ap")

            index_luni = await self._obtine_web_sigur("AjaxGetIndexLuni.php", {"pIdAsoc": id_asociatie})
            luna = _extrage_luna_index_web(index_luni)
            if luna and not self._luna_curenta:
                self._luna_curenta = luna

        self._asociatii = asociatii
        self._apartamente = {k: v for k, v in apartamente.items() if v}

        if not self._asociatii or not any(self._apartamente.values()):
            raise EroareRaspunsEbloc("Nu am putut identifica asociațiile și apartamentele din portalul web e-bloc.ro")

        return {
            "info": self._date_info,
            "asociatii": self._asociatii,
            "apartamente": self._apartamente,
            "drepturi": self._drepturi,
            "luna_curenta": self._luna_curenta,
        }

    async def async_obtine_date(self) -> dict[str, Any]:
        if not self._apartamente:
            await self.async_descopera_conturi()

        date_apartamente: dict[str, dict[str, Any]] = {}
        limita_cereri = asyncio.Semaphore(6)

        async def cerere_limitata(endpoint: str, parametri: dict[str, Any]) -> dict[str, Any]:
            async with limita_cereri:
                return await self._obtine_web_sigur(endpoint, parametri)

        async def pagina_limitata(cale: str) -> str:
            async with limita_cereri:
                return await self._obtine_pagina_web_sigur(cale)

        for id_asociatie, apartamente in self._apartamente.items():
            lista_apartamente = [
                apartament
                for apartament in apartamente
                if str(apartament.get("id_ap") or apartament.get("id") or "").strip()
                and str(apartament.get("id_ap") or apartament.get("id") or "").strip() != "0"
            ]
            if not lista_apartamente:
                continue

            # Datele la nivel de asociație sunt aceleași pentru toate apartamentele.
            # Le citim o singură dată, nu repetat pentru fiecare apartament.
            index_luni_web, facturi_luni_web, datorii_toti_web, plati_toti_web, pagina_contoare = await asyncio.gather(
                cerere_limitata("AjaxGetIndexLuni.php", {"pIdAsoc": id_asociatie}),
                cerere_limitata("AjaxGetFacturiLuni.php", {"pIdAsoc": id_asociatie}),
                cerere_limitata("AjaxGetPlatiDatoriiToti.php", {"pIdAsoc": id_asociatie, "pIdAp": -1}),
                cerere_limitata("AjaxGetPlatiChitanteToti.php", {"pIdAsoc": id_asociatie, "pIdAp": -1}),
                pagina_limitata(f"index.php?page=10&t={int(time.time())}"),
            )

            luna_index = _extrage_luna_index_web(index_luni_web) or self._luna_curenta
            factura_luna = _extrage_luna_facturi_web(facturi_luni_web) or luna_index

            facturi_plati_web, contoare_web_selectat = await asyncio.gather(
                cerere_limitata("AjaxGetFacturiPlati.php", {"pIdAsoc": id_asociatie, "pLuna": factura_luna}) if factura_luna else _rezultat_gol_async(),
                cerere_limitata("AjaxGetIndexContoare.php", {"pIdAsoc": id_asociatie, "pLuna": luna_index, "pIdAp": -1}) if luna_index else _rezultat_gol_async(),
            )

            async def obtine_pachet_apartament(apartament: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
                id_apartament = str(apartament.get("id_ap") or apartament.get("id") or "").strip()
                if not id_apartament or id_apartament == "0":
                    return None

                cheie = f"{id_asociatie}:{id_apartament}"
                home_info_web, home_stat_web, datorii_web, plati_web, plati_ap_web, contoare_web_apartament = await asyncio.gather(
                    cerere_limitata("AjaxGetHomeApInfo.php", {"pIdAsoc": id_asociatie, "pIdAp": id_apartament}),
                    cerere_limitata("AjaxGetHomeStat.php", {"pIdAsoc": id_asociatie, "pIdAp": id_apartament, "pLuni": 12}),
                    cerere_limitata("AjaxGetPlatiDatorii.php", {"pIdAsoc": id_asociatie, "pIdAp": id_apartament}),
                    cerere_limitata("AjaxGetPlatiChitante.php", {"pIdAsoc": id_asociatie, "pIdAp": id_apartament}),
                    cerere_limitata("AjaxGetPlatiAp.php", {"pIdAsoc": id_asociatie, "pIdAp": id_apartament}),
                    cerere_limitata("AjaxGetIndexContoare.php", {"pIdAsoc": id_asociatie, "pLuna": luna_index, "pIdAp": id_apartament}) if luna_index else _rezultat_gol_async(),
                )

                return cheie, {
                    "asociatie_id": id_asociatie,
                    "apartament_id": id_apartament,
                    "apartament": apartament,
                    "home_info_web": home_info_web,
                    "home_stat_web": home_stat_web,
                    "datorii_web": datorii_web,
                    "datorii_toti_web": datorii_toti_web,
                    "plati_web": plati_web,
                    "plati_toti_web": plati_toti_web,
                    "plati_ap_web": plati_ap_web,
                    "facturi_luni_web": facturi_luni_web,
                    "facturi_plati_web": facturi_plati_web,
                    "index_luni_web": index_luni_web,
                    "contoare_web_selectat": contoare_web_selectat,
                    "contoare_web_apartament": contoare_web_apartament,
                    "pagina_contoare": pagina_contoare,
                    "luna_index": luna_index,
                }

            rezultate_apartamente = await asyncio.gather(
                *(obtine_pachet_apartament(apartament) for apartament in lista_apartamente)
            )
            for rezultat in rezultate_apartamente:
                if rezultat is None:
                    continue
                cheie, pachet = rezultat
                date_apartamente[cheie] = pachet

        return {
            "info": self._date_info,
            "asociatii": self._asociatii,
            "apartamente": self._apartamente,
            "drepturi": self._drepturi,
            "luna_curenta": self._luna_curenta,
            "date_apartamente": date_apartamente,
        }

    async def _obtine_web_sigur(self, endpoint: str, parametri: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._cerere_web(endpoint, parametri)
        except Exception as err:
            _LOGGER.debug("Endpoint web e-bloc.ro indisponibil %s: %s", endpoint, err)
            return {}

    async def _obtine_pagina_web_sigur(self, cale: str) -> str:
        try:
            return await self._pagina_web(cale)
        except Exception as err:
            _LOGGER.debug("Pagina web e-bloc.ro indisponibilă %s: %s", cale, err)
            return ""

    async def async_seteaza_numar_persoane(
        self,
        id_asociatie: str,
        id_apartament: str,
        luna: str,
        numar_persoane: int,
    ) -> dict[str, Any]:
        await self._asigura_sesiune()
        return await self._cerere_web(
            "AjaxSetNrPers.php",
            {
                "pIdAsoc": id_asociatie,
                "pIdAp": id_apartament,
                "pLuna": luna,
                "pNrPers": int(numar_persoane),
            },
        )


class ClientFurnizorEbloc(ClientFurnizor):
    cheie_furnizor = "ebloc"
    nume_prietenos = "e-bloc.ro"

    def __init__(self, *, sesiune: ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiEbloc(sesiune, utilizator, parola)

    async def async_testeaza_conexiunea(self) -> str:
        try:
            sesiune = await self.api.async_login()
            await self.api.async_descopera_conturi()
        except EroareAutentificareEbloc as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareEbloc as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsEbloc as err:
            raise EroareParsare(str(err)) from err

        return sesiune.id_utilizator or self.utilizator.lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            date_brute = await self.api.async_obtine_date()
        except EroareAutentificareEbloc as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareEbloc as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsEbloc as err:
            raise EroareParsare(str(err)) from err

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute)
        consumuri = self._mapeaza_consumuri(date_brute, conturi, facturi)

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={
                "sumar": {
                    "numar_conturi": len(conturi),
                    "numar_facturi": len(facturi),
                    "total_rest_de_plata": _suma([f.valoare for f in facturi if f.stare != "platita"]),
                },
                "drepturi": date_brute.get("drepturi", {}),
                "luna_curenta": date_brute.get("luna_curenta"),
                "date_brute": _compacteaza_date_brute(date_brute),
            },
        )

    async def async_seteaza_numar_persoane(self, id_cont: str, luna: str, numar_persoane: int) -> dict[str, Any]:
        parti = str(id_cont or "").split("_", 1)
        if len(parti) != 2:
            raise EroareRaspunsEbloc("ID cont e-bloc.ro invalid pentru actualizarea numărului de persoane")
        return await self.api.async_seteaza_numar_persoane(parti[0], parti[1], luna, numar_persoane)

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        asociatii = {
            str(item.get("id_asoc") or item.get("id") or ""): item
            for item in _lista(date_brute.get("asociatii"))
        }
        rezultate: list[ContUtilitate] = []

        for id_asociatie, apartamente in (date_brute.get("apartamente") or {}).items():
            asociatie = asociatii.get(str(id_asociatie), {})
            nume_asociatie = str(
                asociatie.get("denumire")
                or asociatie.get("nume")
                or asociatie.get("asoc")
                or f"Asociația {id_asociatie}"
            ).strip()

            for apartament in _lista(apartamente):
                id_apartament = str(apartament.get("id_ap") or apartament.get("id") or "").strip()
                if not id_apartament:
                    continue

                numar_ap = str(apartament.get("ap") or apartament.get("apartament") or apartament.get("nr_ap") or id_apartament).strip()
                nume = str(apartament.get("nume") or apartament.get("proprietar") or apartament.get("locatar") or "").strip()
                cod_client = str(apartament.get("cod_client") or apartament.get("cod") or "").strip()

                rezultate.append(
                    ContUtilitate(
                        id_cont=f"{id_asociatie}_{id_apartament}",
                        nume=f"Apartament {numar_ap}" if not nume else f"Apartament {numar_ap} - {nume}",
                        tip_cont="apartament",
                        id_contract=cod_client or None,
                        adresa=nume_asociatie,
                        stare="activ",
                        tip_utilitate="administrare_bloc",
                        tip_serviciu="administrare_bloc",
                        date_brute={
                            "id_asociatie": id_asociatie,
                            "id_apartament": id_apartament,
                            "numar_apartament": numar_ap,
                            "apartament": apartament,
                            "asociatie": asociatie,
                        },
                    )
                )

        return rezultate

    def _mapeaza_facturi(self, date_brute: dict[str, Any]) -> list[FacturaUtilitate]:
        rezultate: list[FacturaUtilitate] = []

        for cheie, pachet in (date_brute.get("date_apartamente") or {}).items():
            if not isinstance(pachet, dict):
                continue

            apartament = pachet.get("apartament") or {}
            id_cont = cheie.replace(":", "_")
            lista_plata = _construieste_lista_plata(pachet, date_brute.get("luna_curenta"))
            luna = lista_plata.luna or date_brute.get("luna_curenta") or "curent"

            if lista_plata.valoare is None:
                continue

            rezultate.append(
                FacturaUtilitate(
                    id_factura=f"ebloc_{id_cont}_{_slug(luna)}",
                    titlu=f"Întreținere {luna}",
                    valoare=lista_plata.valoare,
                    moneda="RON",
                    data_emitere=_data_emitere_din_luna(luna),
                    data_scadenta=None,
                    stare="platita" if (lista_plata.sold_curent or 0) <= 0 else "neplatita",
                    categorie="intretinere",
                    id_cont=id_cont,
                    id_contract=str(apartament.get("cod_client") or "") or None,
                    tip_utilitate="administrare_bloc",
                    tip_serviciu="administrare_bloc",
                    date_brute={
                        "apartament": apartament,
                        "lista_plata": lista_plata.date_brute,
                        "sold_curent": lista_plata.sold_curent,
                        "nr_persoane": lista_plata.nr_persoane,
                    },
                )
            )

        rezultate.sort(key=lambda item: item.data_emitere or date.min, reverse=True)
        return rezultate

    def _mapeaza_consumuri(
        self,
        date_brute: dict[str, Any],
        conturi: list[ContUtilitate],
        facturi: list[FacturaUtilitate],
    ) -> list[ConsumUtilitate]:
        rezultate: list[ConsumUtilitate] = [
            ConsumUtilitate("numar_apartamente", len(conturi), "buc"),
            ConsumUtilitate("numar_facturi", len(facturi), "buc"),
            ConsumUtilitate(
                "sold_curent",
                _suma([_construieste_lista_plata(pachet, date_brute.get("luna_curenta")).sold_curent for pachet in (date_brute.get("date_apartamente") or {}).values()]),
                "RON",
            ),
            ConsumUtilitate(
                "total_neachitat",
                _suma([_construieste_lista_plata(pachet, date_brute.get("luna_curenta")).sold_curent for pachet in (date_brute.get("date_apartamente") or {}).values()]),
                "RON",
            ),
        ]

        for cheie, pachet in (date_brute.get("date_apartamente") or {}).items():
            id_cont = cheie.replace(":", "_")
            lista_plata = _construieste_lista_plata(pachet, date_brute.get("luna_curenta"))
            plati = (
                _extrage_plati_web(pachet.get("plati_web") or {})
                or _extrage_plati_web(pachet.get("plati_ap_web") or {})
                or _extrage_plati_web(pachet.get("plati_toti_web") or {})
            )
            contoare = _extrage_contoare(
                _alege_sursa_contoare(pachet),
                pachet.get("luna_index"),
                pachet,
            )

            ultima_plata = plati[0] if plati else None
            ultima_factura = next((f for f in facturi if f.id_cont == id_cont), None)

            rezultate.extend(
                [
                    ConsumUtilitate("sold_curent", lista_plata.sold_curent, "RON", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("total_neachitat", lista_plata.sold_curent, "RON", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("de_plata", round(max(float(lista_plata.sold_curent or 0), 0.0), 2), "RON", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("valoare_lista_plata", lista_plata.valoare, "RON", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("luna_lista_plata", lista_plata.luna, None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("numar_persoane", lista_plata.nr_persoane, "pers", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("id_ultima_factura", ultima_factura.id_factura if ultima_factura else None, None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("valoare_ultima_factura", ultima_factura.valoare if ultima_factura else None, "RON", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("numar_facturi", len([f for f in facturi if f.id_cont == id_cont]), "buc", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("numar_plati", len(plati), "buc", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("istoric_plati", _rezumat_plati(plati, limita=12), None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc", date_brute={"plati": [_plata_ca_dict(p) for p in plati[:12]]}),
                    ConsumUtilitate("data_ultima_plata", ultima_plata.data_plata.isoformat() if ultima_plata and ultima_plata.data_plata else None, None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("valoare_ultima_plata", ultima_plata.valoare if ultima_plata else None, "RON", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("numar_contoare", len(contoare), "buc", id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("perioada_citire", _prima_valoare([c.perioada_citire for c in contoare]) or _perioada_citire_lunara((pachet or {}).get("index_luni_web") or {}, pachet.get("luna_index")), None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate(
                        "citire_index_permisa",
                        "da" if _citire_index_permisa_din_luni((pachet or {}).get("index_luni_web") or {}, pachet.get("luna_index")) else "nu",
                        None,
                        id_cont=id_cont,
                        tip_utilitate="administrare_bloc",
                        tip_serviciu="administrare_bloc",
                    ),
                    ConsumUtilitate(
                        "zile_pana_citire_index",
                        _zile_pana_citire_lunara((pachet or {}).get("index_luni_web") or {}, pachet.get("luna_index")),
                        "zile",
                        id_cont=id_cont,
                        tip_utilitate="administrare_bloc",
                        tip_serviciu="administrare_bloc",
                    ),
                    ConsumUtilitate("editare_persoane_permisa", "da" if _permite_editare_persoane(pachet) else "nu", None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("luna_setare_persoane", _luna_setare_persoane(pachet, date_brute.get("luna_curenta")), None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                    ConsumUtilitate("urmatoarea_scadenta", _prima_intrare_dict(pachet.get("home_info_web") or {}).get("ultima_zi_plata"), None, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc"),
                ]
            )

            for contor in contoare:
                slug_contor = _slug(contor.nume or contor.id_contor)
                rezultate.extend(
                    [
                        ConsumUtilitate(f"index_precedent_{slug_contor}", contor.index_precedent, contor.unitate, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc", date_brute=contor.date_brute),
                        ConsumUtilitate(f"index_curent_{slug_contor}", contor.index_curent, contor.unitate, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc", date_brute=contor.date_brute),
                        ConsumUtilitate(f"consum_{slug_contor}", contor.consum, contor.unitate, id_cont=id_cont, tip_utilitate="administrare_bloc", tip_serviciu="administrare_bloc", date_brute=contor.date_brute),
                    ]
                )

        return rezultate



async def _rezultat_gol_async() -> dict[str, Any]:
    return {}

def _extrage_conturi_din_pagina_web(html: str) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    asociatii = _extrage_vector_js(html, "gInfoAsoc")
    apartamente_lista = _extrage_vector_js(html, "gInfoAp")

    asociatii_curate: list[dict[str, Any]] = []
    for item in asociatii:
        id_asociatie = str(item.get("id_asoc") or item.get("id") or "").strip()
        if not id_asociatie:
            continue
        item.setdefault("id_asoc", id_asociatie)
        asociatii_curate.append(item)

    apartamente: dict[str, list[dict[str, Any]]] = {}
    for item in apartamente_lista:
        id_asociatie = str(item.get("id_asoc") or "").strip()
        id_apartament = str(item.get("id_ap") or item.get("id") or "").strip()
        if not id_asociatie or not id_apartament or id_apartament == "0":
            continue
        item.setdefault("id_ap", id_apartament)
        apartamente.setdefault(id_asociatie, []).append(item)

    return asociatii_curate, apartamente


def _extrage_vector_js(html: str, nume_variabila: str) -> list[dict[str, Any]]:
    if not html:
        return []

    rezultat: dict[str, dict[str, Any]] = {}
    model = re.compile(
        rf"{re.escape(nume_variabila)}\s*\[\s*(\d+)\s*\]\s*\[\s*[\"']([^\"']+)[\"']\s*\]\s*=\s*([\"'])(.*?)\3\s*;",
        re.DOTALL,
    )
    for match in model.finditer(html):
        index, cheie, _, valoare = match.groups()
        rezultat.setdefault(index, {})[cheie] = _decodeaza_text_web(valoare)

    return [rezultat[index] for index in sorted(rezultat, key=lambda v: int(v))]


def _decodeaza_text_web(text: str) -> str:
    return (
        str(text or "")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\/", "/")
        .strip()
    )


def _imbina_liste_dupa_id(lista_initiala: list[dict[str, Any]], lista_noua: list[dict[str, Any]], cheie_id: str) -> list[dict[str, Any]]:
    rezultat: list[dict[str, Any]] = []
    pozitii: dict[str, dict[str, Any]] = {}

    for item in [*lista_initiala, *lista_noua]:
        if not isinstance(item, dict):
            continue
        ident = str(item.get(cheie_id) or item.get("id") or "").strip()
        if not ident:
            continue
        if ident not in pozitii:
            copie = dict(item)
            copie.setdefault(cheie_id, ident)
            pozitii[ident] = copie
            rezultat.append(copie)
        else:
            pozitii[ident].update({k: v for k, v in item.items() if v not in (None, "")})

    return rezultat


def _extrage_apartamente_din_web(data: dict[str, Any], id_asociatie: str) -> list[dict[str, Any]]:
    candidati = []
    if isinstance(data, dict):
        for cheie in ("aInfoAp", "apartamente", "ap", "data", "rows"):
            valoare = data.get(cheie)
            if isinstance(valoare, list):
                candidati.extend([item for item in valoare if isinstance(item, dict)])
        if not candidati:
            for valoare in data.values():
                if isinstance(valoare, dict) and any(k in valoare for k in ("id_ap", "ap", "apartament")):
                    candidati.append(valoare)

    rezultate: list[dict[str, Any]] = []
    for item in candidati:
        id_apartament = str(item.get("id_ap") or item.get("id") or "").strip()
        if not id_apartament or id_apartament == "0":
            continue
        copie = dict(item)
        copie.setdefault("id_asoc", id_asociatie)
        copie.setdefault("id_ap", id_apartament)
        rezultate.append(copie)
    return rezultate


def _extrage_luna_index_web(data: dict[str, Any]) -> str | None:
    if not isinstance(data, dict):
        return None
    for valoare in data.values():
        if isinstance(valoare, dict):
            luna = valoare.get("luna") or valoare.get("id") or valoare.get("value")
            if luna not in (None, ""):
                return str(luna).strip()
        elif isinstance(valoare, list):
            for item in valoare:
                if isinstance(item, dict):
                    luna = item.get("luna") or item.get("id") or item.get("value")
                    if luna not in (None, ""):
                        return str(luna).strip()
    return _extrage_luna(data)


def _extrage_luna_facturi_web(data: dict[str, Any]) -> str | None:
    return _extrage_luna_index_web(data)

def _lista(valoare: Any) -> list[Any]:
    if isinstance(valoare, list):
        return valoare
    if isinstance(valoare, dict):
        if all(isinstance(v, dict) for v in valoare.values()):
            return list(valoare.values())
        return [valoare]
    return []


def _dicturi_din_structura(valoare: Any) -> list[dict[str, Any]]:
    rezultate: list[dict[str, Any]] = []
    if isinstance(valoare, dict):
        if valoare and all(isinstance(v, dict) for v in valoare.values()):
            for item in valoare.values():
                rezultate.extend(_dicturi_din_structura(item))
        else:
            rezultate.append(valoare)
            for item in valoare.values():
                if isinstance(item, (dict, list)):
                    rezultate.extend(_dicturi_din_structura(item))
    elif isinstance(valoare, list):
        for item in valoare:
            rezultate.extend(_dicturi_din_structura(item))
    return rezultate


def _dict(valoare: Any) -> dict[str, Any]:
    return valoare if isinstance(valoare, dict) else {}


def _float_sigur(valoare: Any) -> float | None:
    if valoare in (None, "", "null", "-", "Necunoscut"):
        return None
    try:
        text = str(valoare).strip()
        text = text.replace("Lei", "").replace("RON", "").replace("lei", "")
        text = text.replace("\xa0", " ").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", ".")
        text = re.sub(r"[^0-9.\-]", "", text)
        if text in ("", ".", "-", "-."):
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _bani_sigur(valoare: Any) -> float | None:
    numeric = _float_sigur(valoare)
    if numeric is None:
        return None

    text = str(valoare or "")
    are_separator_zecimal = "," in text or "." in text

    if abs(numeric) >= 10000 and float(numeric).is_integer() and not are_separator_zecimal:
        return round(numeric / 100, 2)

    return round(numeric, 2)


def _int_sigur(valoare: Any) -> int | None:
    numeric = _float_sigur(valoare)
    if numeric is None:
        return None
    return int(numeric)


def _data_sigura(valoare: Any) -> date | None:
    if not valoare:
        return None
    text = str(valoare).strip()
    luni = {
        "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4, "mai": 5, "iunie": 6,
        "iulie": 7, "august": 8, "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12,
        "ian": 1, "feb": 2, "mar": 3, "apr": 4, "iun": 6, "iul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    match = re.search(r"(\d{1,2})\s+([A-Za-zăâîșțĂÂÎȘȚ]+)\s+(\d{4})", text, re.IGNORECASE)
    if match:
        luna = luni.get(match.group(2).lower())
        if luna:
            return date(int(match.group(3)), luna, int(match.group(1)))

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _data_emitere_din_luna(luna_text: Any) -> date | None:
    if not luna_text:
        return None

    text = str(luna_text).strip().lower()
    luni = {
        "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4, "mai": 5, "iunie": 6,
        "iulie": 7, "august": 8, "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12,
    }
    for nume, luna in luni.items():
        if nume in text:
            an = re.search(r"(20\d{2})", text)
            if an:
                return date(int(an.group(1)), luna, 1)

    match = re.search(r"(20\d{2})[-_/](\d{1,2})", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), 1)

    return None


def _extrage_luna(data: dict[str, Any]) -> str | None:
    for cheie in ("luna", "luna_curenta", "luna_afisata", "luna_lista", "luna_plata"):
        valoare = data.get(cheie)
        if valoare not in (None, ""):
            return str(valoare).strip()

    for valoare in data.values():
        if isinstance(valoare, dict):
            luna = _extrage_luna(valoare)
            if luna:
                return luna
        elif isinstance(valoare, list):
            for item in valoare:
                if isinstance(item, dict):
                    luna = _extrage_luna(item)
                    if luna:
                        return luna
    return None



def _prima_intrare_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if "1" in data and isinstance(data["1"], dict):
            return data["1"]
        for valoare in data.values():
            if isinstance(valoare, dict):
                return valoare
        return data
    return {}


def _are_lista_contoare(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    for item in _dicturi_din_structura(data):
        if str(item.get("id_contor") or item.get("id_index") or "").strip() not in ("", "0"):
            return True
        for cheie in ("aInfoContoare", "contoare", "aContoare", "aIndex", "data", "rows"):
            if _lista(item.get(cheie)):
                return True
    return False


def _alege_sursa_contoare(pachet: dict[str, Any]) -> dict[str, Any]:
    for cheie in ("contoare_web_apartament", "contoare_web_selectat"):
        data = pachet.get(cheie) or {}
        if _are_lista_contoare(data):
            return data
    return pachet.get("contoare_web_apartament") or pachet.get("contoare_web_selectat") or {}


def _perioada_citire_din_home_info(home_info_web: dict[str, Any]) -> str | None:
    info = _prima_intrare_dict(home_info_web)
    start = info.get("citire_contoare_start") or info.get("citiri_contoare_inceput")
    end = info.get("citire_contoare_end") or info.get("citiri_contoare_sfarsit")
    if not start or not end:
        return None
    return f"{_formateaza_data_ro(start)} - {_formateaza_data_ro(end)}"


def _luna_setare_persoane(pachet: dict[str, Any], luna_curenta: str | None) -> str | None:
    info = _prima_intrare_dict(pachet.get("home_info_web") or {})
    return str(info.get("nr_pers_set_luna_min") or info.get("luna_curenta") or luna_curenta or "").strip() or None


def _permite_editare_persoane(pachet: dict[str, Any]) -> bool:
    info = _prima_intrare_dict(pachet.get("home_info_web") or {})
    return str(info.get("right_edit_pers") or "0").strip() == "1"


def _extrage_sold_curent(pachet: dict[str, Any]) -> float | None:
    for sursa in (
        _prima_intrare_dict(pachet.get("home_info_web") or {}),
        _prima_intrare_dict(pachet.get("datorii_web") or {}),
        _prima_intrare_dict(pachet.get("datorii_toti_web") or {}),
        _prima_intrare_dict(pachet.get("plati_ap_web") or {}),
        pachet.get("apartament") or {},
    ):
        for cheie in (
            "suma_de_plata",
            "suma_plata",
            "total_de_plata",
            "de_plata",
            "rest_plata",
            "rest_de_plata",
            "sold_curent",
            "sold",
            "datorie",
        ):
            valoare = _bani_sigur(_dict(sursa).get(cheie))
            if valoare is not None:
                return valoare
    return None


def _extrage_nr_persoane(pachet: dict[str, Any]) -> int | None:
    chei_persoane = ("nr_pers_afisat", "nr_pers", "nr_persoane", "numar_persoane", "persoane", "nrpers")
    for sursa in (
        _prima_intrare_dict(pachet.get("home_info_web") or {}),
        pachet.get("apartament") or {},
    ):
        sursa = _dict(sursa)
        for cheie in chei_persoane:
            if cheie not in sursa:
                continue
            valoare = _int_sigur(sursa.get(cheie))
            if valoare is None:
                continue
            if valoare >= 1000 and valoare % 1000 == 0:
                valoare = valoare // 1000
            if 0 <= valoare <= 50:
                return valoare
    return None

def _extrage_plati(istoric_plati: dict[str, Any]) -> list[PlataEbloc]:
    candidati = (
        _lista(istoric_plati.get("aChitante"))
        or _lista(istoric_plati.get("chitante"))
        or _lista(istoric_plati.get("plati"))
        or _lista(istoric_plati.get("data"))
    )

    rezultate: list[PlataEbloc] = []
    for item in candidati:
        if not isinstance(item, dict):
            continue

        id_plata = str(
            item.get("id")
            or item.get("id_chitanta")
            or item.get("nr_chitanta")
            or item.get("numar")
            or item.get("numar_chitanta")
            or ""
        ).strip()

        data_plata = _data_sigura(item.get("data") or item.get("data_chitanta") or item.get("data_plata"))
        valoare = _bani_sigur(
            item.get("suma")
            or item.get("valoare")
            or item.get("total")
            or item.get("total_plata")
            or item.get("suma_platita")
        )

        if not id_plata:
            id_plata = f"{data_plata.isoformat() if data_plata else 'fara_data'}_{valoare or 'fara_suma'}"

        rezultate.append(
            PlataEbloc(
                id_plata=id_plata,
                data_plata=data_plata,
                valoare=valoare,
                descriere=str(item.get("descriere") or item.get("detalii") or "").strip() or None,
                date_brute=item,
            )
        )

    rezultate.sort(key=lambda plata: plata.data_plata or date.min, reverse=True)
    return rezultate




def _extrage_plati_web(data: dict[str, Any]) -> list[PlataEbloc]:
    candidati: list[dict[str, Any]] = []
    for cheie in ("aChitante", "chitante", "plati", "data", "rows"):
        candidati.extend([x for x in _lista(data.get(cheie)) if isinstance(x, dict)] if isinstance(data, dict) else [])

    if not candidati:
        candidati = [x for x in _dicturi_din_structura(data) if isinstance(x, dict)]

    rezultate: list[PlataEbloc] = []
    for item in candidati:
        if not isinstance(item, dict):
            continue

        id_plata = str(
            item.get("id")
            or item.get("id_chitanta")
            or item.get("nr_chitanta")
            or item.get("numar")
            or item.get("numar_chitanta")
            or item.get("nr")
            or item.get("serie")
            or ""
        ).strip()

        data_plata = _data_sigura(
            item.get("data")
            or item.get("data_chitanta")
            or item.get("data_plata")
            or item.get("date")
            or item.get("data_doc")
        )

        valoare = _bani_sigur(
            item.get("suma")
            or item.get("valoare")
            or item.get("total")
            or item.get("total_plata")
            or item.get("suma_platita")
            or item.get("amount")
            or item.get("val")
        )

        if data_plata is None and valoare is None and not id_plata:
            continue

        if not id_plata:
            id_plata = f"{data_plata.isoformat() if data_plata else 'fara_data'}_{valoare or 'fara_suma'}"

        rezultate.append(
            PlataEbloc(
                id_plata=id_plata,
                data_plata=data_plata,
                valoare=valoare,
                descriere=str(item.get("descriere") or item.get("detalii") or item.get("text") or item.get("explicatie") or "").strip() or None,
                date_brute=item,
            )
        )

    rezultate.sort(key=lambda plata: plata.data_plata or date.min, reverse=True)
    return rezultate


def _plata_ca_dict(plata: PlataEbloc) -> dict[str, Any]:
    return {
        "id_plata": plata.id_plata,
        "data": plata.data_plata.isoformat() if plata.data_plata else None,
        "valoare": plata.valoare,
        "descriere": plata.descriere,
    }


def _rezumat_plati(plati: list[PlataEbloc], *, limita: int = 12) -> str | None:
    if not plati:
        return None

    bucati = []
    for plata in plati[:limita]:
        data_text = plata.data_plata.strftime("%d.%m.%Y") if plata.data_plata else "fără dată"
        valoare_text = f"{plata.valoare:.2f} RON" if isinstance(plata.valoare, (int, float)) else "valoare necunoscută"
        bucati.append(f"{data_text}: {valoare_text}")

    return " | ".join(bucati)

def _extrage_valoare_lista_din_structura(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None

    chei_directe = (
        "valoare_lista_plata",
        "suma_lista_plata",
        "total_lista_plata",
        "total_lista",
        "total_intretinere",
        "total_luna",
        "total_de_plata_luna",
        "valoare_intretinere",
        "suma_intretinere",
        "intretinere_luna",
    )

    for cheie in chei_directe:
        valoare = _bani_sigur(data.get(cheie))
        if valoare is not None and 0 <= valoare < 10000:
            return valoare

    liste_posibile = (
        data.get("aListaPlata"),
        data.get("lista_plata"),
        data.get("aFacturi"),
        data.get("facturi"),
        data.get("cheltuieli"),
        data.get("data"),
    )

    for lista in liste_posibile:
        if not isinstance(lista, list):
            continue

        total = 0.0
        gasit = False
        for item in lista:
            if not isinstance(item, dict):
                continue

            valoare = None
            for cheie in chei_directe + ("valoare", "suma", "total"):
                valoare = _bani_sigur(item.get(cheie))
                if valoare is not None:
                    break

            if valoare is not None and 0 <= valoare < 10000:
                total += valoare
                gasit = True

        if gasit:
            return round(total, 2)

    return None
def _construieste_lista_plata(pachet: dict[str, Any], luna_curenta: str | None) -> ListaPlataEbloc:
    sold_curent = _extrage_sold_curent(pachet)
    home_info = _prima_intrare_dict(pachet.get("home_info_web") or {})
    luna = (
        str(home_info.get("luna_afisata") or "").strip()
        or _extrage_luna(pachet.get("datorii_web") or {})
        or _extrage_luna(pachet.get("facturi_plati_web") or {})
        or luna_curenta
    )
    nr_persoane = _extrage_nr_persoane(pachet)

    valoare = _extrage_valoare_lista_din_structura(pachet.get("datorii_web") or {})
    if valoare is None:
        valoare = _extrage_valoare_lista_din_structura(pachet.get("facturi_plati_web") or {})
    if valoare is None:
        valoare = _extrage_valoare_lista_din_structura(pachet.get("home_info_web") or {})

    plati = (
        _extrage_plati_web(pachet.get("plati_web") or {})
        or _extrage_plati_web(pachet.get("plati_ap_web") or {})
        or _extrage_plati_web(pachet.get("plati_toti_web") or {})
    )
    if valoare is None and plati:
        valoare = plati[0].valoare

    if sold_curent is None:
        sold_curent = 0.0 if valoare is not None and plati else None

    return ListaPlataEbloc(
        luna=luna,
        valoare=valoare,
        sold_curent=sold_curent,
        nr_persoane=nr_persoane,
        date_brute={
            "home_info_web": pachet.get("home_info_web") or {},
            "datorii_web": pachet.get("datorii_web") or {},
            "facturi_plati_web": pachet.get("facturi_plati_web") or {},
        },
    )




def _adauga_luni(data_initiala: date, luni: int) -> date:
    luna = data_initiala.month - 1 + luni
    an = data_initiala.year + luna // 12
    luna = luna % 12 + 1

    zile_luna = (
        31,
        29 if an % 4 == 0 and (an % 100 != 0 or an % 400 == 0) else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    )
    zi = min(data_initiala.day, zile_luna[luna - 1])
    return date(an, luna, zi)


def _interval_citire_lunar(index_luni_web: dict[str, Any], luna: str | None) -> tuple[date, date] | None:
    if not isinstance(index_luni_web, dict):
        return None

    luna_norm = str(luna or "").strip()
    candidati: list[tuple[date, date]] = []

    for item in index_luni_web.values():
        if not isinstance(item, dict):
            continue

        item_luna = str(item.get("luna") or "").strip()
        if luna_norm and item_luna and item_luna != luna_norm and not candidati:
            # Nu ignorăm definitiv: unele asociații trimit luna greșită, dar zilele sunt corecte.
            pass

        start = _data_sigura(item.get("citiri_contoare_inceput") or item.get("citire_contoare_start"))
        end = _data_sigura(item.get("citiri_contoare_sfarsit") or item.get("citire_contoare_end"))

        if start and end:
            candidati.append((start, end))
            if item_luna == luna_norm:
                break

    if not candidati:
        return None

    start, end = candidati[0]
    azi = date.today()

    # e-bloc poate returna o lună viitoare greșită, dar zilele sunt recurente lunar.
    # Păstrăm zilele și calculăm intervalul relevant pentru luna curentă sau următoarea.
    start_curent = date(azi.year, azi.month, min(start.day, 28 if azi.month == 2 else start.day))
    end_curent = date(azi.year, azi.month, min(end.day, 28 if azi.month == 2 else end.day))

    # Dacă intervalul trece peste final de lună, mutăm finalul în luna următoare.
    if end.day < start.day:
        end_curent = _adauga_luni(end_curent, 1)

    if azi <= end_curent:
        return start_curent, end_curent

    return _adauga_luni(start_curent, 1), _adauga_luni(end_curent, 1)


def _perioada_citire_lunara(index_luni_web: dict[str, Any], luna: str | None) -> str | None:
    interval = _interval_citire_lunar(index_luni_web, luna)
    if not interval:
        return None
    start, end = interval
    return f"{_formateaza_data_ro(start)} - {_formateaza_data_ro(end)}"


def _zile_pana_citire_lunara(index_luni_web: dict[str, Any], luna: str | None) -> int | None:
    interval = _interval_citire_lunar(index_luni_web, luna)
    if not interval:
        return None
    start, end = interval
    azi = date.today()
    if start <= azi <= end:
        return 0
    return max((start - azi).days, 0)

def _citire_index_permisa_din_luni(index_luni_web: dict[str, Any], luna: str | None) -> bool | None:
    interval = _interval_citire_lunar(index_luni_web, luna)
    if not interval:
        return None
    start, end = interval
    azi = date.today()
    return start <= azi <= end

def _perioada_citire_din_luni(index_luni_web: dict[str, Any], luna: str | None) -> str | None:
    if not isinstance(index_luni_web, dict):
        return None

    luna_norm = str(luna or "").strip()
    primul_interval: tuple[date, date] | None = None

    for item in index_luni_web.values():
        if not isinstance(item, dict):
            continue

        start = _data_sigura(item.get("citiri_contoare_inceput") or item.get("citire_contoare_start"))
        end = _data_sigura(item.get("citiri_contoare_sfarsit") or item.get("citire_contoare_end"))

        if not start or not end:
            continue

        if primul_interval is None:
            primul_interval = (start, end)

        item_luna = str(item.get("luna") or "").strip()
        if luna_norm and item_luna and item_luna != luna_norm:
            continue

        return f"{_formateaza_data_ro(start)} - {_formateaza_data_ro(end)}"

    if primul_interval:
        start, end = primul_interval
        return f"{_formateaza_data_ro(start)} - {_formateaza_data_ro(end)}"

    return None

def _extrage_contoare(data: dict[str, Any], luna_index: str | None, pachet: dict[str, Any] | None = None) -> list[ContorEbloc]:
    id_apartament = str((pachet or {}).get("apartament_id") or "").strip()
    candidati = [x for x in _dicturi_din_structura(data) if isinstance(x, dict)]

    perioada = (
        _perioada_citire_din_home_info((pachet or {}).get("home_info_web") or {})
        or _perioada_citire_lunara((pachet or {}).get("index_luni_web") or {}, luna_index)
        or _perioada_citire(
            data,
            luna_index,
            surse_suplimentare=[
                (pachet or {}).get("index_luni_web") or {},
                (pachet or {}).get("contoare_web_selectat") or {},
                (pachet or {}).get("contoare_web_apartament") or {},
                {"pagina_contoare": (pachet or {}).get("pagina_contoare") or ""},
            ],
        )
    )

    rezultate: list[ContorEbloc] = []
    for item in candidati:
        item_id_ap = str(item.get("id_ap") or item.get("id_apartament") or "").strip()
        if id_apartament and item_id_ap and item_id_ap not in {id_apartament, "0"}:
            continue

        id_contor = str(item.get("id_contor") or item.get("id_index") or item.get("id") or "").strip()
        if id_contor in ("", "0"):
            continue

        nume = str(item.get("titlu") or item.get("nume") or item.get("denumire") or item.get("contor") or item.get("tip") or item.get("nume_contor") or "").strip()
        index_precedent = _float_sigur(item.get("index_precedent") or item.get("index_vechi") or item.get("index_old") or item.get("precedent") or item.get("indexPrec"))
        index_curent = _float_sigur(item.get("index_curent") or item.get("index_nou") or item.get("index") or item.get("valoare") or item.get("indexNou"))
        consum = _float_sigur(item.get("consum"))
        if consum is None and index_precedent is not None and index_curent is not None:
            consum = round(index_curent - index_precedent, 3)

        rezultate.append(
            ContorEbloc(
                id_contor=id_contor,
                nume=nume or id_contor or "Contor",
                index_precedent=index_precedent,
                index_curent=index_curent,
                consum=consum,
                unitate=str(item.get("unitate") or item.get("um") or "mc").strip() or None,
                citire_permisa=_bool_sigur(item.get("citire_permisa") or item.get("editabil") or item.get("permite_citire") or item.get("flag")),
                perioada_citire=perioada,
                date_brute=item,
            )
        )

    return rezultate


def _perioada_citire(
    data: dict[str, Any],
    luna_index: str | None,
    *,
    surse_suplimentare: list[dict[str, Any]] | None = None,
) -> str | None:
    surse = [data]
    if surse_suplimentare:
        surse.extend([s for s in surse_suplimentare if isinstance(s, dict)])

    for sursa in surse:
        interval = _interval_explicit(sursa)
        if interval:
            return interval

    for sursa in surse:
        interval = _cauta_interval_in_structura(sursa)
        if interval:
            return interval

    return None


def _interval_explicit(data: dict[str, Any]) -> str | None:
    perechi = (
        ("data_start", "data_stop"),
        ("start", "stop"),
        ("inceput", "sfarsit"),
        ("interval_start", "interval_stop"),
        ("data_inceput", "data_sfarsit"),
        ("citire_start", "citire_stop"),
        ("data_start_citire", "data_stop_citire"),
    )

    for cheie_start, cheie_stop in perechi:
        start = data.get(cheie_start)
        stop = data.get(cheie_stop)
        if start and stop:
            return f"{_formateaza_data_ro(start)} - {_formateaza_data_ro(stop)}"

    for cheie in ("perioada_citire", "interval_citire", "interval", "mesaj_interval", "interval_index", "perioada_index"):
        valoare = data.get(cheie)
        if valoare:
            interval = _extrage_interval_text(str(valoare))
            if interval:
                return interval

    return None


def _cauta_interval_in_structura(data: Any) -> str | None:
    if isinstance(data, dict):
        for valoare in data.values():
            interval = _cauta_interval_in_structura(valoare)
            if interval:
                return interval

    if isinstance(data, list):
        for item in data:
            interval = _cauta_interval_in_structura(item)
            if interval:
                return interval

    if isinstance(data, str):
        return _extrage_interval_text(data)

    return None


def _extrage_interval_text(text: str) -> str | None:
    text = str(text or "").replace("\\/", "/")
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&icirc;", "î")
        .replace("&acirc;", "â")
    )
    text = " ".join(text.split())

    match_context = re.search(
        r"Citirea\s+inde[cx][șsţt]?ilor\s+se\s+face\s+(?:în|in)\s+intervalul\s*:?\s*"
        r"(\d{1,2}\s+[A-Za-zăâîșțĂÂÎȘȚ]+\s+20\d{2})\s*[-–]\s*"
        r"(\d{1,2}\s+[A-Za-zăâîșțĂÂÎȘȚ]+\s+20\d{2})",
        text,
        re.IGNORECASE,
    )
    if match_context:
        return f"{_formateaza_data_ro(match_context.group(1))} - {_formateaza_data_ro(match_context.group(2))}"

    modele = (
        r"(\d{1,2}\s+[A-Za-zăâîșțĂÂÎȘȚ]+\s+20\d{2})\s*[-–]\s*(\d{1,2}\s+[A-Za-zăâîșțĂÂÎȘȚ]+\s+20\d{2})",
        r"(\d{1,2}[./-]\d{1,2}[./-]20\d{2})\s*[-–]\s*(\d{1,2}[./-]\d{1,2}[./-]20\d{2})",
        r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2})\s*[-–]\s*(20\d{2}[./-]\d{1,2}[./-]\d{1,2})",
    )

    for model in modele:
        match = re.search(model, text, re.IGNORECASE)
        if match:
            return f"{_formateaza_data_ro(match.group(1))} - {_formateaza_data_ro(match.group(2))}"

    return None

def _formateaza_data_ro(valoare: Any) -> str:
    if isinstance(valoare, date):
        data = valoare
    else:
        data = _data_sigura(valoare)
    if not data:
        return str(valoare).strip()

    luni = (
        "Ianuarie", "Februarie", "Martie", "Aprilie", "Mai", "Iunie",
        "Iulie", "August", "Septembrie", "Octombrie", "Noiembrie", "Decembrie",
    )
    return f"{data.day:02d} {luni[data.month - 1]} {data.year}"
def _bool_sigur(valoare: Any) -> bool | None:
    if valoare in (None, ""):
        return None
    text = str(valoare).strip().lower()
    if text in ("1", "true", "da", "yes", "y"):
        return True
    if text in ("0", "false", "nu", "no", "n"):
        return False
    return None


def _prima_valoare(valori: list[Any]) -> Any:
    for valoare in valori:
        if valoare not in (None, ""):
            return valoare
    return None


def _suma(valori: list[float | None]) -> float:
    return round(sum(v for v in valori if isinstance(v, (int, float))), 2)


def _slug(text: Any) -> str:
    brut = str(text or "").strip().lower()
    rezultat = []
    for caracter in brut:
        if caracter.isalnum():
            rezultat.append(caracter)
        elif rezultat and rezultat[-1] != "_":
            rezultat.append("_")
    return "".join(rezultat).strip("_") or "necunoscut"


def _compacteaza_date_brute(date_brute: dict[str, Any]) -> dict[str, Any]:
    return {
        "numar_asociatii": len(_lista(date_brute.get("asociatii"))),
        "numar_apartamente": sum(len(_lista(v)) for v in (date_brute.get("apartamente") or {}).values()),
        "luna_curenta": date_brute.get("luna_curenta"),
        "drepturi": date_brute.get("drepturi", {}),
    }
