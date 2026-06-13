from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import json
import logging
from typing import Any

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://backend.nova-energy.ro/api"
URL_BAZA_PORTAL = "https://myaccount-admin.nova-energy.ro/api/v1"
ENDPOINT_LOGIN = "/accounts/login/client"
ENDPOINT_COMUTARE_CONT = "/accounts/switch"
ENDPOINT_PUNCTE_CONSUM = "/metering-points"
ENDPOINT_FACTURI = "/invoices"
ENDPOINT_BALANTE = "/balances"
ENDPOINT_PLATI = "/payments"
ENDPOINT_AUTOCITIRI = "/self-readings"
ENDPOINT_PUNCTE_AUTOCITIRE = "/metering-points/self-readings"
ENDPOINT_NOTIFICARI = "/legal-notifications"
ENDPOINT_INCIDENTE = "/incidents"
ENDPOINT_DASHBOARD_BALANTA = "/dashboard/balance"
ENDPOINT_DASHBOARD_SUMAR = "/dashboard/summary"
ENDPOINT_PORTAL_COMUTARE_CONT = "/auth/switch-account"
ENDPOINT_PORTAL_LOGIN = "/auth/login"


class EroareApiNova(Exception):
    pass


class EroareAutentificareNova(EroareApiNova):
    pass


class EroareConectareNova(EroareApiNova):
    pass


class EroareRaspunsNova(EroareApiNova):
    pass


@dataclass(slots=True)
class DateSesiuneNova:
    token: str
    expira_la: int


class ClientApiNova:
    def __init__(self, sesiune: aiohttp.ClientSession, email: str, parola: str) -> None:
        self._sesiune = sesiune
        self._email = email
        self._parola = parola
        self._token: str | None = None
        self._token_expira_la: int | None = None
        self.cont: dict[str, Any] = {}
        self.cont_vizualizat: dict[str, Any] = {}
        self.conturi_asociate: list[dict[str, Any]] = []
        self._portal_token: str | None = None
        self._portal_token_expira_la: int | None = None

    def _url(self, endpoint: str, *, baza: str = URL_BAZA) -> str:
        return f"{baza}{endpoint}"

    def _token_valid(self) -> bool:
        if not self._token or not self._token_expira_la:
            return False
        acum = int(datetime.now(tz=UTC).timestamp())
        return acum < (self._token_expira_la - 60)

    def _portal_token_valid(self) -> bool:
        if not self._portal_token or not self._portal_token_expira_la:
            return False
        acum = int(datetime.now(tz=UTC).timestamp())
        return acum < (self._portal_token_expira_la - 60)

    async def _request(
        self,
        metoda: str,
        endpoint: str,
        *,
        autentificat: bool = True,
        json_data: dict[str, Any] | None = None,
        account_id: str | None = None,
        baza_url: str = URL_BAZA,
    ) -> dict[str, Any]:
        if autentificat and not self._token_valid():
            await self.async_login()

        antete: dict[str, str] = {"Accept": "application/json"}
        if autentificat:
            if not self._token:
                raise EroareAutentificareNova("Lipsește tokenul de autentificare")
            antete["Authorization"] = f"Bearer {self._token}"
            id_cont = account_id or self.cont_vizualizat.get("accountId") or self.cont_vizualizat.get("_id") or self.cont_vizualizat.get("id")
            if id_cont:
                antete["x-account-id"] = str(id_cont)
        if json_data is not None:
            antete["Content-Type"] = "application/json"

        try:
            async with self._sesiune.request(
                metoda,
                self._url(endpoint, baza=baza_url),
                headers=antete,
                json=json_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    raise EroareAutentificareNova(f"Autentificare eșuată pentru {endpoint}: HTTP {raspuns.status}")
                if raspuns.status >= 400:
                    raise EroareApiNova(f"Nova API a returnat HTTP {raspuns.status} pentru {endpoint}: {text}")
                try:
                    data = await raspuns.json()
                except aiohttp.ContentTypeError as err:
                    raise EroareRaspunsNova(f"Răspuns JSON invalid pentru {endpoint}: {text}") from err
        except EroareApiNova:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareNova(f"Eroare de conectare la {endpoint}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareNova(f"Timeout la {endpoint}") from err

        if not isinstance(data, dict):
            raise EroareRaspunsNova(f"Tip de răspuns neașteptat pentru {endpoint}: {type(data)}")
        return data


    async def _request_portal(
        self,
        metoda: str,
        endpoint: str,
        *,
        json_data: dict[str, Any] | None = None,
        authorization: str | None = None,
        autentificat: bool = True,
    ) -> dict[str, Any]:
        """Trimite cereri către API-ul de portal Nova.

        Portalul ``myaccount-admin`` folosește propriul login și propriul token,
        diferit de tokenul API-ului backend ``backend.nova-energy.ro``.
        Tokenul de portal se obține din ``/auth/login`` și se trimite ca Bearer.
        """

        if autentificat and not authorization:
            if not self._portal_token_valid():
                await self.async_login_portal()
            if self._portal_token:
                authorization = f"Bearer {self._portal_token}"

        antete: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://myaccount.nova-energy.ro",
            "Referer": "https://myaccount.nova-energy.ro/",
            "User-Agent": "Mozilla/5.0",
        }
        if authorization:
            antete["Authorization"] = authorization

        try:
            async with self._sesiune.request(
                metoda,
                self._url(endpoint, baza=URL_BAZA_PORTAL),
                headers=antete,
                json=json_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as raspuns:
                text = await raspuns.text()
                if raspuns.status in (401, 403):
                    raise EroareAutentificareNova(f"Autentificare eșuată pentru portal {endpoint}: HTTP {raspuns.status}")
                if raspuns.status >= 400:
                    raise EroareApiNova(f"Nova portal a returnat HTTP {raspuns.status} pentru {endpoint}: {text}")
                try:
                    data = await raspuns.json()
                except aiohttp.ContentTypeError as err:
                    raise EroareRaspunsNova(f"Răspuns JSON invalid pentru portal {endpoint}: {text}") from err
        except EroareApiNova:
            raise
        except aiohttp.ClientError as err:
            raise EroareConectareNova(f"Eroare de conectare la portal {endpoint}: {err}") from err
        except TimeoutError as err:
            raise EroareConectareNova(f"Timeout la portal {endpoint}") from err

        if not isinstance(data, dict):
            raise EroareRaspunsNova(f"Tip de răspuns neașteptat pentru portal {endpoint}: {type(data)}")
        return data

    async def async_login_portal(self) -> DateSesiuneNova:
        """Autentifică sesiunea pentru API-ul de portal Nova."""

        raspuns = await self._request_portal(
            "POST",
            ENDPOINT_PORTAL_LOGIN,
            json_data={"email": self._email, "password": self._parola},
            autentificat=False,
        )
        data = raspuns.get("data", {}) if isinstance(raspuns, dict) else {}
        sesiune = data.get("session") if isinstance(data, dict) else None
        if not raspuns.get("success") or not isinstance(sesiune, dict):
            raise EroareAutentificareNova("Login portal Nova eșuat: răspuns invalid")

        token = sesiune.get("token")
        expira_la = sesiune.get("expireAt")
        if not token or not expira_la:
            raise EroareAutentificareNova("Login portal Nova eșuat: lipsă token sau expirare")

        self._portal_token = str(token)
        self._portal_token_expira_la = int(expira_la)
        return DateSesiuneNova(token=self._portal_token, expira_la=self._portal_token_expira_la)

    async def async_login(self) -> DateSesiuneNova:
        raspuns = await self._request("POST", ENDPOINT_LOGIN, autentificat=False, json_data={"email": self._email, "password": self._parola})
        data = raspuns.get("data", {})
        sesiune = data.get("session")
        if not raspuns.get("success") or not isinstance(sesiune, dict):
            raise EroareAutentificareNova("Login Nova eșuat: răspuns invalid")
        token = sesiune.get("token")
        expira_la = sesiune.get("expireAt")
        if not token or not expira_la:
            raise EroareAutentificareNova("Login Nova eșuat: lipsă token sau expirare")
        self._token = str(token)
        self._token_expira_la = int(expira_la)
        self.cont = data.get("loggedInAccount", {}) or {}
        self.cont_vizualizat = data.get("viewedAccount", {}) or {}
        asociate = self.cont.get("associatedAccounts") if isinstance(self.cont, dict) else []
        self.conturi_asociate = [cont for cont in asociate if isinstance(cont, dict)] if isinstance(asociate, list) else []
        return DateSesiuneNova(token=self._token, expira_la=self._token_expira_la)

    async def async_comuta_cont(self, cont: dict[str, Any]) -> dict[str, Any]:
        """Comută contextul Nova pe contul primit și actualizează datele sesiunii locale."""

        if not isinstance(cont, dict):
            raise EroareRaspunsNova("Cont Nova invalid pentru comutare")

        id_cont = self._account_id(cont)
        payload = {"accountId": id_cont} if id_cont else cont
        raspuns = await self._request("POST", ENDPOINT_COMUTARE_CONT, json_data=payload)
        data = raspuns.get("data", {}) if isinstance(raspuns, dict) else {}
        if not isinstance(data, dict):
            data = {}

        sesiune = data.get("session")
        if isinstance(sesiune, dict):
            token = sesiune.get("token")
            expira_la = sesiune.get("expireAt")
            if token:
                self._token = str(token)
            if expira_la:
                self._token_expira_la = int(expira_la)

        cont_logat = data.get("loggedInAccount")
        if isinstance(cont_logat, dict):
            self.cont = cont_logat
            asociate = self.cont.get("associatedAccounts")
            self.conturi_asociate = [item for item in asociate if isinstance(item, dict)] if isinstance(asociate, list) else []

        cont_vizualizat = data.get("viewedAccount")
        if isinstance(cont_vizualizat, dict):
            self.cont_vizualizat = cont_vizualizat
        else:
            self.cont_vizualizat = cont

        return self.cont_vizualizat

    async def async_comuta_cont_portal(self, account_id: str | None, *, authorization: str | None = None) -> bool:
        """Comută contextul în API-ul de portal Nova, folosit pentru soldul dashboard."""

        if not account_id:
            return False
        try:
            await self._request_portal(
                "POST",
                ENDPOINT_PORTAL_COMUTARE_CONT,
                json_data={"accountId": str(account_id)},
                authorization=authorization,
            )
            return True
        except EroareApiNova as err:
            _LOGGER.debug(
                "Nova: nu s-a putut comuta contextul de portal pe contul %s: %s",
                _nova_mask_identifier(account_id, "cont_anonimizat"),
                err,
            )
            return False

    async def async_validate_credentials(self) -> dict[str, Any]:
        await self.async_login()
        return {
            "account": self.cont,
            "viewed_account": self.cont_vizualizat,
            "metering_points": await self.async_get_metering_points(),
        }

    async def async_get_metering_points(self, account_id: str | None = None) -> list[dict[str, Any]]:
        raspuns = await self._request("GET", ENDPOINT_PUNCTE_CONSUM, account_id=account_id)
        data = raspuns.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("meteringPoints"), list):
                return data["meteringPoints"]
            if isinstance(data.get("docs"), list):
                return data["docs"]
        if isinstance(data, list):
            return data
        if isinstance(raspuns.get("meteringPoints"), list):
            return raspuns["meteringPoints"]
        if isinstance(raspuns.get("docs"), list):
            return raspuns["docs"]
        return []

    async def async_get_invoices(self, account_id: str | None = None) -> dict[str, Any]:
        raspuns = await self._request("GET", ENDPOINT_FACTURI, account_id=account_id)

        data = raspuns.get("data")
        if isinstance(data, dict) and isinstance(data.get("invoices"), list):
            balanta = data.get("balance") if isinstance(data.get("balance"), dict) else {}
            return {"invoices": [f for f in data["invoices"] if isinstance(f, dict)], "balance": balanta}

        docs = raspuns.get("docs", [])
        if not isinstance(docs, list):
            return {"invoices": [], "balance": {}}
        facturi: list[dict[str, Any]] = []
        balanta: dict[str, Any] = {}
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if not balanta and isinstance(doc.get("balance"), dict):
                balanta = doc["balance"]
            nested = doc.get("invoices")
            if isinstance(nested, list):
                facturi.extend([f for f in nested if isinstance(f, dict)])
            else:
                facturi.append(doc)
        return {"invoices": facturi, "balance": balanta}

    async def async_get_balance(self, account_id: str | None = None) -> dict[str, Any]:
        """Citește soldul curent Nova pentru contul activ.

        Sursa principală este portalul ``myaccount-admin`` deoarece acesta
        reflectă soldul afișat în interfața Nova. Portalul are autentificare
        separată față de backend, deci folosim ``/auth/login`` și apoi
        ``/auth/switch-account`` + ``/dashboard/balance``.
        """

        try:
            portal_comutat = await self.async_comuta_cont_portal(account_id)
            raspuns = await self._request_portal("GET", ENDPOINT_DASHBOARD_BALANTA)
            data = raspuns.get("data") if isinstance(raspuns, dict) else {}
            sold = data if isinstance(data, dict) else {}
            if sold:
                _LOGGER.debug(
                    "Diagnostic Nova: sold portal citit: %s",
                    json.dumps(
                        {
                            "account_id": _nova_mask_identifier(account_id, "cont_anonimizat"),
                            "portal_switched": portal_comutat,
                            "auth_mode": "portal_bearer",
                            "balance": _nova_safe_balance_debug(sold),
                            "keys_present": sorted(sold.keys()) if isinstance(sold, dict) else [],
                        },
                        ensure_ascii=False,
                    ),
                )
                return sold
        except EroareApiNova as err:
            _LOGGER.debug(
                "Diagnostic Nova: sold portal indisponibil: %s",
                json.dumps(
                    {
                        "account_id": _nova_mask_identifier(account_id, "cont_anonimizat"),
                        "auth_mode": "portal_bearer",
                        "error": str(err),
                    },
                    ensure_ascii=False,
                ),
            )

        try:
            raspuns = await self._request("GET", ENDPOINT_BALANTE, account_id=account_id)
            sold = _extrage_balanta_nova(raspuns)
            if sold:
                _LOGGER.debug(
                    "Diagnostic Nova: sold backend fallback citit: %s",
                    json.dumps(
                        {
                            "account_id": _nova_mask_identifier(account_id, "cont_anonimizat"),
                            "balance": _nova_safe_balance_debug(sold),
                            "keys_present": sorted(sold.keys()) if isinstance(sold, dict) else [],
                        },
                        ensure_ascii=False,
                    ),
                )
                return sold
        except EroareApiNova as err:
            _LOGGER.debug(
                "Diagnostic Nova: sold backend fallback indisponibil: %s",
                json.dumps(
                    {
                        "account_id": _nova_mask_identifier(account_id, "cont_anonimizat"),
                        "error": str(err),
                    },
                    ensure_ascii=False,
                ),
            )

        return {}

    async def async_get_dashboard_summary(self, account_id: str | None = None) -> dict[str, Any]:
        """Citește sumarul de dashboard din portalul Nova pentru contul activ.

        Sumarul de portal conține facturile așa cum sunt afișate în interfața
        Nova, inclusiv scadența facturii active. Dacă portalul nu răspunde,
        întoarcem un dicționar gol și păstrăm fallback-ul pe backend.
        """

        try:
            await self.async_comuta_cont_portal(account_id)
            raspuns = await self._request_portal("GET", ENDPOINT_DASHBOARD_SUMAR)
            data = raspuns.get("data") if isinstance(raspuns, dict) else {}
            return data if isinstance(data, dict) else {}
        except EroareApiNova as err:
            _LOGGER.debug(
                "Nova: sumarul de portal nu este disponibil pentru contul %s: %s",
                _nova_mask_identifier(account_id, "cont_anonimizat"),
                err,
            )
            return {}

    @staticmethod
    def _facturi_din_sumar_portal(sumar: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(sumar, dict):
            return []
        facturi = sumar.get("invoices")
        if isinstance(facturi, list):
            return [f for f in facturi if isinstance(f, dict)]
        return []

    async def async_get_docs(self, endpoint: str, account_id: str | None = None) -> list[dict[str, Any]]:
        raspuns = await self._request("GET", endpoint, account_id=account_id)
        data = raspuns.get("data")
        if isinstance(data, dict):
            for cheie in ("payments", "selfReadings", "meteringPoints", "legalNotifications", "incidents", "docs"):
                valoare = data.get(cheie)
                if isinstance(valoare, list):
                    return [item for item in valoare if isinstance(item, dict)]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        docs = raspuns.get("docs", [])
        return docs if isinstance(docs, list) else []

    def _account_id(self, cont: dict[str, Any] | None) -> str | None:
        if not isinstance(cont, dict):
            return None
        value = cont.get("accountId") or cont.get("_id") or cont.get("id")
        text = str(value or "").strip()
        return text or None

    def _lista_conturi_disponibile(self) -> list[dict[str, Any]]:
        conturi: list[dict[str, Any]] = []
        vazute: set[str] = set()

        def adauga(cont: dict[str, Any] | None) -> None:
            if not isinstance(cont, dict):
                return
            id_cont = self._account_id(cont)
            if not id_cont or id_cont in vazute:
                return
            vazute.add(id_cont)
            conturi.append(cont)

        adauga(self.cont_vizualizat)
        adauga(self.cont)
        for cont in self.conturi_asociate:
            adauga(cont)

        return conturi

    async def async_get_all_data(self) -> dict[str, Any]:
        if not self._token_valid():
            await self.async_login()

        cont_initial = self.cont_vizualizat if isinstance(self.cont_vizualizat, dict) else {}
        id_cont_initial = self._account_id(cont_initial)
        conturi_disponibile = self._lista_conturi_disponibile()
        conturi_date: list[dict[str, Any]] = []
        conturi_citite: set[str] = set()

        for cont in conturi_disponibile:
            id_cont_tinta = self._account_id(cont)
            if not id_cont_tinta:
                continue

            try:
                if self._account_id(self.cont_vizualizat) != id_cont_tinta:
                    cont_activ = await self.async_comuta_cont(cont)
                else:
                    cont_activ = self.cont_vizualizat or cont
            except EroareApiNova as err:
                _LOGGER.warning("Nova: nu s-a putut comuta pe contul asociat %s: %s", _nova_mask_identifier(id_cont_tinta, "cont_anonimizat"), err)
                continue

            id_cont_nova = self._account_id(cont_activ) or id_cont_tinta
            if not id_cont_nova or id_cont_nova in conturi_citite:
                continue
            conturi_citite.add(id_cont_nova)

            facturi = await self.async_get_invoices(account_id=id_cont_nova)
            balanta_portal = await self.async_get_balance(account_id=id_cont_nova)
            sumar_portal = await self.async_get_dashboard_summary(account_id=id_cont_nova)
            facturi_portal = self._facturi_din_sumar_portal(sumar_portal)
            facturi_cont = facturi_portal or facturi.get("invoices", [])
            puncte = await self.async_get_metering_points(account_id=id_cont_nova)
            plati = await self.async_get_docs(ENDPOINT_PLATI, account_id=id_cont_nova)
            autocitiri = await self.async_get_docs(ENDPOINT_AUTOCITIRI, account_id=id_cont_nova)
            puncte_autocitire = await self.async_get_docs(ENDPOINT_PUNCTE_AUTOCITIRE, account_id=id_cont_nova)
            notificari = await self.async_get_docs(ENDPOINT_NOTIFICARI, account_id=id_cont_nova)
            incidente = await self.async_get_docs(ENDPOINT_INCIDENTE, account_id=id_cont_nova)

            for punct in puncte:
                if isinstance(punct, dict):
                    punct["_nova_account"] = cont_activ
                    punct["_nova_account_id"] = id_cont_nova

            for factura in facturi_cont or []:
                if isinstance(factura, dict):
                    factura["_nova_account"] = cont_activ
                    factura["_nova_account_id"] = id_cont_nova

            conturi_date.append(
                {
                    "account": cont_activ,
                    "account_id": id_cont_nova,
                    "is_viewed_account": id_cont_nova == id_cont_initial,
                    "metering_points": puncte,
                    "invoices": facturi_cont,
                    "invoice_balance": balanta_portal if balanta_portal else (facturi.get("balance", {}) if isinstance(facturi.get("balance", {}), dict) else {}),
                    "portal_summary": sumar_portal,
                    "payments": plati,
                    "self_readings": autocitiri,
                    "metering_points_self_readings": puncte_autocitire,
                    "legal_notifications": notificari,
                    "incidents": incidente,
                }
            )

        if id_cont_initial and self._account_id(self.cont_vizualizat) != id_cont_initial:
            try:
                for cont in conturi_disponibile:
                    if self._account_id(cont) == id_cont_initial:
                        await self.async_comuta_cont(cont)
                        break
            except EroareApiNova as err:
                _LOGGER.debug("Nova: nu s-a putut restaura contul vizualizat initial: %s", err)

        return _combina_date_conturi_nova(
            account=self.cont,
            viewed_account=cont_initial or self.cont_vizualizat,
            associated_accounts=self.conturi_asociate,
            accounts_data=conturi_date,
        )


def _combina_date_conturi_nova(
    *,
    account: dict[str, Any],
    viewed_account: dict[str, Any],
    associated_accounts: list[dict[str, Any]],
    accounts_data: list[dict[str, Any]],
) -> dict[str, Any]:
    puncte: list[dict[str, Any]] = []
    facturi: list[dict[str, Any]] = []
    plati: list[dict[str, Any]] = []
    autocitiri: list[dict[str, Any]] = []
    puncte_autocitire: list[dict[str, Any]] = []
    notificari: list[dict[str, Any]] = []
    incidente: list[dict[str, Any]] = []

    balanta_de_plata = 0.0
    balanta_credit = 0.0
    balanta_prosumator = 0.0
    are_total = False
    are_credit = False
    are_prosumator = False

    for cont_date in accounts_data:
        puncte.extend([p for p in cont_date.get("metering_points", []) or [] if isinstance(p, dict)])
        facturi.extend([f for f in cont_date.get("invoices", []) or [] if isinstance(f, dict)])
        plati.extend([p for p in cont_date.get("payments", []) or [] if isinstance(p, dict)])
        autocitiri.extend([c for c in cont_date.get("self_readings", []) or [] if isinstance(c, dict)])
        puncte_autocitire.extend([p for p in cont_date.get("metering_points_self_readings", []) or [] if isinstance(p, dict)])
        notificari.extend([n for n in cont_date.get("legal_notifications", []) or [] if isinstance(n, dict)])
        incidente.extend([i for i in cont_date.get("incidents", []) or [] if isinstance(i, dict)])

        balanta = cont_date.get("invoice_balance", {}) or {}
        if isinstance(balanta, dict):
            total = _float_sigur(balanta.get("total"))
            if total is not None:
                if total > 0:
                    balanta_de_plata += total
                elif total < 0:
                    balanta_credit += total
                    are_credit = True
                are_total = True
            prosumer = _float_sigur(balanta.get("prosumer"))
            if prosumer is not None:
                balanta_prosumator += prosumer
                are_prosumator = True

    return {
        "account": account,
        "viewed_account": viewed_account,
        "associated_accounts": associated_accounts,
        "accounts_data": accounts_data,
        "metering_points": puncte,
        "invoices": facturi,
        "invoice_balance": {
            "total": round(balanta_de_plata, 2) if are_total else None,
            "credit": round(balanta_credit, 2) if are_credit else None,
            "prosumer": round(balanta_prosumator, 2) if are_prosumator else None,
        },
        "payments": plati,
        "self_readings": autocitiri,
        "metering_points_self_readings": puncte_autocitire,
        "legal_notifications": notificari,
        "incidents": incidente,
    }


class ClientFurnizorNova(ClientFurnizor):
    cheie_furnizor = "nova"
    nume_prietenos = "Nova Power & Gas"

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiNova(sesiune=sesiune, email=utilizator, parola=parola)

    async def async_testeaza_conexiunea(self) -> str:
        try:
            rezultat = await self.api.async_validate_credentials()
        except EroareAutentificareNova as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareNova as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsNova as err:
            raise EroareParsare(str(err)) from err
        cont = rezultat.get("viewed_account", {}) or {}
        return str(cont.get("accountNumber") or cont.get("_id") or self.utilizator)

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            date_brute = await self.api.async_get_all_data()
        except EroareAutentificareNova as err:
            raise EroareAutentificare(str(err)) from err
        except EroareConectareNova as err:
            raise EroareConectare(str(err)) from err
        except EroareRaspunsNova as err:
            raise EroareParsare(str(err)) from err

        _logheaza_diagnostic_nova(date_brute)

        conturi = self._mapeaza_conturi(date_brute)
        facturi = self._mapeaza_facturi(date_brute)
        consumuri = self._mapeaza_consumuri(date_brute, conturi)
        extra = self._construieste_extra(date_brute, facturi)

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra=extra,
        )

    def _mapeaza_conturi(self, date_brute: dict[str, Any]) -> list[ContUtilitate]:
        rezultate: list[ContUtilitate] = []

        for cont_date in date_brute.get("accounts_data", []) or []:
            if not isinstance(cont_date, dict):
                continue

            account = cont_date.get("account", {}) if isinstance(cont_date.get("account"), dict) else {}
            account_id = str(cont_date.get("account_id") or account.get("accountId") or "").strip()
            if not account_id:
                continue

            puncte = [p for p in cont_date.get("metering_points", []) or [] if isinstance(p, dict)]
            balanta = cont_date.get("invoice_balance", {}) if isinstance(cont_date.get("invoice_balance"), dict) else {}
            tipuri_servicii = sorted({
                tip for tip in (
                    _normalizeaza_tip_serviciu(
                        punct.get("utilityType")
                        or punct.get("utility")
                        or punct.get("serviceType")
                        or punct.get("commodity")
                        or punct.get("type")
                        or ""
                    )
                    for punct in puncte
                )
                if tip
            })

            if len(tipuri_servicii) == 1:
                tip_serviciu = tipuri_servicii[0]
            elif len(tipuri_servicii) > 1:
                tip_serviciu = "mixt"
            else:
                tip_serviciu = None

            adresa = account.get("address")
            if not isinstance(adresa, str) or not adresa.strip():
                adrese_puncte = [p.get("address") for p in puncte if isinstance(p.get("address"), str) and p.get("address")]
                adresa = adrese_puncte[0] if adrese_puncte else None

            contracte = [str(p.get("contractId") or "").strip() for p in puncte if str(p.get("contractId") or "").strip()]
            raw = {
                "nova_account": account,
                "nova_account_id": account_id,
                "account_number": account.get("accountNumber"),
                "invoice_balance": balanta,
                "metering_points": puncte,
                "tipuri_servicii_active": tipuri_servicii,
                "payments_count": len(cont_date.get("payments", []) or []),
                "invoices_count": len(cont_date.get("invoices", []) or []),
                "este_prosumator": bool(_valoare_adevarata_nova(balanta.get("isProsumer")) and "curent" in tipuri_servicii),
            }

            rezultate.append(
                ContUtilitate(
                    id_cont=account_id,
                    nume=str(account.get("accountNumber") or account.get("accountName") or account_id),
                    tip_cont=str(account.get("role") or "client") or None,
                    id_contract=", ".join(contracte) if contracte else None,
                    adresa=adresa if isinstance(adresa, str) else None,
                    stare="active",
                    tip_utilitate=tip_serviciu,
                    tip_serviciu=tip_serviciu,
                    este_prosumator=bool(raw.get("este_prosumator")),
                    date_brute=raw,
                )
            )

        return rezultate


    def _mapeaza_facturi(self, date_brute: dict[str, Any]) -> list[FacturaUtilitate]:
        facturi: list[FacturaUtilitate] = []
        balante_conturi = _nova_balante_pe_cont(date_brute)

        for factura in date_brute.get("invoices", []) or []:
            id_factura = str(factura.get("invoiceId") or factura.get("series") or factura.get("invoiceSeries") or factura.get("number") or factura.get("invoiceNumber") or "").strip()
            if not id_factura:
                continue

            id_cont = str(factura.get("_nova_account_id") or "") or self._gaseste_id_cont_pentru_factura(date_brute.get("metering_points", []), factura)
            balanta_cont = balante_conturi.get(id_cont, {}) if id_cont else {}
            sold_total_cont = _float_sigur(balanta_cont.get("total")) if isinstance(balanta_cont, dict) else None

            valoare = _valoare_factura_nova(factura)
            rest_plata = _rest_plata_factura_nova(factura)

            # Dacă portalul Nova arată sold curent negativ sau zero pe cont,
            # facturile cu rest pozitiv sunt acoperite de sold/credit și nu
            # trebuie expuse în dashboard ca datorii active.
            rest_pentru_stare = rest_plata
            if sold_total_cont is not None and sold_total_cont <= 0 and rest_plata is not None and rest_plata > 0:
                rest_pentru_stare = 0.0

            tip_serviciu = _normalizeaza_tip_serviciu(
                factura.get("utilityType")
                or factura.get("utility")
                or factura.get("serviceType")
                or factura.get("commodity")
                or factura.get("type")
                or ""
            )
            facturi.append(
                FacturaUtilitate(
                    id_factura=id_factura,
                    titlu=str(factura.get("type") or factura.get("title") or f"Factura {id_factura}"),
                    valoare=valoare,
                    moneda="RON",
                    data_emitere=_data_sigura(factura.get("issueDate") or factura.get("issuedAt") or factura.get("date")),
                    data_scadenta=_data_sigura(factura.get("dueDate") or factura.get("dueAt")),
                    stare=_deduce_stare_factura(factura, rest_pentru_stare),
                    categorie=_deduce_categorie_factura(factura),
                    id_cont=id_cont,
                    id_contract=str(factura.get("contractId") or "") or None,
                    tip_utilitate=tip_serviciu,
                    tip_serviciu=tip_serviciu,
                    este_prosumator=_deduce_categorie_factura(factura) == "injectie",
                    date_brute={**factura, "rest_plata": rest_pentru_stare},
                )
            )
        facturi.sort(key=lambda x: x.data_emitere or date.min, reverse=True)
        return facturi

    def _gaseste_id_cont_pentru_factura(self, puncte: list[dict[str, Any]], factura: dict[str, Any]) -> str | None:
        numar_punct = str(factura.get("meteringPointNumber") or "").strip()
        cod_specific = str(factura.get("meteringPointCode") or "").strip()
        for punct in puncte or []:
            if numar_punct and str(punct.get("number") or "").strip() == numar_punct:
                return str(punct.get("meteringPointId") or punct.get("_id") or punct.get("id") or "") or None
            if cod_specific and str(punct.get("specificIdForUtilityType") or "").strip() == cod_specific:
                return str(punct.get("meteringPointId") or punct.get("_id") or punct.get("id") or "") or None
        return None

    def _mapeaza_consumuri(self, date_brute: dict[str, Any], conturi: list[ContUtilitate]) -> list[ConsumUtilitate]:
        consumuri: list[ConsumUtilitate] = []
        balanta = date_brute.get("invoice_balance", {}) or {}

        tipuri_servicii_active = sorted({
            tip
            for cont in conturi
            for tip in _nova_tipuri_active_cont(cont)
            if tip
        })
        este_prosumator = any(bool(cont.este_prosumator) for cont in conturi)

        consumuri.extend([
            ConsumUtilitate(cheie="sold_curent", valoare=_float_sigur(balanta.get("total")), unitate="RON"),
            ConsumUtilitate(cheie="sold_credit", valoare=_float_sigur(balanta.get("credit")), unitate="RON"),
            ConsumUtilitate(cheie="sold_prosumator", valoare=_float_sigur(balanta.get("prosumer")), unitate="RON"),
            ConsumUtilitate(cheie="este_prosumator", valoare="da" if este_prosumator else "nu", unitate=None),
            ConsumUtilitate(cheie="tipuri_servicii", valoare=", ".join(tipuri_servicii_active) if tipuri_servicii_active else None, unitate=None),
            ConsumUtilitate(cheie="numar_puncte_consum", valoare=float(sum(len(_nova_puncte_cont(c)) for c in conturi)), unitate="buc"),
            ConsumUtilitate(cheie="numar_conturi_curent", valoare=float(sum(1 for c in conturi if "curent" in _nova_tipuri_active_cont(c))), unitate="buc"),
            ConsumUtilitate(cheie="numar_conturi_gaz", valoare=float(sum(1 for c in conturi if "gaz" in _nova_tipuri_active_cont(c))), unitate="buc"),
            ConsumUtilitate(cheie="numar_facturi", valoare=float(len(date_brute.get("invoices", []) or [])), unitate="buc"),
            ConsumUtilitate(cheie="numar_plati", valoare=float(len(date_brute.get("payments", []) or [])), unitate="buc"),
        ])

        date_conturi = _nova_date_pe_cont(date_brute)
        for cont in conturi:
            raw = cont.date_brute if isinstance(cont.date_brute, dict) else {}
            account_id = str(raw.get("nova_account_id") or "").strip()
            date_cont = date_conturi.get(account_id, {})
            balanta_cont = date_cont.get("invoice_balance", {}) if isinstance(date_cont, dict) else {}
            facturi_cont = [f for f in date_brute.get("invoices", []) or [] if str(f.get("_nova_account_id") or "") == cont.id_cont]
            facturi_cont.sort(key=lambda f: _data_emitere_factura_nova(f) or date.min, reverse=True)
            ultima = _alege_factura_reprezentativa_nova(facturi_cont)
            rest_ultima = _rest_plata_factura_nova(ultima) if ultima else None
            valoare_ultima = _valoare_afisata_factura_nova(ultima) if ultima else None
            sold_total = _float_sigur(balanta_cont.get("total")) if isinstance(balanta_cont, dict) else None
            sold_prosumator = _float_sigur(balanta_cont.get("prosumer")) if isinstance(balanta_cont, dict) else None
            sold_credit = round(float(sold_total), 2) if sold_total is not None and sold_total < 0 else None
            de_plata = round(max(float(sold_total or 0.0), 0.0), 2) if sold_total is not None else None

            if de_plata is not None and de_plata <= 0:
                # Dacă soldul de cont este zero sau negativ, factura reprezentativă
                # rămâne informativă, dar nu mai este tratată ca scadență activă.
                rest_ultima = 0.0
                scadenta_ultima = None
            else:
                # În portalul Nova unele facturi cu rest de plată pot veni cu
                # status „paid”, deși sunt afișate ca documente de plată parțială.
                # Pentru scadența activă folosim restul de plată, nu doar statusul.
                scadenta_ultima = _data_scadenta_factura_nova(ultima) if ultima and (rest_ultima or 0) > 0 else None

            este_prosumator_cont = bool(cont.este_prosumator)

            consumuri.extend([
                ConsumUtilitate(cheie="sold_curent", valoare=sold_total, unitate="RON", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="de_plata", valoare=de_plata, unitate="RON", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="sold_credit", valoare=sold_credit, unitate="RON", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="sold_prosumator", valoare=sold_prosumator, unitate="RON", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="este_prosumator", valoare="da" if este_prosumator_cont else "nu", unitate=None, id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="tipuri_servicii", valoare=", ".join(_nova_tipuri_active_cont(cont)) or None, unitate=None, id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="numar_facturi", valoare=float(len(facturi_cont)), unitate="buc", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="numar_plati", valoare=float(len(date_cont.get("payments", []) or [])) if isinstance(date_cont, dict) else 0.0, unitate="buc", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="factura_restanta", valoare="da" if (rest_ultima or 0) > 0 else "nu", unitate=None, id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="sold_factura", valoare=rest_ultima, unitate="RON", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="id_ultima_factura", valoare=str((ultima or {}).get("invoiceId") or "") or None, unitate=None, id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="valoare_ultima_factura", valoare=valoare_ultima, unitate="RON", id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
                ConsumUtilitate(cheie="urmatoarea_scadenta", valoare=scadenta_ultima.strftime("%d.%m.%Y") if scadenta_ultima else None, unitate=None, id_cont=cont.id_cont, tip_utilitate=cont.tip_utilitate, tip_serviciu=cont.tip_serviciu),
            ])
        return consumuri


    def _construieste_extra(self, date_brute: dict[str, Any], facturi: list[FacturaUtilitate]) -> dict[str, Any]:
        balanta = date_brute.get("invoice_balance", {}) or {}
        factura_reprezentativa = _alege_factura_reprezentativa_nova([f.date_brute for f in facturi if isinstance(f.date_brute, dict)])
        data_scadenta_reprezentativa = _data_scadenta_factura_nova(factura_reprezentativa)
        return {
            "cont": date_brute.get("account", {}),
            "cont_vizualizat": date_brute.get("viewed_account", {}),
            "conturi_asociate": date_brute.get("associated_accounts", []),
            "sumar": {
                "total_rest_de_plata": _float_sigur(balanta.get("total")),
                "sold_prosumator": _float_sigur(balanta.get("prosumer")),
                "numar_facturi": len(facturi),
                "numar_facturi_neachitate": sum(1 for f in facturi if f.stare in {"neplatita", "scadenta"}),
                "ultima_factura_id": str((factura_reprezentativa or {}).get("invoiceId") or "") or (facturi[0].id_factura if facturi else None),
                "ultima_factura_scadenta": data_scadenta_reprezentativa.isoformat() if data_scadenta_reprezentativa else None,
                "ultima_factura_valoare": _valoare_afisata_factura_nova(factura_reprezentativa) if factura_reprezentativa else (facturi[0].valoare if facturi else None),
            },
            "date_brute": {
                "invoice_balance": balanta,
                "payments_count": len(date_brute.get("payments", []) or []),
                "self_readings_count": len(date_brute.get("self_readings", []) or []),
                "metering_points_count": len(date_brute.get("metering_points", []) or []),
                "accounts_count": len(date_brute.get("accounts_data", []) or []),
            },
        }


def _nova_date_pe_cont(date_brute: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rezultate: dict[str, dict[str, Any]] = {}
    for cont_date in date_brute.get("accounts_data", []) or []:
        if not isinstance(cont_date, dict):
            continue
        account_id = str(cont_date.get("account_id") or "").strip()
        if account_id:
            rezultate[account_id] = cont_date
    return rezultate


def _nova_balante_pe_cont(date_brute: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        account_id: (cont_date.get("invoice_balance", {}) or {})
        for account_id, cont_date in _nova_date_pe_cont(date_brute).items()
        if isinstance(cont_date.get("invoice_balance", {}), dict)
    }


def _nova_puncte_cont(cont: ContUtilitate) -> list[dict[str, Any]]:
    raw = cont.date_brute if isinstance(cont.date_brute, dict) else {}
    puncte = raw.get("metering_points", [])
    return [punct for punct in puncte if isinstance(punct, dict)] if isinstance(puncte, list) else []


def _nova_tipuri_active_cont(cont: ContUtilitate) -> list[str]:
    raw = cont.date_brute if isinstance(cont.date_brute, dict) else {}
    tipuri = raw.get("tipuri_servicii_active")
    if isinstance(tipuri, list):
        return sorted({str(tip).strip() for tip in tipuri if str(tip).strip()})
    tip = str(cont.tip_serviciu or cont.tip_utilitate or "").strip()
    return [tip] if tip else []


def _valoare_adevarata_nova(valoare: Any) -> bool:
    if isinstance(valoare, bool):
        return valoare
    if isinstance(valoare, (int, float)):
        return valoare != 0
    if valoare in (None, "", [], {}):
        return False
    return str(valoare).strip().lower() in {"1", "true", "da", "yes", "y", "on"}



def _logheaza_diagnostic_nova(date_brute: dict[str, Any]) -> None:
    """Scrie in log un diagnostic anonimizat pentru investigarea conturilor Nova.

    Folosim warning intentionat in beta, ca mesajele sa apara direct in
    Settings -> System -> Logs, fara configurare manuala de logger.
    """

    try:
        puncte = date_brute.get("metering_points", []) or []
        facturi = date_brute.get("invoices", []) or []
        balanta = date_brute.get("invoice_balance", {}) or {}
        accounts_data = date_brute.get("accounts_data", []) or []

        _LOGGER.warning(
            "[NOVA DEBUG] Sumar global: %s",
            json.dumps(
                {
                    "logged_account": _nova_safe_account_debug(date_brute.get("account", {}) or {}),
                    "viewed_account": _nova_safe_account_debug(date_brute.get("viewed_account", {}) or {}),
                    "associated_accounts_count": len(date_brute.get("associated_accounts", []) or []),
                    "accounts_data_count": len(accounts_data),
                    "metering_points_count": len(puncte),
                    "invoices_count": len(facturi),
                    "payments_count": len(date_brute.get("payments", []) or []),
                    "self_readings_count": len(date_brute.get("self_readings", []) or []),
                    "metering_points_self_readings_count": len(date_brute.get("metering_points_self_readings", []) or []),
                    "global_balance": _nova_safe_balance_debug(balanta),
                },
                ensure_ascii=False,
                default=str,
            ),
        )

        for index, cont_date in enumerate(accounts_data[:10]):
            if not isinstance(cont_date, dict):
                continue
            cont_puncte = [p for p in cont_date.get("metering_points", []) or [] if isinstance(p, dict)]
            cont_facturi = [f for f in cont_date.get("invoices", []) or [] if isinstance(f, dict)]
            cont_plati = [p for p in cont_date.get("payments", []) or [] if isinstance(p, dict)]
            cont_balanta = cont_date.get("invoice_balance", {}) if isinstance(cont_date.get("invoice_balance"), dict) else {}
            portal_summary = cont_date.get("portal_summary", {}) if isinstance(cont_date.get("portal_summary"), dict) else {}

            _LOGGER.warning(
                "[NOVA DEBUG] Cont[%s]: %s",
                index,
                json.dumps(
                    {
                        "account_id": _nova_mask_identifier(cont_date.get("account_id"), "cont_anonimizat"),
                        "is_viewed_account": bool(cont_date.get("is_viewed_account")),
                        "account": _nova_safe_account_debug(cont_date.get("account", {}) or {}),
                        "metering_points_count": len(cont_puncte),
                        "metering_points": [_nova_safe_metering_point_debug(punct) for punct in cont_puncte[:8]],
                        "invoices_count": len(cont_facturi),
                        "invoices": [_nova_safe_invoice_debug(factura) for factura in cont_facturi[:8]],
                        "payments_count": len(cont_plati),
                        "invoice_balance": _nova_safe_balance_debug(cont_balanta),
                        "portal_summary_keys": sorted(str(key) for key in portal_summary.keys())[:40],
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )

        _LOGGER.warning(
            "[NOVA DEBUG] Facturi agregate: %s",
            json.dumps(
                {
                    "invoices_count": len(facturi),
                    "invoices": [_nova_safe_invoice_debug(factura) for factura in facturi[:12]],
                    "metering_points": [_nova_safe_metering_point_debug(punct) for punct in puncte[:12]],
                },
                ensure_ascii=False,
                default=str,
            ),
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("[NOVA DEBUG] Nu s-a putut genera diagnosticul anonimizat: %s", err)

def _nova_safe_account_debug(cont: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cont, dict):
        return {}

    return {
        "id": _nova_mask_identifier(
            cont.get("accountId") or cont.get("_id") or cont.get("id"),
            "cont_anonimizat",
        ),
        "account_number": _nova_mask_identifier(
            cont.get("accountNumber") or cont.get("number") or cont.get("clientCode"),
            "numar_cont_anonimizat",
        ),
        "type": _nova_safe_label(cont.get("type") or cont.get("accountType") or cont.get("role")),
        "status": _nova_safe_label(cont.get("status")),
        "keys_present": sorted(str(key) for key in cont.keys())[:40],
    }


def _nova_safe_metering_point_debug(punct: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(punct, dict):
        return {}

    adresa = punct.get("address")
    return {
        "id": _nova_mask_identifier(
            punct.get("meteringPointId") or punct.get("_id") or punct.get("id"),
            "punct_consum_anonimizat",
        ),
        "number": _nova_mask_identifier(punct.get("number"), "numar_punct_anonimizat"),
        "specific_id": _nova_mask_identifier(
            punct.get("specificIdForUtilityType") or punct.get("specificId") or punct.get("code"),
            "cod_punct_anonimizat",
        ),
        "utility_type": _nova_safe_label(
            punct.get("utilityType")
            or punct.get("utility")
            or punct.get("serviceType")
            or punct.get("commodity")
            or punct.get("type")
        ),
        "normalized_utility_type": _normalizeaza_tip_serviciu(
            punct.get("utilityType")
            or punct.get("utility")
            or punct.get("serviceType")
            or punct.get("commodity")
            or punct.get("type")
            or ""
        ),
        "contract_type": _nova_safe_label(punct.get("contractType")),
        "contract_id": _nova_mask_identifier(punct.get("contractId"), "contract_anonimizat"),
        "status": _nova_safe_label(punct.get("status")),
        "has_address": bool(adresa),
        "address": _nova_mask_identifier(adresa, "adresa_anonimizata") if adresa else None,
        "is_prosumer_flag": _nova_boolish(
            punct.get("isProsumer") or punct.get("prosumer") or punct.get("hasInjection") or punct.get("injection")
        ),
        "keys_present": sorted(str(key) for key in punct.keys())[:50],
    }


def _nova_safe_invoice_debug(factura: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(factura, dict):
        return {}

    return {
        "id": _nova_mask_identifier(
            factura.get("invoiceId")
            or factura.get("id")
            or factura.get("_id")
            or factura.get("number")
            or factura.get("invoiceNumber"),
            "factura_anonimizata",
        ),
        "invoice_number": _nova_mask_identifier(
            factura.get("invoiceNumber") or factura.get("number") or factura.get("series") or factura.get("invoiceSeries"),
            "numar_factura_anonimizat",
        ),
        "issue_date": _nova_safe_label(factura.get("issueDate") or factura.get("issuedAt") or factura.get("date")),
        "due_date": _nova_safe_label(factura.get("dueDate") or factura.get("dueAt")),
        "amount": _float_sigur(
            factura.get("amountTotal")
            or factura.get("value")
            or factura.get("invoiceValue")
            or factura.get("total")
            or factura.get("amount")
            or factura.get("totalAmount")
        ),
        "remaining": _float_sigur(
            factura.get("amountToPay")
            or factura.get("restToPay")
            or factura.get("rest")
            or factura.get("remainingValue")
            or factura.get("remaining")
            or factura.get("amountRemaining")
        ),
        "status": _nova_safe_label(factura.get("status") or factura.get("paymentStatus")),
        "type": _nova_safe_label(factura.get("type")),
        "title": _nova_safe_label(factura.get("title")),
        "category": _nova_safe_label(factura.get("category") or factura.get("description") or factura.get("invoiceType")),
        "utility_type": _nova_safe_label(
            factura.get("utilityType")
            or factura.get("utility")
            or factura.get("serviceType")
            or factura.get("commodity")
        ),
        "normalized_utility_type": _normalizeaza_tip_serviciu(
            factura.get("utilityType")
            or factura.get("utility")
            or factura.get("serviceType")
            or factura.get("commodity")
            or factura.get("type")
            or ""
        ),
        "metering_point_number": _nova_mask_identifier(
            factura.get("meteringPointNumber"),
            "numar_punct_anonimizat",
        ),
        "metering_point_code": _nova_mask_identifier(
            factura.get("meteringPointCode"),
            "cod_punct_anonimizat",
        ),
        "contract_id": _nova_mask_identifier(factura.get("contractId"), "contract_anonimizat"),
        "keys_present": sorted(str(key) for key in factura.keys())[:60],
    }


def _extrage_balanta_nova(raspuns: dict[str, Any]) -> dict[str, Any]:
    """Normalizează răspunsurile posibile ale endpoint-ului Nova /balances."""

    if not isinstance(raspuns, dict):
        return {}

    candidati: list[dict[str, Any]] = []

    def adauga_candidat(valoare: Any) -> None:
        if isinstance(valoare, dict):
            candidati.append(valoare)

    adauga_candidat(raspuns)
    data = raspuns.get("data")
    adauga_candidat(data)

    if isinstance(data, dict):
        for cheie in ("balance", "balances", "currentBalance", "accountBalance", "saldo"):
            valoare = data.get(cheie)
            if isinstance(valoare, dict):
                candidati.append(valoare)
            elif isinstance(valoare, list):
                candidati.extend([item for item in valoare if isinstance(item, dict)])
    elif isinstance(data, list):
        candidati.extend([item for item in data if isinstance(item, dict)])

    docs = raspuns.get("docs")
    if isinstance(docs, list):
        candidati.extend([item for item in docs if isinstance(item, dict)])

    for candidat in candidati:
        if not isinstance(candidat, dict):
            continue

        total = _float_sigur(
            candidat.get("total")
            or candidat.get("sold")
            or candidat.get("balance")
            or candidat.get("currentBalance")
            or candidat.get("amount")
            or candidat.get("amountToPay")
        )
        prosumer = _float_sigur(candidat.get("prosumer") or candidat.get("prosumerBalance") or candidat.get("soldProsumator"))
        credit = _float_sigur(candidat.get("credit") or candidat.get("creditBalance"))

        rezultat = dict(candidat)
        if total is not None:
            rezultat["total"] = total
        if prosumer is not None:
            rezultat["prosumer"] = prosumer
        if credit is not None:
            rezultat["credit"] = credit

        if any(_float_sigur(rezultat.get(cheie)) is not None for cheie in ("total", "prosumer", "credit", "electricity", "gas")):
            return rezultat

    return {}


def _nova_safe_balance_debug(balanta: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(balanta, dict):
        return {}

    return {
        "total": _float_sigur(balanta.get("total")),
        "prosumer": _float_sigur(balanta.get("prosumer")),
        "keys_present": sorted(str(key) for key in balanta.keys())[:40],
    }


def _nova_mask_identifier(valoare: Any, prefix: str) -> str | None:
    if valoare in (None, "", [], {}):
        return None
    text = json.dumps(valoare, ensure_ascii=False, sort_keys=True, default=str) if isinstance(valoare, (dict, list)) else str(valoare)
    digest = hashlib.sha256(f"utilitati_romania_nova::{text}".encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def _nova_safe_label(valoare: Any, *, limita: int = 80) -> str | None:
    if valoare in (None, ""):
        return None
    text = str(valoare).strip()
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) > limita:
        text = f"{text[:limita]}…"
    return text


def _nova_boolish(valoare: Any) -> bool | None:
    if valoare in (None, ""):
        return None
    if isinstance(valoare, bool):
        return valoare
    if isinstance(valoare, (int, float)):
        return bool(valoare)
    text = str(valoare).strip().lower()
    if text in {"true", "1", "da", "yes", "y"}:
        return True
    if text in {"false", "0", "nu", "no", "n"}:
        return False
    return None

def _normalizeaza_tip_serviciu(valoare: Any) -> str | None:
    if valoare in (None, ""):
        return None
    text = str(valoare).strip().lower()
    if not text:
        return None

    if any(cuvant in text for cuvant in ("gaz", "gaze", "natural gas", "gas")):
        return "gaz"
    if any(cuvant in text for cuvant in ("energie electric", "electricitate", "electric", "curent", "power", "energy", "electricity")):
        return "curent"
    return text


def _prima_valoare_nova(*valori: Any) -> Any:
    for valoare in valori:
        if valoare not in (None, "", "null"):
            return valoare
    return None


def _float_sigur(valoare: Any) -> float | None:
    if valoare in (None, "", "null"):
        return None
    try:
        return float(valoare)
    except (TypeError, ValueError):
        return None


def _data_sigura(valoare: Any) -> date | None:
    if not valoare:
        return None
    text = str(valoare)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None



def _rest_plata_factura_nova(factura: dict[str, Any] | None) -> float | None:
    if not isinstance(factura, dict):
        return None
    return _float_sigur(
        _prima_valoare_nova(
            factura.get("amountToPay"),
            factura.get("restToPay"),
            factura.get("rest"),
            factura.get("remainingValue"),
            factura.get("remaining"),
            factura.get("amountRemaining"),
        )
    )


def _valoare_factura_nova(factura: dict[str, Any] | None) -> float | None:
    if not isinstance(factura, dict):
        return None
    return _float_sigur(
        _prima_valoare_nova(
            factura.get("amountTotal"),
            factura.get("value"),
            factura.get("invoiceValue"),
            factura.get("total"),
            factura.get("amount"),
            factura.get("totalAmount"),
        )
    )


def _status_factura_nova(factura: dict[str, Any] | None) -> str:
    if not isinstance(factura, dict):
        return ""
    return str(factura.get("status") or factura.get("paymentStatus") or "").strip().lower()


def _data_emitere_factura_nova(factura: dict[str, Any] | None) -> date | None:
    if not isinstance(factura, dict):
        return None
    return _data_sigura(factura.get("issueDate") or factura.get("issuedAt") or factura.get("date"))


def _data_scadenta_factura_nova(factura: dict[str, Any] | None) -> date | None:
    if not isinstance(factura, dict):
        return None
    return _data_sigura(factura.get("dueDate") or factura.get("dueAt"))


def _este_factura_activa_nova(factura: dict[str, Any] | None) -> bool:
    if not isinstance(factura, dict):
        return False
    status = _status_factura_nova(factura)
    if any(text in status for text in ("paid", "plat", "reversed", "storno", "cancel")):
        return False
    rest = _rest_plata_factura_nova(factura)
    if rest is not None:
        return rest > 0
    valoare = _valoare_factura_nova(factura)
    return valoare is not None and valoare > 0


def _este_factura_pozitiva_nova(factura: dict[str, Any] | None) -> bool:
    if not isinstance(factura, dict):
        return False
    status = _status_factura_nova(factura)
    if any(text in status for text in ("reversed", "storno", "cancel")):
        return False
    valoare = _valoare_factura_nova(factura)
    if valoare is not None and valoare > 0:
        return True
    rest = _rest_plata_factura_nova(factura)
    return rest is not None and rest > 0


def _alege_factura_reprezentativa_nova(facturi: list[dict[str, Any]]) -> dict[str, Any] | None:
    facturi_valide = [f for f in facturi or [] if isinstance(f, dict)]
    if not facturi_valide:
        return None

    active = [f for f in facturi_valide if _este_factura_activa_nova(f)]
    if active:
        return sorted(
            active,
            key=lambda f: (
                _data_scadenta_factura_nova(f) or date.max,
                _data_emitere_factura_nova(f) or date.min,
            ),
        )[0]

    pozitive = [f for f in facturi_valide if _este_factura_pozitiva_nova(f)]
    if pozitive:
        return sorted(pozitive, key=lambda f: _data_emitere_factura_nova(f) or date.min, reverse=True)[0]

    return sorted(facturi_valide, key=lambda f: _data_emitere_factura_nova(f) or date.min, reverse=True)[0]


def _valoare_afisata_factura_nova(factura: dict[str, Any] | None) -> float | None:
    valoare = _valoare_factura_nova(factura)
    rest = _rest_plata_factura_nova(factura)
    if (valoare is None or valoare <= 0) and rest is not None and rest > 0:
        return rest
    return valoare


def _deduce_stare_factura(factura: dict[str, Any], rest_plata: float | None) -> str:
    status_brut = str(factura.get("status") or factura.get("paymentStatus") or "").lower()
    if any(text in status_brut for text in ("reversed", "storno", "cancel")):
        return "stornata"
    if "paid" in status_brut or "plat" in status_brut:
        return "platita"
    if rest_plata is not None and rest_plata <= 0:
        return "platita"
    if rest_plata is not None and rest_plata > 0:
        data_scadenta = _data_sigura(factura.get("dueDate") or factura.get("dueAt"))
        if data_scadenta and data_scadenta < date.today():
            return "scadenta"
        return "neplatita"
    return status_brut or "necunoscuta"


def _deduce_categorie_factura(factura: dict[str, Any]) -> str:
    text = " ".join(str(factura.get(camp) or "") for camp in ["type", "title", "category", "description", "invoiceType"]).lower()
    valoare = _float_sigur(
        factura.get("amountTotal")
        or factura.get("value")
        or factura.get("invoiceValue")
        or factura.get("total")
        or factura.get("amount")
        or factura.get("totalAmount")
    )
    if "inject" in text or "prosum" in text or "compens" in text or "sold" in text:
        return "injectie"
    if valoare is not None and valoare < 0:
        return "injectie"
    return "consum"
