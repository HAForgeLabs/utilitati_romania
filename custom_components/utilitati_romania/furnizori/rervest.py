from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any
from urllib.parse import urljoin

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://cp.rervest.ro"
URL_API = f"{URL_BAZA}/api"
ORIGIN = "https://rervest.ro"

ENDPOINT_LOGIN = "/auth/login"
ENDPOINT_USER = "/user"
ENDPOINT_FACTURI = "/invoices"
ENDPOINT_CLIENTI = "/customers"
ENDPOINT_SINCRONIZARE_CLIENT = "/customer/sync"
ENDPOINT_PLATI_CARD = "/card-payments"
ENDPOINT_PLATI = "/payments"
ENDPOINT_CERERI_ACTUALIZARE = "/update-requests"
ENDPOINT_COMENZI = "/orders"
ENDPOINT_PRODUSE = "/products"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


class EroareApiRerVest(Exception):
    pass


class EroareAutentificareRerVest(EroareApiRerVest):
    pass


class EroareConectareRerVest(EroareApiRerVest):
    pass


class EroareRaspunsRerVest(EroareApiRerVest):
    pass


class ClientApiRerVest:
    def __init__(self, sesiune: aiohttp.ClientSession, email: str, parola: str) -> None:
        self._sesiune = sesiune
        self._email = email
        self._parola = parola
        self._token: str | None = None
        self._autentificat = False

    def _url(self, endpoint: str) -> str:
        return f"{URL_API}{endpoint}"

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "User-Agent": USER_AGENT,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _request(
        self,
        metoda: str,
        endpoint: str,
        *,
        autentificat: bool = True,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        if autentificat and not self._autentificat:
            await self.async_login()

        try:
            async with self._sesiune.request(
                metoda,
                self._url(endpoint),
                headers=self._headers(json_body=json_data is not None),
                json=json_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403, 419):
                    if autentificat and self._autentificat:
                        self._autentificat = False
                        if self._token:
                            self._token = None
                        return await self._request(metoda, endpoint, autentificat=autentificat, json_data=json_data)
                    raise EroareAutentificareRerVest(f"Autentificare RER Vest eșuată pentru {endpoint}: HTTP {raspuns.status}")
                if raspuns.status >= 400:
                    raise EroareApiRerVest(f"RER Vest a returnat HTTP {raspuns.status} pentru {endpoint}: {text[:500]}")
                if not text.strip():
                    return {}
                try:
                    return await raspuns.json(content_type=None)
                except Exception as err:
                    raise EroareRaspunsRerVest(f"Răspuns JSON invalid pentru {endpoint}: {text[:500]}") from err
        except EroareApiRerVest:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareRerVest(f"Eroare de conectare la RER Vest pentru {endpoint}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareRerVest(f"Timeout la RER Vest pentru {endpoint}") from err

    async def async_login(self) -> dict[str, Any]:
        raspuns = await self._request(
            "POST",
            ENDPOINT_LOGIN,
            autentificat=False,
            json_data={"email": self._email, "password": self._parola},
        )
        if not isinstance(raspuns, dict):
            raise EroareAutentificareRerVest("Login RER Vest a returnat un răspuns invalid")

        token = _primul_text(
            raspuns,
            "token",
            "access_token",
            "accessToken",
            "jwt",
            "plainTextToken",
            "auth_token",
            adanc=True,
        )
        if token:
            self._token = token

        mesaj = str(_primul_text(raspuns, "message", "error", "status", adanc=True) or "").lower()
        succes = raspuns.get("success")
        if succes is False or any(cuv in mesaj for cuv in ("invalid", "wrong", "incorrect", "eroare", "parol")):
            raise EroareAutentificareRerVest("Credentialele RER Vest nu au fost acceptate")

        self._autentificat = True
        return raspuns

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        user = await self.async_get_user()
        clienti = await self.async_get_customers()
        facturi = await self.async_get_invoices()
        return {"user": user, "customers": clienti, "invoices": facturi}

    async def async_get_user(self) -> dict[str, Any]:
        raspuns = await self._request("GET", ENDPOINT_USER)
        return raspuns if isinstance(raspuns, dict) else {}

    async def async_get_invoices(self) -> list[dict[str, Any]]:
        return _extrage_lista(await self._request("GET", ENDPOINT_FACTURI))

    async def async_get_customers(self) -> list[dict[str, Any]]:
        return _extrage_lista(await self._request("GET", ENDPOINT_CLIENTI))

    async def async_get_docs(self, endpoint: str) -> list[dict[str, Any]]:
        return _extrage_lista(await self._request("GET", endpoint))

    async def async_sync_customers(self) -> None:
        try:
            await self._request("POST", ENDPOINT_SINCRONIZARE_CLIENT, json_data={})
        except EroareApiRerVest:
            _LOGGER.debug("Sincronizarea clienților RER Vest nu a reușit", exc_info=True)

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._autentificat:
            await self.async_login()
        await self.async_sync_customers()
        return {
            "user": await self.async_get_user(),
            "customers": await self.async_get_customers(),
            "invoices": await self.async_get_invoices(),
            "card_payments": await self.async_get_docs(ENDPOINT_PLATI_CARD),
            "payments": await self.async_get_docs(ENDPOINT_PLATI),
            "update_requests": await self.async_get_docs(ENDPOINT_CERERI_ACTUALIZARE),
            "orders": await self.async_get_docs(ENDPOINT_COMENZI),
            "products": await self.async_get_docs(ENDPOINT_PRODUSE),
        }


class ClientFurnizorRerVest(ClientFurnizor):
    cheie_furnizor = "rervest"
    nume_prietenos = "RER Vest"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiRerVest(sesiune=sesiune, email=utilizator, parola=parola)

    async def async_testeaza_conexiunea(self) -> str:
        try:
            rezultat = await self.api.async_validate_credentials()
        except EroareAutentificareRerVest as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareRerVest as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsRerVest as err:
            raise EroareParsare(str(err)) from err

        user = rezultat.get("user") or {}
        clienti = rezultat.get("customers") or []
        client = clienti[0] if clienti else {}
        unic = _primul_text(client, "id", "customer_id", "customerId", "code", "cod_client") or _primul_text(user, "id", "email")
        return str(unic or self.utilizator)

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            date_brute = await self.api.async_get_all_data()
        except EroareAutentificareRerVest as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareRerVest as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsRerVest as err:
            raise EroareParsare(str(err)) from err

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute, conturi)
        consumuri = self._mapeaza_consumuri(date_brute, conturi, facturi)
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra=self._construieste_extra(date_brute, facturi),
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        clienti = date_brute.get("customers") or []
        rezultate: list[ContUtilitate] = []
        vazute: set[str] = set()
        for client in clienti:
            if not isinstance(client, dict):
                continue
            id_cont = _primul_text(
                client,
                "id",
                "customer_id",
                "customerId",
                "client_id",
                "clientId",
                "code",
                "customer_code",
                "cod_client",
                "contract_id",
                "contractId",
                adanc=False,
            )
            if not id_cont:
                continue
            id_cont = str(id_cont).strip()
            if id_cont in vazute:
                continue
            vazute.add(id_cont)

            adresa = _adresa_din_obiect(client)
            nume = _primul_text(client, "name", "full_name", "display_name", "customer_name", "client_name", "denumire")
            rezultate.append(
                ContUtilitate(
                    id_cont=id_cont,
                    nume=nume or f"Client {id_cont}",
                    tip_cont="salubritate",
                    id_contract=_primul_text(client, "contract_id", "contractId", "contract_number", "contractNumber"),
                    adresa=adresa,
                    stare=_primul_text(client, "status", "state", "active"),
                    tip_utilitate="salubritate",
                    tip_serviciu="salubritate",
                    date_brute=client,
                )
            )

        if not rezultate:
            user = date_brute.get("user") if isinstance(date_brute.get("user"), dict) else {}
            id_cont = _primul_text(user, "id", "email") or self.utilizator
            rezultate.append(
                ContUtilitate(
                    id_cont=str(id_cont),
                    nume=_primul_text(user, "name", "email") or self.utilizator,
                    tip_cont="salubritate",
                    tip_utilitate="salubritate",
                    tip_serviciu="salubritate",
                    date_brute=user,
                )
            )
        return rezultate

    def _mapeaza_facturi(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        cont_principal = conturi[0] if conturi else None
        for factura in date_brute.get("invoices", []) or []:
            if not isinstance(factura, dict):
                continue
            id_factura = _primul_text(
                factura,
                "id",
                "invoice_id",
                "invoiceId",
                "invoice_number",
                "invoiceNumber",
                "number",
                "series",
                "serie",
                "document_number",
                "documentNumber",
            )
            if not id_factura:
                continue
            id_factura = str(id_factura).strip()
            id_cont = self._gaseste_id_cont(factura, conturi) or (cont_principal.id_cont if cont_principal else None)
            valoare = _float_sigur(_primul_valoare(factura, "total", "amount", "value", "invoice_value", "invoiceValue", "total_amount", "totalAmount", "valoare"))
            rest_plata = _float_sigur(_primul_valoare(factura, "remaining", "amount_remaining", "amountRemaining", "rest", "rest_plata", "unpaid", "unpaid_amount", "unpaidAmount", "amount_to_pay", "amountToPay"))
            status = _deduce_stare_factura(factura, rest_plata)
            pdf_url = _pdf_url(id_factura, factura)
            raw = dict(factura)
            if rest_plata is not None:
                raw["rest_plata"] = rest_plata
            elif status in {"neplatita", "scadenta"}:
                raw["rest_plata"] = valoare
            else:
                raw["rest_plata"] = 0.0 if status == "platita" else None
            raw["pdf_url"] = pdf_url
            facturi.append(
                FacturaUtilitate(
                    id_factura=id_factura,
                    titlu=_titlu_factura(factura, id_factura),
                    valoare=valoare,
                    moneda=_primul_text(factura, "currency", "moneda") or "RON",
                    data_emitere=_data_sigura(_primul_valoare(factura, "issue_date", "issueDate", "issued_at", "issuedAt", "invoice_date", "invoiceDate", "date", "created_at", "createdAt")),
                    data_scadenta=_data_sigura(_primul_valoare(factura, "due_date", "dueDate", "deadline", "scadenta", "payment_deadline", "paymentDeadline")),
                    stare=status,
                    categorie="consum",
                    id_cont=id_cont,
                    id_contract=_primul_text(factura, "contract_id", "contractId", "contract_number", "contractNumber"),
                    tip_utilitate="salubritate",
                    tip_serviciu="salubritate",
                    date_brute=raw,
                )
            )
        facturi.sort(key=lambda item: item.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont(self, factura: dict[str, Any], conturi: list[ContUtilitate]) -> str | None:
        valori = {
            str(_primul_valoare(factura, key) or "").strip()
            for key in (
                "customer_id",
                "customerId",
                "client_id",
                "clientId",
                "customer_code",
                "cod_client",
                "contract_id",
                "contractId",
                "contract_number",
                "contractNumber",
            )
        }
        valori.discard("")
        for cont in conturi:
            raw = cont.date_brute or {}
            candidati = {cont.id_cont, cont.id_contract or ""}
            for key in ("id", "customer_id", "customerId", "code", "customer_code", "cod_client", "contract_id", "contractId"):
                value = raw.get(key)
                if value not in (None, ""):
                    candidati.add(str(value).strip())
            if valori & candidati:
                return cont.id_cont
        return None

    def _mapeaza_consumuri(
        self,
        date_brute: dict[str, Any],
        conturi: list[ContUtilitate],
        facturi: list[FacturaUtilitate],
    ) -> list[ConsumUtilitate]:
        total_neachitat = sum(
            float(f.date_brute.get("rest_plata") if f.date_brute.get("rest_plata") is not None else f.valoare or 0)
            for f in facturi
            if f.stare in {"neplatita", "scadenta"}
        )
        consumuri = [
            ConsumUtilitate(cheie="sold_curent", valoare=round(total_neachitat, 2), unitate="RON"),
            ConsumUtilitate(cheie="numar_conturi", valoare=float(len(conturi)), unitate="buc"),
            ConsumUtilitate(cheie="numar_facturi", valoare=float(len(facturi)), unitate="buc"),
            ConsumUtilitate(cheie="numar_plati", valoare=float(len(date_brute.get("payments", []) or [])), unitate="buc"),
            ConsumUtilitate(cheie="numar_plati_card", valoare=float(len(date_brute.get("card_payments", []) or [])), unitate="buc"),
            ConsumUtilitate(cheie="numar_comenzi", valoare=float(len(date_brute.get("orders", []) or [])), unitate="buc"),
        ]
        ultima_plata = _ultima_inregistrare_dupa_data(date_brute.get("payments", []) or [])
        if ultima_plata:
            consumuri.append(ConsumUtilitate(cheie="data_ultima_plata", valoare=_data_text(ultima_plata), unitate=None))
            consumuri.append(ConsumUtilitate(cheie="valoare_ultima_plata", valoare=_float_sigur(_primul_valoare(ultima_plata, "amount", "value", "total", "valoare")), unitate="RON"))
        return consumuri

    def _construieste_extra(self, date_brute: dict[str, Any], facturi: list[FacturaUtilitate]) -> dict[str, Any]:
        return {
            "cont": date_brute.get("user", {}),
            "sumar": {
                "numar_clienti": len(date_brute.get("customers", []) or []),
                "numar_facturi": len(facturi),
                "numar_facturi_neachitate": sum(1 for f in facturi if f.stare in {"neplatita", "scadenta"}),
                "ultima_factura_id": facturi[0].id_factura if facturi else None,
                "ultima_factura_scadenta": facturi[0].data_scadenta.isoformat() if facturi and facturi[0].data_scadenta else None,
                "ultima_factura_valoare": facturi[0].valoare if facturi else None,
            },
            "date_brute": {
                "customers_count": len(date_brute.get("customers", []) or []),
                "payments_count": len(date_brute.get("payments", []) or []),
                "card_payments_count": len(date_brute.get("card_payments", []) or []),
                "update_requests_count": len(date_brute.get("update_requests", []) or []),
                "orders_count": len(date_brute.get("orders", []) or []),
                "products_count": len(date_brute.get("products", []) or []),
            },
        }


def _extrage_lista(raspuns: Any) -> list[dict[str, Any]]:
    if isinstance(raspuns, list):
        return [item for item in raspuns if isinstance(item, dict)]
    if not isinstance(raspuns, dict):
        return []
    for key in ("data", "docs", "items", "results", "records", "invoices", "customers", "payments", "orders", "products"):
        value = raspuns.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extrage_lista(value)
            if nested:
                return nested
    return []


def _primul_valoare(obiect: dict[str, Any], *chei: str) -> Any:
    for cheie in chei:
        if cheie in obiect and obiect.get(cheie) not in (None, ""):
            return obiect.get(cheie)
    return None


def _primul_text(obiect: dict[str, Any], *chei: str, adanc: bool = False) -> str | None:
    value = _primul_valoare(obiect, *chei)
    if value not in (None, ""):
        return str(value).strip()
    if adanc:
        for item in obiect.values():
            if isinstance(item, dict):
                found = _primul_text(item, *chei, adanc=True)
                if found:
                    return found
            elif isinstance(item, list):
                for subitem in item:
                    if isinstance(subitem, dict):
                        found = _primul_text(subitem, *chei, adanc=True)
                        if found:
                            return found
    return None


def _adresa_din_obiect(obiect: dict[str, Any]) -> str | None:
    direct = _primul_text(obiect, "address", "adresa", "full_address", "fullAddress", "billing_address", "billingAddress")
    if direct:
        return direct
    address_obj = obiect.get("address") or obiect.get("billing_address") or obiect.get("location")
    if isinstance(address_obj, dict):
        parti = []
        for key in ("city", "locality", "localitate", "street", "strada", "number", "numar", "building", "bloc", "apartment", "apartament"):
            value = address_obj.get(key)
            if value not in (None, ""):
                parti.append(str(value).strip())
        return ", ".join(parti) or None
    parti = []
    for key in ("city", "locality", "localitate", "street", "strada", "number", "numar", "building", "bloc", "apartment", "apartament"):
        value = obiect.get(key)
        if value not in (None, ""):
            parti.append(str(value).strip())
    return ", ".join(parti) or None


def _float_sigur(valoare: Any) -> float | None:
    if valoare in (None, "", "null"):
        return None
    if isinstance(valoare, bool):
        return None
    try:
        if isinstance(valoare, str):
            valoare = valoare.replace("RON", "").replace("lei", "").replace("Lei", "").replace(" ", "").replace(",", ".")
        return float(valoare)
    except (TypeError, ValueError):
        return None


def _data_sigura(valoare: Any) -> date | None:
    if not valoare:
        return None
    if isinstance(valoare, date) and not isinstance(valoare, datetime):
        return valoare
    text = str(valoare).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _data_text(obiect: dict[str, Any]) -> str | None:
    data = _data_sigura(_primul_valoare(obiect, "date", "created_at", "createdAt", "paid_at", "paidAt", "payment_date", "paymentDate"))
    return data.isoformat() if data else None


def _ultima_inregistrare_dupa_data(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [item for item in items if isinstance(item, dict)]
    if not valid:
        return None
    return sorted(valid, key=lambda item: _data_sigura(_primul_valoare(item, "date", "created_at", "createdAt", "paid_at", "paidAt", "payment_date", "paymentDate")) or date.min, reverse=True)[0]


def _titlu_factura(factura: dict[str, Any], id_factura: str) -> str:
    numar = _primul_text(factura, "invoice_number", "invoiceNumber", "number", "series", "serie", "document_number", "documentNumber") or id_factura
    return _primul_text(factura, "title", "description", "name") or f"Factura {numar}"


def _pdf_url(id_factura: str, factura: dict[str, Any]) -> str:
    direct = _primul_text(factura, "pdf_url", "download_url", "document_url", "pdf", "url")
    if direct:
        return urljoin(URL_BAZA, direct)
    return f"{URL_BAZA}/invoices/{id_factura}/download"


def _deduce_stare_factura(factura: dict[str, Any], rest_plata: float | None) -> str:
    status = str(_primul_text(factura, "status", "payment_status", "paymentStatus", "state", "stare") or "").strip().lower()
    scadenta = _data_sigura(_primul_valoare(factura, "due_date", "dueDate", "deadline", "scadenta"))
    marcaj_neachitat = _primul_valoare(
        factura,
        "unpaid",
        "is_unpaid",
        "isUnpaid",
        "unpaid_invoice",
        "unpaidInvoice",
        "open",
        "is_open",
        "isOpen",
    )
    marcaj_achitat = _primul_valoare(
        factura,
        "paid",
        "is_paid",
        "isPaid",
        "paid_invoice",
        "paidInvoice",
        "settled",
        "is_settled",
        "isSettled",
    )

    if rest_plata is not None:
        if rest_plata > 0:
            return "scadenta" if scadenta and scadenta < date.today() else "neplatita"
        return "platita"

    if _este_adevarat(marcaj_neachitat):
        return "scadenta" if scadenta and scadenta < date.today() else "neplatita"

    if any(token in status for token in ("unpaid", "neach", "neplat", "rest", "restant", "scadent", "scadenta")):
        return "scadenta" if scadenta and scadenta < date.today() else "neplatita"

    if _este_adevarat(marcaj_achitat):
        return "platita"

    if any(token in status for token in ("paid", "platit", "plătit", "achitat", "stins")):
        return "platita"

    return status or "necunoscuta"


def _este_adevarat(valoare: Any) -> bool:
    if isinstance(valoare, bool):
        return valoare
    if valoare in (None, ""):
        return False
    if isinstance(valoare, (int, float)):
        return valoare != 0
    return str(valoare).strip().lower() in {"1", "true", "yes", "da", "neachitat", "neplatit", "unpaid", "restant"}
