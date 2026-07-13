from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_API = "https://aquatim-api.deventure.co/api"
URL_PORTAL = "https://self.aquatim.ro/"
DEVICE_ID = "utilitati-romania-home-assistant"


class EroareApiAquatim(Exception):
    pass


class EroareAutentificareAquatim(EroareApiAquatim):
    pass


class EroareConectareAquatim(EroareApiAquatim):
    pass


class ClientApiAquatim:
    def __init__(self, sesiune: aiohttp.ClientSession, email: str, parola: str) -> None:
        self._sesiune = sesiune
        self._email = email
        self._parola = parola
        self._token: str | None = None
        self._user_id: str | None = None

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "Dalvik/2.1.0"}
        if json_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _request(self, method: str, endpoint: str, *, params=None, json_data=None, auth=True):
        if auth and not self._token:
            await self.async_login()
        try:
            async with self._sesiune.request(
                method,
                f"{URL_API}{endpoint}",
                params=params,
                json=json_data,
                headers=self._headers(json_body=json_data is not None),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status in (401, 403):
                    if auth and self._token:
                        self._token = None
                        return await self._request(method, endpoint, params=params, json_data=json_data, auth=auth)
                    raise EroareAutentificareAquatim("Credentialele Aquatim nu au fost acceptate")
                if response.status >= 400:
                    raise EroareApiAquatim(f"Aquatim HTTP {response.status} pentru {endpoint}")
                if isinstance(payload, dict) and payload.get("Success") is False:
                    if payload.get("StatusCode") == 16:
                        raise EroareAutentificareAquatim("Credentialele Aquatim nu au fost acceptate")
                    raise EroareApiAquatim(f"Aquatim a returnat StatusCode={payload.get('StatusCode')}")
                return payload
        except EroareApiAquatim:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EroareConectareAquatim(f"Eroare de conectare la Aquatim: {err}") from err
        except Exception as err:
            raise EroareApiAquatim(f"Răspuns Aquatim invalid: {err}") from err

    async def async_login(self) -> dict[str, Any]:
        payload = await self._request("POST", "/Account/Login", auth=False, json_data={
            "u": self._email, "p": self._parola, "d": 0, "c": "5.5", "a": 1, "io": None, "di": DEVICE_ID,
        })
        data = payload.get("Data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("access_token"):
            raise EroareAutentificareAquatim("Login Aquatim invalid")
        self._token = str(data["access_token"])
        raw_user = data.get("userData")
        if isinstance(raw_user, str):
            import json
            try:
                user = json.loads(raw_user)
                self._user_id = str(user.get("i") or "") or None
            except ValueError:
                pass
        return data

    async def async_get_all_data(self) -> dict[str, Any]:
        login = await self.async_login()
        if not self._user_id:
            raise EroareApiAquatim("Aquatim nu a furnizat ID-ul utilizatorului")
        points_payload = await self._request("GET", "/CustomerConsumptionPoint/GetList", params={"userId": self._user_id})
        points = _data_list(points_payload)
        all_meters: dict[str, list[dict[str, Any]]] = {}
        all_history: dict[str, list[dict[str, Any]]] = {}
        all_invoices: dict[str, list[dict[str, Any]]] = {}
        all_payments: dict[str, list[dict[str, Any]]] = {}
        for point in points:
            code = str(point.get("cc") or "")
            if not code:
                continue
            meters_payload = await self._request(
                "POST",
                "/Customer/MetersForCustomer",
                params={"clientCode": code},
            )
            meters = _data_list(meters_payload)

            all_meters[code] = meters
            all_invoices[code] = _data_list(await self._request("POST", "/CustomerPayment/GetInvoicesHistory", json_data={"clientCode": code, "cs": None}))
            all_payments[code] = _data_list(await self._request("POST", "/CustomerPayment/GetPaymentHistory", json_data={"clientCode": code, "cs": None}))
            history: list[dict[str, Any]] = []
            for meter in meters:
                series = str(meter.get("n") or "")
                if series:
                    history.extend(_data_list(await self._request("POST", "/CustomerIndex/GetHistory", json_data={"clientCode": code, "cs": series})))
            all_history[code] = history
        return {"login": login, "points": points, "meters": all_meters, "history": all_history, "invoices": all_invoices, "payments": all_payments}


class ClientFurnizorAquatim(ClientFurnizor):
    cheie_furnizor = "aquatim"
    nume_prietenos = "Aquatim"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiAquatim(sesiune, utilizator, parola)

    async def async_testeaza_conexiunea(self) -> str:
        try:
            data = await self.api.async_get_all_data()
            points = data.get("points") or []
            return str((points[0].get("cc") if points else None) or self.utilizator)
        except EroareAutentificareAquatim as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareAquatim as err:
            raise EroareConectare(str(err)) from err
        except EroareApiAquatim as err:
            raise EroareParsare(str(err)) from err

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            raw = await self.api.async_get_all_data()
        except EroareAutentificareAquatim as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareAquatim as err:
            raise EroareConectare(str(err)) from err
        except EroareApiAquatim as err:
            raise EroareParsare(str(err)) from err
        conturi = self._map_accounts(raw)
        facturi = self._map_invoices(raw)
        consumuri = self._map_consumption(raw, conturi, facturi)
        return InstantaneuFurnizor("aquatim", "Aquatim", conturi, facturi, consumuri, {"portal_url": URL_PORTAL, "perioada_transmitere_index": "20-25 ale lunii"})

    def _map_accounts(self, raw):
        result=[]
        for p in raw.get("points", []):
            code=str(p.get("cc") or "").strip()
            if not code: continue
            meters=raw.get("meters", {}).get(code, [])
            result.append(ContUtilitate(id_cont=code, nume=str(p.get("n") or f"Client {code}"), tip_cont="apa", id_contract=str(p.get("nc") or "") or None, tip_utilitate="apa", tip_serviciu="apa_canal", date_brute={"punct_consum_id":p.get("i"),"cod_client":code,"numar_contract":p.get("nc"),"contoare":meters,"numar_contoare":len(meters),"payments":raw.get("payments", {}).get(code, [])}))
        return result

    def _map_invoices(self, raw):
        result=[]
        for code, items in raw.get("invoices", {}).items():
            for item in items:
                number=str(item.get("InvoiceNumber") or "").strip()
                if not number: continue
                due=_float(item.get("AmountDue")) or 0.0
                result.append(FacturaUtilitate(number, str(item.get("Name") or f"Factura {number}"), _float(item.get("InvoiceValue")), "RON", _date(item.get("StartDate")), _date(item.get("EndDate")), "platita" if due <= 0 or item.get("Status")==2 else "neplatita", "consum", code, None, "apa", "apa_canal", False, {**item,"rest_plata":due,"pdf_url":item.get("DownloadUrl") or item.get("Url")}))
        result.sort(key=lambda x:x.data_emitere or date.min, reverse=True)
        return result

    def _map_consumption(self, raw, accounts, invoices):
        out=[]
        today=date.today(); is_open=20 <= today.day <= 25
        for account in accounts:
            code=account.id_cont
            inv=[i for i in invoices if i.id_cont==code]
            history=raw.get("history",{}).get(code,[])
            latest=history[0] if history else {}
            due=sum(float(i.date_brute.get("rest_plata") or 0) for i in inv)
            last=inv[0] if inv else None
            payments=raw.get("payments",{}).get(code,[])
            payments.sort(key=lambda item: _date(item.get("Date")) or date.min, reverse=True)
            latest_payment=payments[0] if payments else None
            values=[
                ("de_plata",due,"RON",None),("sold_curent",due,"RON",None),("numar_facturi",len(inv),None,None),("numar_facturi_neachitate",sum(1 for i in inv if i.stare!="platita"),None,None),("factura_restanta","da" if due>0 else "nu",None,None),("valoare_ultima_factura",last.valoare if last else None,"RON",last.data_emitere.isoformat() if last and last.data_emitere else None),("id_ultima_factura",last.id_factura if last else None,None,None),("data_ultima_factura",last.data_emitere.isoformat() if last and last.data_emitere else None,None,None),("urmatoarea_scadenta",last.data_scadenta.isoformat() if last and last.data_scadenta else None,None,None),("numar_plati",len(payments),None,None),("data_ultima_plata",(_date(latest_payment.get("Date")).isoformat() if latest_payment and _date(latest_payment.get("Date")) else None),None,None),("valoare_ultima_plata",_float(latest_payment.get("Value")) if latest_payment else None,"RON",None),("numar_contoare",len(raw.get("meters",{}).get(code,[])),None,None),("index_contor",_float(latest.get("NewIndex")),"m³",latest.get("Period")),("ultim_index",_float(latest.get("NewIndex")),"m³",latest.get("Period")),("ultim_consum",_float(latest.get("Quantity")),"m³",latest.get("Period")),("consum_lunar",_float(latest.get("Quantity")),"m³",latest.get("Period")),("citire_index_permisa","da" if is_open else "nu",None,None),("perioada_citire","20-25 ale lunii",None,None),("zile_pana_citire_index",_days_to_window(today),"zile",None),
            ]
            for key,val,unit,period in values:
                out.append(ConsumUtilitate(key,val,unit,period,code,"apa","apa_canal",latest if key in {"index_contor","ultim_index","ultim_consum","consum_lunar"} else (latest_payment or {}) if key in {"numar_plati","data_ultima_plata","valoare_ultima_plata"} else {}))
        return out


def _data_list(payload):
    return [x for x in (payload.get("Data") if isinstance(payload,dict) else []) or [] if isinstance(x,dict)]

def _float(value):
    try: return float(str(value).replace(",",".")) if value not in (None,"") else None
    except ValueError: return None

def _date(value):
    if not value: return None
    for fmt in ("%d.%m.%Y","%d-%m-%Y","%Y-%m-%d"):
        try: return datetime.strptime(str(value),fmt).date()
        except ValueError: pass
    return None

def _days_to_window(today):
    if 20 <= today.day <= 25: return 0
    if today.day < 20: return 20-today.day
    import calendar
    days=calendar.monthrange(today.year,today.month)[1]
    return days-today.day+20
