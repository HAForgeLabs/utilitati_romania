from __future__ import annotations

from .apa_canal import ClientFurnizorApaCanal
from .apa_brasov import ClientFurnizorApaBrasov
from .apa_oradea import ClientFurnizorApaOradea
from .baza import ClientFurnizor
from .comprest import ClientFurnizorComprest
from .digi import ClientFurnizorDigi
from .eon import ClientFurnizorEon
from .ebloc import ClientFurnizorEbloc
from .engie import ClientFurnizorEngie
from .hidroelectrica import ClientFurnizorHidroelectrica
from .myelectrica import ClientFurnizorMyElectrica
from .deer import ClientFurnizorDeer
from .nova import ClientFurnizorNova
from .orange import ClientFurnizorOrange
from .rervest import ClientFurnizorRerVest

FURNIZORI: dict[str, type[ClientFurnizor]] = {
    ClientFurnizorComprest.cheie_furnizor: ClientFurnizorComprest,
    ClientFurnizorNova.cheie_furnizor: ClientFurnizorNova,
    ClientFurnizorDigi.cheie_furnizor: ClientFurnizorDigi,
    ClientFurnizorEon.cheie_furnizor: ClientFurnizorEon,
    ClientFurnizorApaCanal.cheie_furnizor: ClientFurnizorApaCanal,
    ClientFurnizorApaBrasov.cheie_furnizor: ClientFurnizorApaBrasov,
    ClientFurnizorApaOradea.cheie_furnizor: ClientFurnizorApaOradea,
    ClientFurnizorHidroelectrica.cheie_furnizor: ClientFurnizorHidroelectrica,
    ClientFurnizorMyElectrica.cheie_furnizor: ClientFurnizorMyElectrica,
    ClientFurnizorDeer.cheie_furnizor: ClientFurnizorDeer,
    ClientFurnizorEbloc.cheie_furnizor: ClientFurnizorEbloc,
    ClientFurnizorOrange.cheie_furnizor: ClientFurnizorOrange,
    ClientFurnizorRerVest.cheie_furnizor: ClientFurnizorRerVest,
    ClientFurnizorEngie.cheie_furnizor: ClientFurnizorEngie,
}


def obtine_clasa_furnizor(cheie_furnizor: str) -> type[ClientFurnizor]:
    return FURNIZORI[cheie_furnizor]
