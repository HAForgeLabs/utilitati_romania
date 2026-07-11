from __future__ import annotations

from .apa_canal import ClientFurnizorApaCanal
from .apa_brasov import ClientFurnizorApaBrasov
from .apa_oradea import ClientFurnizorApaOradea
from .apa_galati import ClientFurnizorApaGalati
from .aparegio import ClientFurnizorAparegio
from .baza import ClientFurnizor
from .comprest import ClientFurnizorComprest
from .digi import ClientFurnizorDigi
from .eon import ClientFurnizorEon
from .ebloc import ClientFurnizorEbloc
from .engie import ClientFurnizorEngie
from .hidroelectrica import ClientFurnizorHidroelectrica
from .hidro_prahova import ClientFurnizorHidroPrahova
from .myelectrica import ClientFurnizorMyElectrica
from .deer import ClientFurnizorDeer
from .deo import ClientFurnizorDeo
from .nova import ClientFurnizorNova
from .orange import ClientFurnizorOrange
from .polaris import ClientFurnizorPolaris
from .rervest import ClientFurnizorRerVest

FURNIZORI: dict[str, type[ClientFurnizor]] = {
    ClientFurnizorComprest.cheie_furnizor: ClientFurnizorComprest,
    ClientFurnizorNova.cheie_furnizor: ClientFurnizorNova,
    ClientFurnizorDigi.cheie_furnizor: ClientFurnizorDigi,
    ClientFurnizorEon.cheie_furnizor: ClientFurnizorEon,
    ClientFurnizorApaCanal.cheie_furnizor: ClientFurnizorApaCanal,
    ClientFurnizorApaBrasov.cheie_furnizor: ClientFurnizorApaBrasov,
    ClientFurnizorApaOradea.cheie_furnizor: ClientFurnizorApaOradea,
    ClientFurnizorApaGalati.cheie_furnizor: ClientFurnizorApaGalati,
    ClientFurnizorAparegio.cheie_furnizor: ClientFurnizorAparegio,
    ClientFurnizorHidroelectrica.cheie_furnizor: ClientFurnizorHidroelectrica,
    ClientFurnizorHidroPrahova.cheie_furnizor: ClientFurnizorHidroPrahova,
    ClientFurnizorMyElectrica.cheie_furnizor: ClientFurnizorMyElectrica,
    ClientFurnizorDeer.cheie_furnizor: ClientFurnizorDeer,
    ClientFurnizorDeo.cheie_furnizor: ClientFurnizorDeo,
    ClientFurnizorEbloc.cheie_furnizor: ClientFurnizorEbloc,
    ClientFurnizorOrange.cheie_furnizor: ClientFurnizorOrange,
    ClientFurnizorPolaris.cheie_furnizor: ClientFurnizorPolaris,
    ClientFurnizorRerVest.cheie_furnizor: ClientFurnizorRerVest,
    ClientFurnizorEngie.cheie_furnizor: ClientFurnizorEngie,
}


def obtine_clasa_furnizor(cheie_furnizor: str) -> type[ClientFurnizor]:
    return FURNIZORI[cheie_furnizor]
