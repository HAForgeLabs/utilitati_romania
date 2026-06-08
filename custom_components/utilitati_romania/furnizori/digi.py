from __future__ import annotations

from datetime import date
from typing import Any

from ..const import (
    CONF_DIGI_COOKIES,
    CONF_DIGI_HISTORY_LIMIT,
    CONF_DIGI_SELECTED_ACCOUNT_ID,
    CONF_DIGI_SELECTED_ACCOUNT_LABEL,
    FURNIZOR_DIGI,
    IMPLICIT_DIGI_HISTORY_LIMIT,
)
from ..exceptions import EroareAutentificare, EroareConectare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor
from .digi_api import DigiApiClient, DigiAuthError, DigiError, DigiReauthRequired


def _parseaza_data(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip().replace(".", "-").replace("/", "-")
    parti = text.split("-")
    if len(parti) != 3:
        return None
    try:
        zi, luna, an = [int(p) for p in parti]
        return date(an, luna, zi)
    except ValueError:
        return None


def _normalizare_slug(text: str) -> str:
    value = (text or "").lower()
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
        value = value.replace(src, dst)
    rezultat = []
    for ch in value:
        rezultat.append(ch if ch.isalnum() else "_")
    slug = "".join(rezultat)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "cont"


def _service_label(value: str | None) -> str:
    text = str(value or "").strip()
    return text or "Servicii Digi"


def _service_slug(value: str | None) -> str:
    return _normalizare_slug(_service_label(value))


def _servicii_din_latest(latest: dict[str, Any]) -> list[dict[str, Any]]:
    servicii = latest.get("services") or []
    return servicii if isinstance(servicii, list) else []


def _numar_servicii_din_latest(latest: dict[str, Any]) -> int:
    servicii = _servicii_din_latest(latest)
    if servicii:
        return len(servicii)

    for key in ("numar_servicii", "services_count", "services_total"):
        val = latest.get(key)
        try:
            if val is not None:
                return int(val)
        except Exception:
            pass

    return 0


class ClientFurnizorDigi(ClientFurnizor):
    cheie_furnizor = FURNIZOR_DIGI
    nume_prietenos = "Digi România"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = DigiApiClient(sesiune)

    async def async_inchide(self) -> None:
        await self.api.close()

    def importa_cookies(self, cookies: list[dict[str, Any]] | None) -> None:
        self.api.import_cookies(cookies or [])

    def exporta_cookies(self) -> list[dict[str, Any]]:
        return self.api.export_cookies()

    async def async_testeaza_conexiunea(self) -> str:
        cookies = self.optiuni.get(CONF_DIGI_COOKIES) or []
        if not cookies:
            raise EroareAutentificare("Sesiunea Digi lipsește. Reconfigurează integrarea.")
        self.importa_cookies(cookies)
        try:
            await self.api.async_fetch_data(
                history_limit=int(self.optiuni.get(CONF_DIGI_HISTORY_LIMIT, IMPLICIT_DIGI_HISTORY_LIMIT))
            )
        except DigiReauthRequired as err:
            raise EroareAutentificare("Sesiunea Digi a expirat. Este necesară reautentificarea.") from err
        except DigiAuthError as err:
            raise EroareAutentificare(str(err)) from err
        except DigiError as err:
            raise EroareConectare(str(err)) from err
        return f"{self.utilizator.lower()}::{self.optiuni.get(CONF_DIGI_SELECTED_ACCOUNT_ID) or 'default'}"

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        cookies = self.optiuni.get(CONF_DIGI_COOKIES) or []
        if cookies:
            self.importa_cookies(cookies)

        try:
            digi_data = await self.api.async_fetch_data(
                history_limit=int(self.optiuni.get(CONF_DIGI_HISTORY_LIMIT, IMPLICIT_DIGI_HISTORY_LIMIT))
            )
        except DigiReauthRequired as err:
            raise EroareAutentificare("Sesiunea Digi a expirat. Este necesară reautentificarea.") from err
        except DigiAuthError as err:
            raise EroareAutentificare(str(err)) from err
        except DigiError as err:
            raise EroareConectare(str(err)) from err

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []

        total_sold = 0.0
        total_ultima_factura = 0.0
        total_numar_servicii = 0
        exista_restanta = False

        account_id = self.optiuni.get(CONF_DIGI_SELECTED_ACCOUNT_ID) or digi_data.account_id or "digi"
        account_label = self.optiuni.get(CONF_DIGI_SELECTED_ACCOUNT_LABEL) or digi_data.account_label or "Cont Digi"

        latest_global: dict[str, Any] | None = None
        latest_global_issue_date: date | None = None
        scadente_restante: list[date] = []

        adrese_unice: set[str] = set()

        for address_key, entry in digi_data.invoices_by_address.items():
            slug_adresa = _normalizare_slug(entry.address or address_key)
            adrese_unice.add(address_key or slug_adresa)

            grupuri_servicii: dict[str, list[dict[str, Any]]] = {}
            for item in entry.history or []:
                label_serviciu = _service_label(item.get("description"))
                grupuri_servicii.setdefault(label_serviciu, []).append(item)

            if not grupuri_servicii:
                latest = entry.latest or {}
                grupuri_servicii[_service_label(latest.get("description"))] = [latest]

            for label_serviciu, items_serviciu in grupuri_servicii.items():
                items_serviciu = [item for item in items_serviciu if item]
                if not items_serviciu:
                    continue

                items_serviciu.sort(key=lambda x: _parseaza_data(x.get("issue_date")) or date.min, reverse=True)
                latest = items_serviciu[0]
                slug_serviciu = _service_slug(label_serviciu)
                id_cont = f"digi_{slug_adresa}_{slug_serviciu}"

                facturi_neachitate = [
                    item for item in items_serviciu
                    if float(item.get("rest") or 0.0) > 0
                    or "neach" in str(item.get("status") or "").lower()
                ]

                rest = round(
                    sum(
                        max(float(item.get("rest") or item.get("amount") or 0.0), 0.0)
                        for item in facturi_neachitate
                    ),
                    2,
                )
                amount = float(latest.get("amount") or 0.0)
                issue_date = _parseaza_data(latest.get("issue_date"))
                numar_servicii = _numar_servicii_din_latest(latest)

                total_sold += max(rest, 0.0)
                total_ultima_factura += amount
                total_numar_servicii += numar_servicii
                exista_restanta = exista_restanta or rest > 0

                for item_neachitat in facturi_neachitate:
                    scadenta_item = _parseaza_data(item_neachitat.get("due_date"))
                    if scadenta_item:
                        scadente_restante.append(scadenta_item)

                if issue_date and (latest_global_issue_date is None or issue_date > latest_global_issue_date):
                    latest_global_issue_date = issue_date
                    latest_global = latest
                elif latest_global is None:
                    latest_global = latest

                conturi.append(
                    ContUtilitate(
                        id_cont=id_cont,
                        nume=entry.address,
                        tip_cont="servicii",
                        id_contract=str(account_id),
                        adresa=entry.address,
                        stare="restant" if rest > 0 else "activ",
                        tip_utilitate="telecom",
                        tip_serviciu=label_serviciu,
                        date_brute={
                            "address_key": address_key,
                            "account_id": account_id,
                            "account_label": account_label,
                            "service_label": label_serviciu,
                            "latest": latest,
                            "history": items_serviciu,
                            "unpaid_count": len(facturi_neachitate),
                        },
                    )
                )

                for idx, item in enumerate(items_serviciu):
                    factura_id = str(item.get("invoice_id") or f"{id_cont}_{idx}")
                    facturi.append(
                        FacturaUtilitate(
                            id_factura=factura_id,
                            titlu=str(item.get("description") or label_serviciu or f"Factură Digi {entry.address}"),
                            valoare=float(item.get("amount") or 0.0),
                            moneda="RON",
                            data_emitere=_parseaza_data(item.get("issue_date")),
                            data_scadenta=_parseaza_data(item.get("due_date")),
                            stare=item.get("status"),
                            categorie="factura",
                            id_cont=id_cont,
                            id_contract=str(account_id),
                            tip_utilitate="telecom",
                            tip_serviciu=label_serviciu,
                            date_brute=dict(item),
                        )
                    )

                consumuri.extend(
                    [
                        ConsumUtilitate("sold_curent", round(rest, 2), "RON", id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                        ConsumUtilitate("de_plata", round(rest, 2), "RON", id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                        ConsumUtilitate("valoare_ultima_factura", round(amount, 2), "RON", id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                        ConsumUtilitate("id_ultima_factura", latest.get("invoice_number") or latest.get("invoice_id"), None, id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                        ConsumUtilitate("urmatoarea_scadenta", latest.get("due_date"), None, id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                        ConsumUtilitate("factura_restanta", "da" if rest > 0 else "nu", None, id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                        ConsumUtilitate("sold_factura", round(rest, 2), "RON", id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                        ConsumUtilitate("numar_servicii", numar_servicii, None, id_cont=id_cont, tip_utilitate="telecom", tip_serviciu=label_serviciu),
                    ]
                )

        scadenta_generala = min(scadente_restante).isoformat() if scadente_restante else None
        id_ultima_factura_generala = None
        valoare_ultima_factura_generala = round(total_ultima_factura, 2)

        if latest_global:
            id_ultima_factura_generala = latest_global.get("invoice_number") or latest_global.get("invoice_id")
            valoare_ultima_factura_generala = round(float(latest_global.get("amount") or 0.0), 2)

        consumuri.extend(
            [
                ConsumUtilitate("sold_curent", round(total_sold, 2), "RON", tip_utilitate="telecom", tip_serviciu="servicii digi"),
                ConsumUtilitate("de_plata", round(total_sold, 2), "RON", tip_utilitate="telecom", tip_serviciu="servicii digi"),
                ConsumUtilitate("total_neachitat", round(total_sold, 2), "RON", tip_utilitate="telecom", tip_serviciu="servicii digi"),
                ConsumUtilitate("valoare_ultima_factura", valoare_ultima_factura_generala, "RON", tip_utilitate="telecom", tip_serviciu="servicii digi"),
                ConsumUtilitate("id_ultima_factura", id_ultima_factura_generala, None, tip_utilitate="telecom", tip_serviciu="servicii digi"),
                ConsumUtilitate("urmatoarea_scadenta", scadenta_generala, None, tip_utilitate="telecom", tip_serviciu="servicii digi"),
                ConsumUtilitate("factura_restanta", "da" if exista_restanta else "nu", None, tip_utilitate="telecom", tip_serviciu="servicii digi"),
                ConsumUtilitate("numar_servicii", total_numar_servicii, None, tip_utilitate="telecom", tip_serviciu="servicii digi"),
            ]
        )

        return InstantaneuFurnizor(
            furnizor=FURNIZOR_DIGI,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={
                "account_id": account_id,
                "account_label": account_label,
                "needs_reauth": digi_data.needs_reauth,
                "last_update": digi_data.last_update.isoformat() if digi_data.last_update else None,
                "addresses_count": len(adrese_unice),
            },
        )
