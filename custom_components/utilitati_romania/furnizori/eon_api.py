"""Client API pentru comunicarea cu E.ON România."""

from __future__ import annotations

# Portions of this file are derived from open-source Home Assistant custom
# integrations originally authored by Cristian Necrea and published under the MIT License.
#
# Copyright (c) Cristian Necrea
# Copyright (c) Marius Onițiu
#
# Licensed under the MIT License. See the LICENSE file in this repository.

import asyncio
import json
import logging
import time

from aiohttp import ClientSession, ClientTimeout

from .eon_const import (
    API_TIMEOUT,
    AUTH_VERIFY_SECRET,
    HEADERS,
    MFA_REQUIRED_CODE,
    TOKEN_MAX_AGE,
    TOKEN_REFRESH_THRESHOLD,
    URL_CONSUMPTION_CONVENTION,
    URL_CONTRACT_DETAILS,
    URL_CONTRACTS_DETAILS_LIST,
    URL_CONTRACTS_LIST,
    URL_CONTRACTS_WITH_SUBCONTRACTS,
    URL_GRAPHIC_CONSUMPTION,
    URL_INVOICE_BALANCE,
    URL_INVOICE_BALANCE_PROSUM,
    URL_INVOICES_PROSUM,
    URL_INVOICES_UNPAID,
    URL_LOGIN,
    URL_METER_HISTORY,
    URL_METER_INDEX,
    URL_METER_SUBMIT,
    URL_MFA_LOGIN,
    URL_MFA_RESEND,
    URL_PAYMENT_LIST,
    URL_REFRESH_TOKEN,
    URL_RESCHEDULING_PLANS,
    URL_USER_DETAILS,
)
from .eon_helper import generate_verify_hmac

_LOGGER = logging.getLogger(__name__)

URL_INVOICES_PAID = "https://api2.eon.ro/invoices/v1/invoices/list-paid"


def _mask_email(value: str) -> str:
    """Maschează adresa de email pentru afișare în MFA."""
    value = (value or "").strip()
    if "@" not in value:
        return value or "email"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        local_masked = local[0] + "*" * max(1, len(local) - 1)
    else:
        local_masked = local[:2] + "*" * max(2, len(local) - 2)
    return f"{local_masked}@{domain}"


class EonApiClient:
    """Client pentru API-ul E.ON România."""

    def __init__(self, session: ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password

        self._access_token: str | None = None
        self._token_type: str = "Bearer"
        self._expires_in: int = 3600
        self._refresh_token: str | None = None
        self._id_token: str | None = None
        self._uuid: str | None = None
        self._token_obtained_at: float = 0.0

        self._timeout = ClientTimeout(total=API_TIMEOUT)
        self._auth_lock = asyncio.Lock()
        self._token_generation: int = 0

        self._mfa_data: dict | None = None
        self._mfa_blocked: bool = False

    @property
    def has_token(self) -> bool:
        return self._access_token is not None

    @property
    def uuid(self) -> str | None:
        return self._uuid

    @property
    def mfa_required(self) -> bool:
        return self._mfa_data is not None

    @property
    def mfa_data(self) -> dict | None:
        return self._mfa_data

    @property
    def pending_email_masked(self) -> str:
        """Destinatar mascat pentru pasul MFA din config flow."""
        if not self._mfa_data:
            return _mask_email(self._username)
        mfa_type = str(self._mfa_data.get("type") or "").upper()
        recipient = str(self._mfa_data.get("recipient") or "").strip()
        if mfa_type == "EMAIL":
            return recipient or _mask_email(self._username)
        return recipient or "email"

    @property
    def mfa_blocked(self) -> bool:
        return self._mfa_blocked

    def clear_mfa_block(self) -> None:
        self._mfa_blocked = False
        self._mfa_data = None
        _LOGGER.debug("[AUTH] Blocaj MFA resetat.")

    def is_token_likely_valid(self) -> bool:
        if self._access_token is None:
            return False
        age = time.monotonic() - self._token_obtained_at
        effective_max = (
            self._expires_in - TOKEN_REFRESH_THRESHOLD
            if self._expires_in > TOKEN_REFRESH_THRESHOLD
            else TOKEN_MAX_AGE
        )
        return age < effective_max

    def export_token_data(self) -> dict | None:
        if self._access_token is None:
            return None
        return {
            "access_token": self._access_token,
            "token_type": self._token_type,
            "expires_in": self._expires_in,
            "refresh_token": self._refresh_token,
            "id_token": self._id_token,
            "uuid": self._uuid,
            "obtained_at_wallclock": time.time() - (time.monotonic() - self._token_obtained_at),
        }

    def inject_token(self, token_data: dict) -> None:
        self._access_token = token_data.get("access_token")
        self._token_type = token_data.get("token_type", "Bearer")
        self._expires_in = token_data.get("expires_in", 3600)
        self._refresh_token = token_data.get("refresh_token")
        self._id_token = token_data.get("id_token")
        self._uuid = token_data.get("uuid")

        wallclock_obtained = token_data.get("obtained_at_wallclock")
        if wallclock_obtained:
            age_seconds = time.time() - wallclock_obtained
            if age_seconds < 0:
                age_seconds = 0
            self._token_obtained_at = time.monotonic() - age_seconds
            _LOGGER.debug(
                "Token injectat cu vârstă reală: %.0fs (expires_in=%s).",
                age_seconds,
                self._expires_in,
            )
        else:
            self._token_obtained_at = 0.0
            _LOGGER.debug(
                "Token injectat fără wallclock — se va face refresh la prima cerere."
            )

        self._token_generation += 1
        self._mfa_blocked = False
        self._mfa_data = None
        _LOGGER.debug(
            "Token injectat (access=%s..., refresh=%s, gen=%s, valid=%s).",
            f"***({len(self._access_token)}ch)" if self._access_token else "None",
            "da" if self._refresh_token else "nu",
            self._token_generation,
            self.is_token_likely_valid(),
        )

    async def async_login(self) -> bool:
        self._mfa_data = None

        verify = generate_verify_hmac(self._username, AUTH_VERIFY_SECRET)
        payload = {
            "username": self._username,
            "password": self._password,
            "verify": verify,
        }

        _LOGGER.debug("[LOGIN] Trimitere cerere: URL=%s, user=%s", URL_LOGIN, self._username)

        try:
            async with self._session.post(
                URL_LOGIN, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[LOGIN] Răspuns: Status=%s", resp.status)

                data: dict[str, object] = {}
                if response_text:
                    try:
                        parsed = json.loads(response_text)
                        if isinstance(parsed, dict):
                            data = parsed
                    except (json.JSONDecodeError, ValueError):
                        data = {}

                if resp.status == 200:
                    if not data:
                        _LOGGER.error("[LOGIN] Răspuns 200 fără JSON valid: %s", response_text[:1000])
                        self._invalidate_tokens()
                        return False
                    self._apply_token_data(data)
                    _LOGGER.debug("[LOGIN] Token obținut cu succes (expires_in=%s).", self._expires_in)
                    return True

                if resp.status == 400:
                    if str(data.get("code")) == MFA_REQUIRED_CODE:
                        second_factor_type = str(data.get("secondFactorType") or "EMAIL").upper()
                        recipient = str(data.get("secondFactorRecipient") or "").strip()
                        if second_factor_type == "EMAIL" and not recipient:
                            recipient = _mask_email(self._username)
                        self._mfa_data = {
                            "uuid": data.get("description"),
                            "type": second_factor_type,
                            "alternative_type": str(data.get("secondFactorAlternativeType") or "SMS").upper(),
                            "recipient": recipient,
                            "validity": data.get("secondFactorValidity", 60),
                        }
                        _LOGGER.warning(
                            "[LOGIN] MFA necesar. Tip=%s, Destinatar=%s, Valabilitate=%ss.",
                            self._mfa_data["type"],
                            self._mfa_data["recipient"],
                            self._mfa_data["validity"],
                        )
                        return False

                    _LOGGER.error("[LOGIN DEBUG] 400 RAW: %s", response_text[:1000])

                _LOGGER.error(
                    "[LOGIN] Eroare autentificare. Cod HTTP=%s, Răspuns=%s",
                    resp.status,
                    response_text[:1000],
                )
                self._invalidate_tokens()
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[LOGIN] Depășire de timp.")
            self._invalidate_tokens()
            return False
        except Exception:
            _LOGGER.exception("[LOGIN] Eroare neașteptată la autentificare.")
            self._invalidate_tokens()
            return False

    async def async_mfa_complete(self, code: str) -> bool:
        if not self._mfa_data or not self._mfa_data.get("uuid"):
            _LOGGER.error("[MFA] Nu există sesiune MFA activă.")
            return False

        payload = {
            "uuid": self._mfa_data["uuid"],
            "code": code,
            "interval": None,
            "type": None,
        }

        try:
            async with self._session.post(
                URL_MFA_LOGIN, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[MFA] Răspuns: Status=%s", resp.status)

                if resp.status == 200:
                    data = json.loads(response_text)
                    access_token = data.get("access_token")
                    if access_token:
                        self._apply_token_data(data)
                        self._mfa_data = None
                        _LOGGER.debug("[MFA] Login 2FA reușit.")
                        return True

                _LOGGER.error(
                    "[MFA] Autentificare 2FA eșuată. Cod HTTP=%s, Răspuns=%s",
                    resp.status,
                    response_text,
                )
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[MFA] Depășire de timp.")
            return False
        except Exception as e:
            _LOGGER.error("[MFA] Eroare: %s", e)
            return False

    async def async_mfa_resend(self, mfa_type: str | None = None) -> bool:
        if not self._mfa_data or not self._mfa_data.get("uuid"):
            _LOGGER.error("[MFA-RESEND] Nu există sesiune MFA activă.")
            return False

        send_type = mfa_type or self._mfa_data.get("type", "EMAIL")
        payload = {
            "uuid": self._mfa_data["uuid"],
            "secondFactorValidity": None,
            "type": send_type,
            "action": "AUTHORIZATION",
            "recipient": None,
        }

        try:
            async with self._session.post(
                URL_MFA_RESEND, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[MFA-RESEND] Status=%s, Body=%s", resp.status, response_text)

                if resp.status == 200:
                    try:
                        data = json.loads(response_text)
                    except (json.JSONDecodeError, ValueError):
                        data = {}

                    new_uuid = data.get("uuid")
                    if new_uuid:
                        self._mfa_data["uuid"] = new_uuid
                    new_recipient = data.get("recipient")
                    if new_recipient:
                        self._mfa_data["recipient"] = new_recipient
                    return True

                _LOGGER.error(
                    "[MFA-RESEND] Retransmitere eșuată. Cod HTTP=%s, Răspuns=%s",
                    resp.status,
                    response_text,
                )
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[MFA-RESEND] Depășire de timp.")
            return False
        except Exception as e:
            _LOGGER.error("[MFA-RESEND] Eroare: %s", e)
            return False

    async def async_refresh_token(self) -> bool:
        if not self._refresh_token:
            _LOGGER.debug("[REFRESH] Nu există refresh_token.")
            return False

        payload = {"refreshToken": self._refresh_token}

        try:
            async with self._session.post(
                URL_REFRESH_TOKEN, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[REFRESH] Răspuns: Status=%s", resp.status)

                if resp.status == 200:
                    data = json.loads(response_text)
                    self._apply_token_data(data)
                    _LOGGER.debug("[REFRESH] Token reîmprospătat cu succes.")
                    return True

                response_text = await resp.text()
                _LOGGER.warning(
                    "[REFRESH] Eroare la reîmprospătare. Cod HTTP=%s, Răspuns=%s",
                    resp.status,
                    response_text[:1000],
                )
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[REFRESH] Depășire de timp.")
            return False
        except Exception as e:
            _LOGGER.error("[REFRESH] Eroare: %s", e)
            return False

    def _apply_token_data(self, data: dict) -> None:
        self._access_token = data.get("access_token")
        self._token_type = data.get("token_type", "Bearer")
        self._expires_in = data.get("expires_in", 3600)
        self._refresh_token = data.get("refresh_token")
        self._id_token = data.get("idToken")
        self._uuid = data.get("uuid")
        self._token_obtained_at = time.monotonic()
        self._token_generation += 1

    def invalidate_token(self) -> None:
        self._access_token = None
        self._token_obtained_at = 0.0

    def _invalidate_tokens(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._id_token = None
        self._uuid = None
        self._token_obtained_at = 0.0

    async def async_ensure_authenticated(self) -> bool:
        return await self._ensure_token_valid()

    async def _ensure_token_valid(self) -> bool:
        if self.is_token_likely_valid():
            return True

        if self._mfa_blocked:
            _LOGGER.debug("[AUTH] Login blocat — MFA necesar.")
            return False

        async with self._auth_lock:
            if self.is_token_likely_valid():
                return True

            if self._mfa_blocked:
                return False

            if self._refresh_token:
                if await self.async_refresh_token():
                    return True
                _LOGGER.debug("[AUTH] Refresh eșuat. Se încearcă login complet.")

            self._invalidate_tokens()
            result = await self.async_login()

            if not result and self._mfa_data is not None:
                self._mfa_blocked = True
                _LOGGER.error("[AUTH] MFA necesar — reconfigurare necesară.")

            return result

    async def async_fetch_user_details(self):
        return await self._request_with_token("GET", URL_USER_DETAILS, "user_details")

    async def async_fetch_contracts_list(
        self,
        partner_code: str | None = None,
        collective_contract: str | None = None,
        limit: int | None = None,
    ):
        params = {}
        if partner_code:
            params["partnerCode"] = partner_code
        if collective_contract:
            params["collectiveContract"] = collective_contract
        if limit is not None:
            params["limit"] = str(limit)

        url = URL_CONTRACTS_LIST
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"

        return await self._request_with_token("GET", url, "contracts_list")

    async def async_fetch_contract_details(self, account_contract: str, include_meter_reading: bool = True):
        url = URL_CONTRACT_DETAILS.format(accountContract=account_contract)
        if include_meter_reading:
            url = f"{url}?includeMeterReading=true"
        return await self._request_with_token("GET", url, f"contract_details ({account_contract})")

    async def async_fetch_contracts_with_subcontracts(self, account_contract: str | None = None):
        url = URL_CONTRACTS_WITH_SUBCONTRACTS
        label = f"contracts_with_subcontracts ({account_contract or 'all'})"
        return await self._request_with_token("GET", url, label)

    async def async_fetch_contracts_details_list(self, account_contracts: list[str]):
        if not account_contracts:
            return None
        payload = {
            "accountContracts": account_contracts,
            "includeMeterReading": True,
        }
        return await self._request_with_token_post(
            URL_CONTRACTS_DETAILS_LIST,
            payload,
            f"contracts_details_list ({len(account_contracts)} subcontracte)",
        )

    async def async_fetch_invoices_unpaid(self, account_contract: str, include_subcontracts: bool = False):
        params = f"?accountContract={account_contract}&status=unpaid"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        return await self._request_with_token(
            "GET",
            f"{URL_INVOICES_UNPAID}{params}",
            f"invoices_unpaid ({account_contract})",
        )


    async def async_fetch_invoices_paid(self, account_contract: str, max_pages: int | None = None):
        return await self._paginated_request(
            base_url=URL_INVOICES_PAID,
            params={"accountContract": account_contract, "status": "paid"},
            list_key="list",
            label=f"invoices_paid ({account_contract})",
            max_pages=max_pages,
        )

    async def async_fetch_invoices_prosum(self, account_contract: str, max_pages: int | None = None):
        return await self._paginated_request(
            base_url=URL_INVOICES_PROSUM,
            params={"accountContract": account_contract},
            list_key="list",
            label=f"invoices_prosum ({account_contract})",
            max_pages=max_pages,
        )

    async def async_fetch_invoice_balance(self, account_contract: str, include_subcontracts: bool = False):
        params = f"?accountContract={account_contract}"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        return await self._request_with_token(
            "GET",
            f"{URL_INVOICE_BALANCE}{params}",
            f"invoice_balance ({account_contract})",
        )

    async def async_fetch_invoice_balance_prosum(self, account_contract: str, include_subcontracts: bool = False):
        params = f"?accountContract={account_contract}"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        return await self._request_with_token(
            "GET",
            f"{URL_INVOICE_BALANCE_PROSUM}{params}",
            f"invoice_balance_prosum ({account_contract})",
        )

    async def async_fetch_payments(self, account_contract: str, max_pages: int | None = None):
        return await self._paginated_request(
            base_url=URL_PAYMENT_LIST,
            params={"accountContract": account_contract},
            list_key="list",
            label=f"payments ({account_contract})",
            max_pages=max_pages,
        )

    async def async_fetch_rescheduling_plans(
        self,
        account_contract: str,
        include_subcontracts: bool = False,
        status: str | None = None,
    ):
        params = f"?accountContract={account_contract}"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        if status:
            params += f"&status={status}"
        return await self._request_with_token(
            "GET",
            f"{URL_RESCHEDULING_PLANS}{params}",
            f"rescheduling_plans ({account_contract})",
        )

    async def async_fetch_graphic_consumption(self, account_contract: str):
        url = URL_GRAPHIC_CONSUMPTION.format(accountContract=account_contract)
        return await self._request_with_token("GET", url, f"graphic_consumption ({account_contract})")

    async def async_fetch_meter_index(self, account_contract: str):
        url = URL_METER_INDEX.format(accountContract=account_contract)
        return await self._request_with_token("GET", url, f"meter_index ({account_contract})")

    async def async_fetch_meter_history(self, account_contract: str):
        url = URL_METER_HISTORY.format(accountContract=account_contract)
        return await self._request_with_token("GET", url, f"meter_history ({account_contract})")

    async def async_fetch_consumption_convention(self, account_contract: str):
        url = URL_CONSUMPTION_CONVENTION.format(accountContract=account_contract)
        return await self._request_with_token("GET", url, f"consumption_convention ({account_contract})")

    async def async_submit_meter_index(self, account_contract: str, indexes: list[dict]):
        label = f"submit_meter ({account_contract})"

        if not account_contract or not indexes:
            _LOGGER.error("[%s] Parametri invalizi.", label)
            return None

        payload = {
            "accountContract": account_contract,
            "channel": "MOBILE",
            "indexes": indexes,
        }

        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Token invalid.", label)
            return None

        gen_before = self._token_generation
        headers = {**HEADERS, "Authorization": f"{self._token_type} {self._access_token}"}

        try:
            async with self._session.post(
                URL_METER_SUBMIT,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                response_text = await resp.text()

                if resp.status == 200:
                    return json.loads(response_text)

                if resp.status == 401:
                    if self._token_generation != gen_before:
                        _LOGGER.debug("[%s] Token reînnoit de alt apel. Retry.", label)
                    else:
                        self.invalidate_token()
                        if not await self._ensure_token_valid():
                            return None

                    headers_retry = {**HEADERS, "Authorization": f"{self._token_type} {self._access_token}"}
                    async with self._session.post(
                        URL_METER_SUBMIT,
                        json=payload,
                        headers=headers_retry,
                        timeout=self._timeout,
                    ) as resp_retry:
                        response_text_retry = await resp_retry.text()
                        if resp_retry.status == 200:
                            return json.loads(response_text_retry)
                        return None

                _LOGGER.error("[%s] Eroare HTTP=%s, Body=%s", label, resp.status, response_text)
                return None

        except asyncio.TimeoutError:
            _LOGGER.error("[%s] Depășire de timp.", label)
            return None
        except Exception as e:
            _LOGGER.exception("[%s] Eroare: %s", label, e)
            return None

    async def _request_with_token(self, method: str, url: str, label: str = "request"):
        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Nu s-a putut obține un token valid.", label)
            return None

        gen_before = self._token_generation

        resp_data, status = await self._do_request(method, url, label)
        if status != 401:
            return resp_data

        if self._token_generation != gen_before:
            _LOGGER.debug("[%s] 401 dar tokenul a fost deja reînnoit. Retry.", label)
        else:
            self.invalidate_token()
            if not await self._ensure_token_valid():
                _LOGGER.error("[%s] Reautentificare eșuată.", label)
                return None

        resp_data, status = await self._do_request(method, url, label)
        if status == 401:
            _LOGGER.error("[%s] A doua încercare a eșuat cu 401.", label)
            return None

        return resp_data

    async def _request_with_token_post(self, url: str, payload, label: str = "request_post"):
        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Nu s-a putut obține un token valid.", label)
            return None

        gen_before = self._token_generation

        resp_data, status = await self._do_request("POST", url, label, json_payload=payload)
        if status != 401:
            return resp_data

        if self._token_generation != gen_before:
            _LOGGER.debug("[%s] 401 dar tokenul a fost deja reînnoit. Retry.", label)
        else:
            self.invalidate_token()
            if not await self._ensure_token_valid():
                _LOGGER.error("[%s] Reautentificare eșuată.", label)
                return None

        resp_data, status = await self._do_request("POST", url, label, json_payload=payload)
        if status == 401:
            _LOGGER.error("[%s] A doua încercare a eșuat cu 401.", label)
            return None

        return resp_data

    async def _do_request(self, method: str, url: str, label: str = "request", json_payload=None):
        headers = {**HEADERS}
        if self._access_token:
            headers["Authorization"] = f"{self._token_type} {self._access_token}"

        try:
            kwargs = {"headers": headers, "timeout": self._timeout}
            if json_payload is not None:
                kwargs["json"] = json_payload

            async with self._session.request(method, url, **kwargs) as resp:
                response_text = await resp.text()

                if resp.status == 200:
                    try:
                        return json.loads(response_text), resp.status
                    except Exception:
                        return await resp.json(), resp.status

                _LOGGER.error("[%s] Eroare %s %s -> HTTP=%s, Body=%s", label, method, url, resp.status, response_text)
                return None, resp.status

        except asyncio.TimeoutError:
            _LOGGER.error("[%s] Depășire de timp: %s %s.", label, method, url)
            return None, 0
        except Exception as e:
            _LOGGER.error("[%s] Eroare: %s %s -> %s", label, method, url, e)
            return None, 0

    async def _paginated_request(
        self,
        base_url: str,
        params: dict,
        list_key: str = "list",
        label: str = "paginated",
        max_pages: int | None = None,
    ):
        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Nu s-a putut obține un token valid.", label)
            return None

        results: list = []
        page = 1
        retried = False

        while True:
            query_parts = [f"{k}={v}" for k, v in params.items()]
            query_parts.append(f"page={page}")
            url = f"{base_url}?{'&'.join(query_parts)}"

            gen_before = self._token_generation
            headers = {**HEADERS, "Authorization": f"{self._token_type} {self._access_token}"}

            try:
                async with self._session.get(
                    url, headers=headers, timeout=self._timeout
                ) as resp:
                    response_text = await resp.text()

                    if resp.status == 200:
                        data = json.loads(response_text)
                        chunk = data.get(list_key, [])
                        results.extend(chunk)
                        retried = False

                        has_next = data.get("hasNext", False)
                        if not has_next:
                            break
                        if max_pages is not None and page >= max_pages:
                            break
                        page += 1
                        continue

                    if resp.status == 401 and not retried:
                        if self._token_generation != gen_before:
                            _LOGGER.debug("[%s] Token reînnoit de alt apel. Retry pagină %s.", label, page)
                        else:
                            self.invalidate_token()
                            if not await self._ensure_token_valid():
                                return results if results else None
                        retried = True
                        continue

                    _LOGGER.error("[%s] Eroare HTTP=%s la pagina %s, Body=%s", label, resp.status, page, response_text)
                    break

            except asyncio.TimeoutError:
                _LOGGER.error("[%s] Depășire de timp la pagina %s.", label, page)
                break
            except Exception as e:
                _LOGGER.error("[%s] Eroare: %s", label, e)
                break

        return results