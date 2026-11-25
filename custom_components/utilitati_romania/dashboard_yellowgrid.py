"""Surse sigure pentru panoul energetic YellowGrid din Home Assistant.

Panoul citește stările senzorilor deja publicați. Nu expunem date de
conectare, identificatori interni ai instalațiilor sau răspunsuri API brute.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import entity_registry as er

from .const import CONF_FURNIZOR, DOMENIU, FURNIZOR_YELLOWGRID


def descriere_panou_yellowgrid(hass: Any) -> dict[str, Any]:
    """Asociază fiecare cont cu senzorii săi folosind registrul de entități."""
    registru = er.async_get(hass)
    conturi_dashboard: list[dict[str, Any]] = []

    for intrare in hass.config_entries.async_entries(DOMENIU):
        if intrare.data.get(CONF_FURNIZOR) != FURNIZOR_YELLOWGRID:
            continue

        entitati = {
            element.unique_id: element.entity_id
            for element in er.async_entries_for_config_entry(registru, intrare.entry_id)
            if element.domain == "sensor" and element.unique_id and not element.disabled_by
        }
        prefix = f"{intrare.entry_id}_yellowgrid_"
        prefix_portofel = f"{prefix}portofel_"
        portofel = {
            identificator[len(prefix_portofel):]: entitate
            for identificator, entitate in entitati.items()
            if identificator.startswith(prefix_portofel)
        }

        coordonator = hass.data.get(DOMENIU, {}).get(intrare.entry_id)
        instantaneu = getattr(coordonator, "data", None)
        instalatii: list[dict[str, Any]] = []
        for planta in getattr(instantaneu, "conturi", []) or []:
            identificator = str(getattr(planta, "id_cont", "") or "")
            prefix_instalatie = f"{prefix}{identificator}_"
            senzori = {
                cheie[len(prefix_instalatie):]: entitate
                for cheie, entitate in entitati.items()
                if cheie.startswith(prefix_instalatie)
            }
            if not senzori:
                continue
            instalatii.append({
                "nume": str(getattr(planta, "nume", "Instalație fotovoltaică") or "Instalație fotovoltaică"),
                "senzori": senzori,
            })

        conturi_dashboard.append({
            "portofel": portofel,
            "instalatii": instalatii,
        })

    return {"conturi": conturi_dashboard}
