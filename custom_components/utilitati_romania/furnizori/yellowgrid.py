"""Furnizor YellowGrid pentru Utilitati Romania: telemetrie si sold, doar citire."""

from __future__ import annotations

import logging
from typing import Any

from ..const import (
    COD_TARA_IMPLICIT_YELLOWGRID, CONF_YELLOWGRID_COUNTRY_CODE,
)
from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ContUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor
from .yellowgrid_api import (
    SesiuneYellowGrid, interval_zi_romania, normalizare_fluxuri,
    normalizare_telemetrie, numar_api,
)

_LOGGER = logging.getLogger(__name__)


class ClientFurnizorYellowGrid(ClientFurnizor):
    cheie_furnizor = "yellowgrid"
    nume_prietenos = "YellowGrid"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self._api = SesiuneYellowGrid(
            sesiune,
            telefon=utilizator,
            parola=parola,
            cod_tara=str(optiuni.get(CONF_YELLOWGRID_COUNTRY_CODE) or COD_TARA_IMPLICIT_YELLOWGRID),
        )

    @property
    def istoric_diagnostic(self) -> list[dict[str, Any]]:
        return self._api.istoric_diagnostic

    async def async_testeaza_conexiunea(self) -> str:
        instalatii = await self._api.get_plants()
        if not instalatii:
            raise EroareParsare("YellowGrid: contul nu are instalatii disponibile")
        customer = self._api.customer_id
        if not customer:
            raise EroareAutentificare("YellowGrid: identificator cont indisponibil")
        return customer

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        instalatii = await self._api.get_plants()
        if not instalatii:
            raise EroareConectare("YellowGrid: nu au fost returnate instalatii")
        start, end = interval_zi_romania()
        conturi: list[ContUtilitate] = []
        reusite = 0
        for index, planta in enumerate(instalatii, start=1):
            device_id = planta.get("deviceId")
            if not isinstance(device_id, str) or not device_id:
                continue
            nume = str(planta.get("plantName") or f"Instalatia {index}").strip()
            valori: dict[str, Any] = {}
            try:
                valori.update(normalizare_telemetrie(await self._api.get_telemetry(device_id)))
                reusite += 1
            except (EroareConectare, EroareParsare) as err:
                _LOGGER.debug("YellowGrid: telemetrie temporar indisponibila: %s", type(err).__name__)
            try:
                valori.update(normalizare_fluxuri(await self._api.get_flows(device_id, start, end)))
                reusite += 1
            except (EroareConectare, EroareParsare) as err:
                _LOGGER.debug("YellowGrid: fluxuri temporar indisponibile: %s", type(err).__name__)
            # API-ul de castiguri este disponibil, insa interpretarea financiara
            # din aplicatie nu a fost confirmata; nu publicam senzori monetari estimati.
            conturi.append(ContUtilitate(
                id_cont=device_id,
                nume=nume,
                tip_cont="instalatie_fotovoltaica",
                stare=valori.get("state"),
                tip_utilitate="energie electrica",
                tip_serviciu="solar",
                date_brute={"yellowgrid": valori},
            ))

        portofel: dict[str, float | None] = {}
        try:
            wallet = await self._api.get_wallet()
            portofel = {
                "balance": numar_api(wallet.get("balance")),
                "withdrawable": numar_api(wallet.get("withdrawalableAmount")),
                "unpaid": numar_api(wallet.get("unpaidAmount")),
            }
            reusite += 1
        except (EroareConectare, EroareParsare) as err:
            _LOGGER.debug("YellowGrid: portofel temporar indisponibil: %s", type(err).__name__)

        if not conturi or reusite == 0:
            raise EroareConectare("YellowGrid: nu s-au putut citi datele disponibile")
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=[],
            consumuri=[],
            extra={"yellowgrid_wallet": portofel},
        )
