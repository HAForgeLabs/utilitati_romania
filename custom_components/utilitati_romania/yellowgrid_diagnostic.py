"""Diagnostic local, fara date de acces, pentru configurarile YellowGrid."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)
_FILENAME = "utilitati_romania_yellowgrid_diagnostic.json"


def _write_report(filename: str, report: dict[str, Any]) -> None:
    """Scrie atomic ultimul rezultat, fara a pastra credentiale sau date brute."""
    target = Path(filename)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".yellowgrid_diagnostic_",
            suffix=".tmp", dir=target.parent, delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(report, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp_name, target)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_trace(client: Any) -> list[dict[str, Any]]:
    """Copiaza numai campurile explicit permise din traseul clientului API."""
    if client is None:
        return []
    values = getattr(client, "istoric_diagnostic", ())
    if not isinstance(values, (tuple, list)):
        return []
    result: list[dict[str, Any]] = []
    for event in values[-16:]:
        if not isinstance(event, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("etapa", "metoda", "http", "durata_ms", "rezultat"):
            value = event.get(key)
            if key in {"etapa", "metoda", "rezultat"} and isinstance(value, str):
                if value in {
                    "login", "refresh", "instalatii", "telemetrie", "fluxuri",
                    "castiguri", "portofel", "api", "configurare_client", "GET", "POST", "ok",
                    "http_error", "network_error", "timeout", "json_invalid",
                    "response_too_large", "config_invalid",
                }:
                    item[key] = value
            elif key == "http" and type(value) is int and 100 <= value <= 599:
                item[key] = value
            elif key == "durata_ms" and type(value) is int and value >= 0:
                item[key] = value
        if item:
            result.append(item)
    return result


async def async_salvare_diagnostic_yellowgrid(
    hass: Any, *, operatie: str, rezultat: str, etapa: str,
    client: Any = None, eroare: BaseException | None = None,
    cod_tara: str = "004",
) -> str:
    """Salveaza ultimul test si inregistreaza sumarul in jurnalul HA."""
    identifier = uuid4().hex[:10]
    requests = _safe_trace(client)
    last = requests[-1] if requests else {}
    report = {
        "versiune_diagnostic": 1,
        "id_test": identifier,
        "ora_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "operatie": operatie if operatie in {"configurare", "reautentificare", "optiuni"} else "configurare",
        "rezultat": rezultat if rezultat in {"ok", "eroare"} else "eroare",
        "etapa": etapa if etapa in {"validare_telefon", "licenta", "api", "finalizare"} else "api",
        "cod_tara_transmis": cod_tara if cod_tara == "004" else "alt_cod",
        "format_mobileNumber": "07xxxxxxxx",
        "tip_eroare": type(eroare).__name__ if eroare is not None else None,
        "ultima_cerere": last,
        "cereri_api": requests,
        "protectie_date": "Fara telefon, parola, x-apikey, JWT, URL-uri cu identificatori sau raspunsuri brute",
    }
    path = hass.config.path(_FILENAME)
    try:
        await hass.async_add_executor_job(_write_report, path, report)
    except (OSError, TypeError, ValueError):
        _LOGGER.warning("YellowGrid diagnostic [%s]: fisierul de raport nu a putut fi scris", identifier)
    final_stage = last.get("etapa", report["etapa"])
    http_status = last.get("http", "-")
    if report["rezultat"] == "eroare":
        _LOGGER.warning(
            "YellowGrid diagnostic [%s]: %s; etapa=%s; http=%s; eroare=%s; fisier=%s",
            identifier, report["rezultat"], final_stage, http_status,
            report["tip_eroare"] or "necunoscuta", _FILENAME,
        )
    else:
        _LOGGER.info(
            "YellowGrid diagnostic [%s]: ok; etapa=%s; http=%s; fisier=%s",
            identifier, final_stage, http_status, _FILENAME,
        )
    return identifier
