from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_SECURITY_APP = "https://security-bo.apanovabucuresti.ro"
URL_SECURITY_CLIENT = "https://security-client.apanovabucuresti.ro"
URL_CLIENT_AUTH = "https://client-authorization.apanovabucuresti.ro"
URL_GATEWAY = "https://callistogateway.apanovabucuresti.ro"
URL_PORTAL = "https://www.apanovabucuresti.ro"

APP_USER = "app.prod1"
APP_PASSWORD = "F9#$|]nY*G67]2Mo"
USER_AGENT = "okhttp/4.12.0"
TIMEOUT = aiohttp.ClientTimeout(total=35)


class EroareApiApaNovaBucuresti(Exception):
    pass


class EroareAutentificareApaNovaBucuresti(EroareApiApaNovaBucuresti):
    pass


class EroareConectareApaNovaBucuresti(EroareApiApaNovaBucuresti):
    pass


class EroareRaspunsApaNovaBucuresti(EroareApiApaNovaBucuresti):
    pass


class ClientApiApaNovaBucuresti:
    def __init__(self, sesiune: aiohttp.ClientSession, utilizator: str, parola: str) -> None:
        self._sesiune = sesiune
        self._utilizator = utilizator.strip()
        self._parola = parola
        self._app_token: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_id: str | None = None

    @staticmethod
    def _headers(token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["X-AUTH-TOKEN"] = token
        return headers

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
        autentificare: bool = False,
    ) -> Any:
        try:
            async with self._sesiune.request(
                method,
                url,
                headers=self._headers(token),
                json=json_body,
                timeout=TIMEOUT,
            ) as raspuns:
                try:
                    continut = await raspuns.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    text = (await raspuns.text())[:300]
                    raise EroareRaspunsApaNovaBucuresti(
                        f"Apa Nova București a returnat un răspuns invalid pentru {url}: {text}"
                    ) from err

                if raspuns.status in (401, 403):
                    raise EroareAutentificareApaNovaBucuresti(
                        "Sesiunea Apa Nova București nu este validă"
                    )
                if raspuns.status >= 400:
                    mesaj = ""
                    if isinstance(continut, dict):
                        mesaj = str(
                            continut.get("errorMessage")
                            or continut.get("message")
                            or continut.get("Message")
                            or ""
                        ).strip()
                    if autentificare:
                        raise EroareAutentificareApaNovaBucuresti(
                            mesaj or "Credentialele Apa Nova București nu au fost acceptate"
                        )
                    raise EroareRaspunsApaNovaBucuresti(
                        f"Apa Nova București a returnat HTTP {raspuns.status}"
                        + (f": {mesaj}" if mesaj else "")
                    )
                return continut
        except EroareApiApaNovaBucuresti:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareApaNovaBucuresti(
                f"Eroare de conectare la Apa Nova București: {err}"
            ) from err

    async def async_login(self) -> dict[str, Any]:
        try:
            app_login = await self._request_json(
                "POST",
                f"{URL_SECURITY_APP}/api/Login",
                json_body={"userMail": APP_USER, "password": APP_PASSWORD},
                autentificare=True,
            )
        except EroareApiApaNovaBucuresti:
            app_login = await self._request_json(
                "POST",
                f"{URL_SECURITY_CLIENT}/api/Login",
                json_body={"userMail": APP_USER, "password": APP_PASSWORD},
                autentificare=True,
            )
        if not isinstance(app_login, dict) or not app_login.get("accessToken"):
            raise EroareRaspunsApaNovaBucuresti(
                "Apa Nova București nu a furnizat tokenul tehnic al aplicației"
            )
        self._app_token = str(app_login["accessToken"])

        user_login = await self._request_json(
            "POST",
            f"{URL_SECURITY_CLIENT}/api/Login",
            token=self._app_token,
            json_body={"userMail": self._utilizator, "password": self._parola},
            autentificare=True,
        )
        if not isinstance(user_login, dict) or not user_login.get("accessToken"):
            raise EroareAutentificareApaNovaBucuresti(
                "Credentialele Apa Nova București nu au fost acceptate"
            )

        self._access_token = str(user_login["accessToken"])
        self._refresh_token = str(user_login.get("refreshToken") or "") or None
        self._user_id = str(user_login.get("userId") or "") or None
        if not self._user_id:
            raise EroareRaspunsApaNovaBucuresti(
                "Apa Nova București nu a furnizat identificatorul utilizatorului"
            )
        return user_login

    async def _ensure_login(self) -> None:
        if not self._access_token or not self._user_id:
            await self.async_login()

    async def async_profile(self) -> dict[str, Any]:
        await self._ensure_login()
        rezultat = await self._request_json(
            "GET",
            f"{URL_CLIENT_AUTH}/api/User/{quote(self._user_id or '', safe='')}",
            token=self._access_token,
        )
        return rezultat if isinstance(rezultat, dict) else {}

    async def async_client_codes(self) -> list[str]:
        await self._ensure_login()
        rezultat = await self._request_json(
            "GET",
            f"{URL_CLIENT_AUTH}/api/ClientAuthorization/GetCodClientListByToken?token={quote(self._access_token or '', safe='')}",
            token=self._access_token,
        )
        if not isinstance(rezultat, list):
            raise EroareRaspunsApaNovaBucuresti(
                "Lista codurilor de client Apa Nova București are un format necunoscut"
            )
        return [str(item).lstrip("0") or "0" for item in rezultat if str(item).strip()]

    async def async_gateway(self, path: str) -> dict[str, Any]:
        await self._ensure_login()
        rezultat = await self._request_json(
            "GET",
            f"{URL_GATEWAY}{path}",
            token=self._access_token,
        )
        if not isinstance(rezultat, dict):
            return {}
        if rezultat.get("statusCode") not in (None, 200):
            raise EroareRaspunsApaNovaBucuresti(
                str(rezultat.get("errorMessage") or "Răspuns invalid Apa Nova București")
            )
        continut = rezultat.get("content")
        return continut if isinstance(continut, dict) else {}

    async def async_all_data(self) -> dict[str, Any]:
        login = await self.async_login()
        profile = await self.async_profile()
        client_codes = await self.async_client_codes()
        if not client_codes:
            payload = ((profile.get("userData") or {}).get("Payload") or {}) if isinstance(profile, dict) else {}
            fallback = str(payload.get("clientNumber") or "").lstrip("0")
            if fallback:
                client_codes = [fallback]

        date_from = (date.today() - timedelta(days=550)).isoformat()
        date_to = date.today().isoformat()
        clients: list[dict[str, Any]] = []
        for code in client_codes:
            encoded = quote(code, safe="")
            client: dict[str, Any] = {"client_code": code}
            calls = {
                "consumption_points": f"/api/v2/apiclientconsumptionpoint/{encoded}",
                "balance": f"/api/v2/apiclientsold/{encoded}",
                "unpaid_invoices": f"/api/v2/apiclientunpaidinvoices?clientNumber={encoded}",
                "invoices": (
                    f"/api/v2/apiclientinvoices?clientNumber={encoded}"
                    f"&dateFrom={date_from}&dateTo={date_to}"
                ),
                "meter_reading": f"/api/v2/apiclientcheckmeterautoreading/{encoded}",
                "index_history": (
                    f"/api/v2/apiclientindexhistory?clientNumber={encoded}"
                    f"&dateFrom={date_from}&dateTo={date_to}"
                ),
                "payments": f"/api/v2/apiclientpayments/{encoded}",
            }
            for key, path in calls.items():
                try:
                    client[key] = await self.async_gateway(path)
                except EroareApiApaNovaBucuresti:
                    if key in {"consumption_points", "balance", "invoices"}:
                        raise
                    _LOGGER.debug(
                        "Nu s-au putut încărca datele opționale Apa Nova București: %s",
                        key,
                        exc_info=True,
                    )
                    client[key] = {}
            clients.append(client)

        return {
            "login": login,
            "profile": profile,
            "clients": clients,
        }


class ClientFurnizorApaNovaBucuresti(ClientFurnizor):
    cheie_furnizor = "apa_nova_bucuresti"
    nume_prietenos = "Apa Nova București"

    async def async_testeaza_conexiunea(self) -> str:
        api = ClientApiApaNovaBucuresti(self.sesiune, self.utilizator, self.parola)
        try:
            await api.async_login()
            codes = await api.async_client_codes()
        except EroareAutentificareApaNovaBucuresti as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaNovaBucuresti as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaNovaBucuresti as err:
            raise EroareParsare(str(err)) from err
        return codes[0] if codes else self.utilizator.lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        api = ClientApiApaNovaBucuresti(self.sesiune, self.utilizator, self.parola)
        try:
            raw = await api.async_all_data()
        except EroareAutentificareApaNovaBucuresti as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareApaNovaBucuresti as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsApaNovaBucuresti as err:
            raise EroareParsare(str(err)) from err

        conturi, cont_by_client = self._map_accounts(raw)
        facturi = self._map_invoices(raw, cont_by_client)
        consumuri = self._map_consumptions(raw, conturi, cont_by_client, facturi)
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={
                "portal_url": URL_PORTAL,
                "numar_coduri_client": len(raw.get("clients") or []),
                "numar_facturi": len(facturi),
            },
        )

    @staticmethod
    def _map_accounts(raw: dict[str, Any]) -> tuple[list[ContUtilitate], dict[str, list[str]]]:
        conturi: list[ContUtilitate] = []
        by_client: dict[str, list[str]] = {}
        for client in raw.get("clients") or []:
            code = str(client.get("client_code") or "").strip()
            points = (client.get("consumption_points") or {}).get("ConsumptionPointInfo") or []
            active_points = [p for p in points if p.get("ConsumptionMeters")]
            selected = active_points or points
            if not selected:
                selected = [{
                    "ConsumptionPointCode": code,
                    "ConsumptionInstallation": code,
                    "ConsumptionClientAddress": f"Cod client {code}",
                    "ConsumptionMeters": [],
                }]
            by_client[code] = []
            for point in selected:
                point_code = str(point.get("ConsumptionPointCode") or "").strip()
                installation = str(point.get("ConsumptionInstallation") or "").strip()
                account_id = point_code or installation or code
                if not account_id or account_id in by_client[code]:
                    continue
                by_client[code].append(account_id)
                address = str(point.get("ConsumptionClientAddress") or "").strip()
                meters = [str(v) for v in (point.get("ConsumptionMeters") or []) if str(v).strip()]
                conturi.append(
                    ContUtilitate(
                        id_cont=account_id,
                        nume=address or f"Cod client {code}",
                        tip_cont="loc_consum",
                        id_contract=installation or None,
                        adresa=address or None,
                        stare="activ" if meters else "fără contor",
                        tip_utilitate="apa",
                        tip_serviciu="apa_canal",
                        date_brute={
                            "cod_client": code,
                            "punct_consum": point_code,
                            "numar_instalatie": installation,
                            "contoare": meters,
                            "numar_contoare": len(meters),
                        },
                    )
                )
        return conturi, by_client

    @staticmethod
    def _map_invoices(raw: dict[str, Any], by_client: dict[str, list[str]]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        seen: set[str] = set()
        for client in raw.get("clients") or []:
            code = str(client.get("client_code") or "").strip()
            account_id = (by_client.get(code) or [code])[0]
            invoices = (client.get("invoices") or {}).get("Invoices") or []
            for inv in invoices:
                invoice_id = str(inv.get("InvoiceNumber") or "").strip()
                if not invoice_id or invoice_id in seen:
                    continue
                seen.add(invoice_id)
                sold = _float(inv.get("Sold"))
                status = str(inv.get("SapStatus") or "").strip().lower()
                unpaid = sold > 0.005 or status in {"neachitata", "neachitată"}
                facturi.append(
                    FacturaUtilitate(
                        id_factura=invoice_id,
                        titlu=f"Factura {invoice_id}",
                        valoare=_float_or_none(inv.get("Total")),
                        moneda="RON",
                        data_emitere=_date(inv.get("DateIn")),
                        data_scadenta=_date(inv.get("DueDate")),
                        stare="neachitata" if unpaid else "achitata",
                        categorie="apa",
                        id_cont=account_id,
                        id_contract=None,
                        tip_utilitate="apa",
                        tip_serviciu="apa_canal",
                        date_brute={**dict(inv), "cod_client": code, "sold_factura": sold},
                    )
                )
        facturi.sort(key=lambda item: item.data_emitere or date.min, reverse=True)
        return facturi

    @staticmethod
    def _map_consumptions(
        raw: dict[str, Any],
        conturi: list[ContUtilitate],
        by_client: dict[str, list[str]],
        facturi: list[FacturaUtilitate],
    ) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        accounts = {cont.id_cont: cont for cont in conturi}
        for client in raw.get("clients") or []:
            code = str(client.get("client_code") or "").strip()
            ids = by_client.get(code) or []
            total_balance = _float((client.get("balance") or {}).get("Sold"))
            client_invoices = [f for f in facturi if (f.date_brute or {}).get("cod_client") == code]
            unpaid = [f for f in client_invoices if f.stare == "neachitata"]
            latest = client_invoices[0] if client_invoices else None
            latest_due = latest.data_scadenta if latest else None
            payments = (client.get("payments") or {}).get("Payments") or []
            valid_payments = [item for item in payments if _date(item.get("PaymentDate"))]
            latest_payment_date = max(
                (_date(item.get("PaymentDate")) for item in valid_payments),
                default=None,
            )
            latest_payment_rows = [
                item for item in valid_payments
                if _date(item.get("PaymentDate")) == latest_payment_date
            ]
            latest_payment_total = (
                round(sum(_float(item.get("PaidAmount")) for item in latest_payment_rows), 2)
                if latest_payment_rows
                else None
            )
            latest_payment_types = sorted({
                str(item.get("PaymentType") or "").strip()
                for item in latest_payment_rows
                if str(item.get("PaymentType") or "").strip()
            })
            latest_payment_documents = [
                str(item.get("DocumentNumber") or "").strip()
                for item in latest_payment_rows
                if str(item.get("DocumentNumber") or "").strip()
            ]

            meter_details = (client.get("meter_reading") or {}).get("MeterReadingDetails") or []
            history_points = (client.get("index_history") or {}).get("ConsumptionPoints") or []
            details_by_point = {
                str(item.get("ConsumptionPointIdentifier") or ""): item for item in meter_details
            }
            history_by_install = {
                str(item.get("InstallationNumber") or ""): item for item in history_points
            }

            for account_id in ids:
                account = accounts.get(account_id)
                if account is None:
                    continue
                account_raw = account.date_brute
                point = str(account_raw.get("punct_consum") or "")
                installation = str(account_raw.get("numar_instalatie") or "")
                meter = details_by_point.get(point) or {}
                history = history_by_install.get(installation) or {}
                meter_histories = history.get("IndexHistoryByMeter") or []
                readings: list[dict[str, Any]] = []
                for meter_history in meter_histories:
                    readings.extend(meter_history.get("MeterIndexList") or [])
                readings.sort(key=lambda item: str(item.get("EndDate") or ""), reverse=True)
                last_consumption = _float_or_none(readings[0].get("Consumption")) if readings else None
                next_read = _date(history.get("NextReadDate"))
                is_smart = bool(meter.get("IsSmart"))
                current_index = _float_or_none(meter.get("LastIndex"))

                values: dict[str, tuple[Any, str | None, dict[str, Any]]] = {
                    "de_plata": (total_balance, "RON", {}),
                    "valoare_ultima_factura": (latest.valoare if latest else None, "RON", {}),
                    "id_ultima_factura": (latest.id_factura if latest else None, None, {}),
                    "data_ultima_factura": (
                        latest.data_emitere.isoformat() if latest and latest.data_emitere else None,
                        None,
                        {},
                    ),
                    "urmatoarea_scadenta": (latest_due.isoformat() if latest_due else None, None, {}),
                    "factura_restanta": (bool(unpaid), None, {}),
                    "numar_facturi": (len(client_invoices), None, {}),
                    "numar_facturi_neachitate": (len(unpaid), None, {}),
                    "numar_plati": (len(payments), None, {}),
                    "data_ultima_plata": (
                        latest_payment_date.isoformat() if latest_payment_date else None,
                        None,
                        {},
                    ),
                    "valoare_ultima_plata": (latest_payment_total, "RON", {
                        "metode_plata": latest_payment_types,
                        "documente_plata": latest_payment_documents,
                        "numar_documente_ultima_plata": len(latest_payment_rows),
                    }),
                    "numar_contoare": (len(account_raw.get("contoare") or []), None, {}),
                    "index_contor": (current_index, "m³", {
                        "data_index": meter.get("LastIndexDate"),
                        "serie_contor": meter.get("Sernr"),
                        "contor_inteligent": is_smart,
                    }),
                    "ultim_consum": (last_consumption, "m³", {
                        "istoric": readings[:12],
                    }),
                    "data_ultima_citire": (meter.get("LastIndexDate"), None, {}),
                    "urmatoarea_citire": (next_read.isoformat() if next_read else None, None, {}),
                    "citire_index_permisa": (False if is_smart else None, None, {}),
                    "contor_inteligent": (is_smart, None, {}),
                    "serie_contor": (meter.get("Sernr") or ((account_raw.get("contoare") or [None])[0]), None, {}),
                }
                for key, (value, unit, extra) in values.items():
                    if value is None:
                        continue
                    consumuri.append(
                        ConsumUtilitate(
                            cheie=key,
                            valoare=value,
                            unitate=unit,
                            id_cont=account_id,
                            tip_utilitate="apa",
                            tip_serviciu="apa_canal",
                            date_brute={"cod_client": code, **extra},
                        )
                    )
        return consumuri



def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0



def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None



def _date(value: Any) -> date | None:
    if not value or str(value).startswith("0000-"):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None
