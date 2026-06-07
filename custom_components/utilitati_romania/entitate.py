from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FURNIZOR, CONF_PREMISE_LABEL, DOMENIU
from .furnizori.apa_brasov import nume_scurt_locatie_apa_brasov
from .coordonator import CoordonatorUtilitatiRomania


class EntitateUtilitatiRomania(CoordinatorEntity[CoordonatorUtilitatiRomania]):
    _attr_has_entity_name = True

    def __init__(self, coordonator: CoordonatorUtilitatiRomania) -> None:
        super().__init__(coordonator)
        if coordonator.intrare.data.get(CONF_FURNIZOR) == "apa_brasov":
            # Dispozitivul de pe config entry este doar dispozitivul de control/rezumat al
            # furnizorului. Locațiile reale au propriile dispozitive, definite separat
            # în platformele de senzori. Nu legăm acest device de adresa primei locații,
            # altfel Home Assistant ajunge să redenumească grupul furnizorului și să
            # afișeze duplicate confuze.
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMENIU, coordonator.intrare.entry_id)},
                name="Apă Brașov",
                manufacturer="Compania Apa Brașov",
                model="Apă și canal",
            )
            return

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMENIU, coordonator.intrare.entry_id)},
            name=coordonator.intrare.title,
            manufacturer="onitium",
            model=coordonator.cheie_furnizor,
        )
