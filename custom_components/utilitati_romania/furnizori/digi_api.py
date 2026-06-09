from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from ..const import (
    ADDRESS_CONFIRM_URL,
    ADDRESS_SELECT_URL,
    BASE_URL,
    INVOICES_URL,
    LOGIN_URL,
    TWO_FA_SEND_URL,
    TWO_FA_URL,
    TWO_FA_VALIDATE_URL,
    USER_AGENT,
)
from .digi_models import AddressInvoices, DigiData, InvoiceDetail, InvoiceSummary

_LOGGER = logging.getLogger(__name__)



def _digi_mask_token(value: Any, *, visible: int = 4) -> str | None:
    """Mascheaza un identificator Digi pastrand doar suficient pentru corelare in log."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    if len(text) <= visible * 2:
        return "***"
    return f"{text[:visible]}…{text[-visible:]}"


def _digi_mask_text(value: Any) -> str | None:
    """Mascheaza textul care poate contine numere de telefon sau coduri client."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return ""
    text = re.sub(r"\b\d{3,}\b", "***", text)
    if len(text) > 80:
        text = f"{text[:77]}..."
    return text


def _digi_stable_anon_label(prefix: str, value: Any) -> str | None:
    """Genereaza o eticheta anonima stabila pentru corelarea elementelor in log."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return ""

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def _digi_mask_address(value: Any) -> str | None:
    """Anonimizeaza complet adresa, pastrand doar un indiciu tehnic stabil pentru diagnostic."""
    return _digi_stable_anon_label("adresa_anonimizata", value)


def _digi_mask_address_key(value: Any) -> str | None:
    """Anonimizeaza cheia interna de adresa, deoarece poate contine fragmente din adresa reala."""
    return _digi_stable_anon_label("cheie_adresa_anonimizata", value)


def _digi_mask_service_name(value: Any) -> str | None:
    """Mascheaza denumirea serviciului fara a ascunde complet tipul serviciului."""
    return _digi_mask_text(value)


def _digi_safe_invoice_debug(item: dict[str, Any]) -> dict[str, Any]:
    """Construieste o reprezentare sigura pentru logurile de diagnostic Digi."""
    services = item.get("services") or []
    if not isinstance(services, list):
        services = []

    return {
        "invoice_id": _digi_mask_token(item.get("invoice_id")),
        "invoice_number": _digi_mask_token(item.get("invoice_number")),
        "issue_date": item.get("issue_date"),
        "due_date": item.get("due_date"),
        "amount": item.get("amount"),
        "rest": item.get("rest"),
        "status": _digi_mask_text(item.get("status")),
        "description": _digi_mask_text(item.get("description")),
        "services_count": len(services),
        "services": [
            {
                "name": _digi_mask_service_name(service.get("name") if isinstance(service, dict) else service),
                "amount": service.get("amount") if isinstance(service, dict) else None,
            }
            for service in services[:10]
        ],
    }


def _digi_safe_row_debug(row: InvoiceSummary) -> dict[str, Any]:
    """Construieste o reprezentare sigura pentru randurile extrase din pagina de facturi."""
    return {
        "invoice_id": _digi_mask_token(row.invoice_id),
        "address_key": _digi_mask_address_key(row.address_key),
        "address": _digi_mask_address(row.address),
        "issue_date": row.issue_date,
        "due_date": row.due_date,
        "description": _digi_mask_text(row.description),
        "amount": row.amount,
    }

RE_INPUT_TAG = re.compile(r"<input[^>]*>", re.I | re.S)
RE_LABEL_FOR = re.compile(
    r'<label[^>]+for=["\']([^"\']+)["\'][^>]*>(.*?)</label>',
    re.I | re.S,
)
RE_ADDRESS_OPTION = re.compile(
    r'<option[^>]+id=["\'](address-[^"\']+)["\'][^>]*>(.*?)</option>',
    re.I | re.S,
)
RE_SCRIPT_CFG = re.compile(
    r'<script[^>]+id=["\']client-invoices-cfg["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)

RE_ROW = re.compile(
    r'<div class=["\']my-account-tbl-row["\'][^>]*data-invoice-address=["\']([^"\']+)["\'][^>]*>\s*'
    r'<div class=["\']my-account-tbl-col date["\']>\s*(.*?)\s*</div>\s*'
    r'<div class=["\']my-account-tbl-col description["\']>\s*(.*?)\s*<span>\s*(.*?)\s*</span>\s*</div>\s*'
    r'<div class=["\']my-account-tbl-col amount["\']>\s*(.*?)\s*</div>',
    re.I | re.S,
)

RE_CURRENT_ROW = re.compile(
    r'<div class=["\']my-account-tbl-row["\'][^>]*data-invoice-address=["\']([^"\']+)["\'][^>]*>\s*'
    r'<div class=["\']my-account-tbl-col select check["\']>\s*'
    r'<button[^>]*data-invoices-id=["\'](\d+)["\'][^>]*>.*?</button>\s*</div>\s*'
    r'<div class=["\']my-account-tbl-col date["\']>\s*(.*?)\s*</div>\s*'
    r'<div class=["\']my-account-tbl-col description["\']>\s*(.*?)\s*<span>\s*(.*?)\s*</span>\s*</div>\s*'
    r'<div class=["\']my-account-tbl-col amount["\']>\s*(.*?)\s*</div>',
    re.I | re.S,
)

RE_DETAILS_TITLE = re.compile(
    r"Factura\s+([^<]+?)\s+din data de\s+([0-9.\-/]+)",
    re.I | re.S,
)
RE_PDF = re.compile(
    r'href=["\']([^"\']*?/my-account/invoices/pdf-download[^"\']+)["\']',
    re.I,
)
RE_SERVICE_ROW = re.compile(
    r'<div class=["\']popup-content-item["\']>\s*<div class=["\']name["\']>\s*(.*?)\s*</div>\s*'
    r'<div class=["\']price["\']>\s*(.*?)\s*</div>',
    re.I | re.S,
)
RE_HEX32 = re.compile(r"\b[a-f0-9]{32}\b", re.I)
RE_PHONE_PARAM = re.compile(
    r'(?:phone|form-phone-number-confirm|phone-number-confirm)[^a-f0-9]{0,40}([a-f0-9]{32})',
    re.I | re.S,
)
RE_SELECT_BLOCK = re.compile(
    r'<select[^>]*(?:id|name)=["\']([^"\']+)["\'][^>]*>(.*?)</select>',
    re.I | re.S,
)
RE_OPTION_TAG = re.compile(
    r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>(.*?)</option>',
    re.I | re.S,
)

RE_LABEL_VALUE_MONEY = re.compile(
    r'>\s*(Total|Rest)\s*<.*?>\s*([0-9]+(?:(?:[.,]|&period;)[0-9]{2})?)\s*LEI',
    re.I | re.S,
)
RE_LABEL_VALUE_TEXT = re.compile(
    r'>\s*Status\s*<.*?>\s*([^<]+)',
    re.I | re.S,
)


class DigiError(Exception):
    """Base Digi exception."""


class DigiAuthError(DigiError):
    """Credentials invalid."""


class DigiTwoFactorRequired(DigiError):
    """2FA step required."""


class DigiTwoFactorError(DigiError):
    """2FA validation failed."""


class DigiAccountSelectionRequired(DigiError):
    """Account selection is needed."""


class DigiReauthRequired(DigiError):
    """Saved session expired."""


@dataclass(slots=True)
class TwoFactorOption:
    value: str
    label: str


@dataclass(slots=True)
class TwoFactorContext:
    methods: dict[str, dict[str, Any]]
    html: str
    selections: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AddressOption:
    value: str
    label: str


class DigiApiClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        connector = session.connector
        if connector is None:
            raise DigiError("HTTP session connector is unavailable")

        self._session = aiohttp.ClientSession(
            connector=connector,
            connector_owner=False,
            cookie_jar=aiohttp.CookieJar(),
            timeout=session.timeout,
        )
        self._default_headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": BASE_URL,
            "Origin": BASE_URL,
        }

    async def close(self) -> None:
        if not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, url: str, **kwargs: Any) -> aiohttp.ClientResponse:
        headers = dict(self._default_headers)
        headers.update(kwargs.pop("headers", {}))
        response = await self._session.request(method, url, headers=headers, **kwargs)
        return response

    async def _read_text(self, response: aiohttp.ClientResponse) -> str:
        return await response.text(errors="ignore")

    def export_cookies(self) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        for cookie in self._session.cookie_jar:
            cookies.append(
                {
                    "key": cookie.key,
                    "value": cookie.value,
                    "domain": cookie["domain"],
                    "path": cookie["path"],
                    "secure": bool(cookie["secure"]),
                    "expires": cookie["expires"],
                }
            )
        return cookies

    def import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        jar = self._session.cookie_jar
        jar.clear()

        if not cookies:
            return

        for item in cookies:
            domain = str(item.get("domain", "")).strip()
            key = str(item.get("key", "")).strip()
            value = str(item.get("value", ""))

            if not domain or not key:
                continue

            morsel = {key: value}
            jar.update_cookies(
                morsel,
                response_url=URL(f"https://{domain.lstrip('.')}"),
            )

    async def begin_login(self, email: str, password: str) -> tuple[str, str]:
        self._session.cookie_jar.clear()

        payload = {
            "signin-input-app": "0",
            "signin-input-email": email,
            "signin-input-password": password,
            "signin-submit-button": "",
        }
        resp = await self._request(
            "POST",
            LOGIN_URL,
            data=payload,
            allow_redirects=True,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        text = await self._read_text(resp)
        final_url = str(resp.url)

        if "auth/login" in final_url and "2fa" not in final_url:
            raise DigiAuthError("Invalid credentials")
        if "/auth/2fa" in final_url:
            return final_url, text
        if "/auth/address-select" in final_url:
            return final_url, text
        if "/my-account" in final_url or final_url.rstrip("/") == BASE_URL:
            return final_url, text

        return final_url, text

    async def login(self, email: str, password: str) -> tuple[str, str]:
        return await self.begin_login(email, password)

    async def get_2fa_context(self, html: str | None = None) -> TwoFactorContext:
        if html is None:
            resp = await self._request("GET", TWO_FA_URL, allow_redirects=True)
            html = await self._read_text(resp)

        methods = self._parse_2fa_context(html)
        if not methods:
            _LOGGER.debug("Digi 2FA HTML first 1500 chars: %s", html[:1500])
            raise DigiTwoFactorRequired("Could not parse 2FA page")

        return TwoFactorContext(methods=methods, html=html)

    @staticmethod
    def _parse_attrs(tag: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        pattern = r'(\w+(?:-\w+)*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))'
        for key, value1, value2, value3 in re.findall(pattern, tag, re.I):
            attrs[key.lower()] = value1 or value2 or value3 or ""
        return attrs

    def _extract_hidden_inputs(self, html: str) -> dict[str, str]:
        hidden: dict[str, str] = {}
        for tag in RE_INPUT_TAG.findall(html):
            attrs = self._parse_attrs(tag)
            if attrs.get("type", "").lower() != "hidden":
                continue
            name = attrs.get("name")
            if name:
                hidden[name] = attrs.get("value", "")
        return hidden

    def _extract_select_options(self, html: str, *candidate_names: str) -> list[TwoFactorOption]:
        candidates = {name.lower() for name in candidate_names if name}
        options: list[TwoFactorOption] = []

        for select_name, select_body in RE_SELECT_BLOCK.findall(html):
            name_l = select_name.lower()
            if candidates and name_l not in candidates:
                continue

            for value, label_html in RE_OPTION_TAG.findall(select_body):
                clean_value = (value or "").strip()
                clean_label = self._clean_text(label_html)
                if not clean_value or not clean_label:
                    continue
                options.append(TwoFactorOption(value=clean_value, label=clean_label))

        deduped: list[TwoFactorOption] = []
        seen: set[tuple[str, str]] = set()
        for option in options:
            key = (option.value, option.label)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(option)

        return deduped

    def _extract_radio_options(self, html: str) -> list[AddressOption]:
        labels = {key: self._clean_text(val) for key, val in RE_LABEL_FOR.findall(html)}
        options: list[AddressOption] = []

        for tag in RE_INPUT_TAG.findall(html):
            attrs = self._parse_attrs(tag)
            if attrs.get("type", "").lower() != "radio":
                continue
            input_id = attrs.get("id", "")
            value = attrs.get("value", "")
            label = labels.get(input_id, "")
            if value and label:
                options.append(AddressOption(value=value, label=label))

        return options

    def _parse_2fa_context(self, html: str) -> dict[str, dict[str, Any]]:
        methods: dict[str, dict[str, Any]] = {}
        hidden = self._extract_hidden_inputs(html)
        html_lower = html.lower()

        phone_value: str | None = None

        for key in (
            "form-phone-number-confirm",
            "phone",
            "phone-number-confirm",
            "form_phone_number_confirm",
        ):
            value = hidden.get(key)
            if value and RE_HEX32.fullmatch(value):
                phone_value = value
                break

        if not phone_value:
            for key, value in hidden.items():
                key_l = key.lower()
                if ("phone" in key_l or "telefon" in key_l) and value and RE_HEX32.fullmatch(value):
                    phone_value = value
                    break

        if not phone_value:
            match = RE_PHONE_PARAM.search(html)
            if match:
                phone_value = match.group(1)

        sms_candidates = self._extract_select_options(
            html,
            "form-my-account-2fa-send-phone",
            "phone",
            "phone-number-confirm",
            "form-phone-number-confirm",
        )

        if not phone_value and (
            "trimite sms" in html_lower
            or "codul primit prin sms" in html_lower
            or "cod de siguranță prin sms" in html_lower
            or "cod de siguranta prin sms" in html_lower
        ):
            tokens = list(dict.fromkeys(RE_HEX32.findall(html)))
            if len(tokens) == 1:
                phone_value = tokens[0]

        if phone_value or sms_candidates:
            sms_method: dict[str, Any] = {
                "send_url": TWO_FA_SEND_URL,
                "send_payload": {
                    "action": "myAccount2FASend",
                },
                "validate_payload": {
                    "action": "myAccount2FAVerify",
                },
            }
            if phone_value:
                sms_method["default_target"] = phone_value
            if sms_candidates:
                sms_method["target_options"] = [
                    {"value": option.value, "label": option.label} for option in sms_candidates
                ]
            methods["sms"] = sms_method
        elif (
            "trimite sms" in html_lower
            or "codul primit prin sms" in html_lower
            or "cod de siguranță prin sms" in html_lower
            or "cod de siguranta prin sms" in html_lower
        ):
            _LOGGER.debug(
                "Digi 2FA page looks like SMS flow but phone target was not found. Hidden keys: %s",
                list(hidden.keys()),
            )

        email_candidates = {
            key: value
            for key, value in hidden.items()
            if ("mail" in key.lower() or "email" in key.lower()) and value
        }
        if email_candidates:
            key, value = next(iter(email_candidates.items()))
            methods["email"] = {
                "send_url": TWO_FA_SEND_URL,
                "send_payload": {
                    "action": "myAccount2FASend",
                    key: value,
                },
                "validate_payload": {
                    "action": "myAccount2FAVerify",
                    key: value,
                },
            }

        _LOGGER.debug(
            "Digi 2FA parse: methods=%s hidden_keys=%s",
            list(methods.keys()),
            list(hidden.keys()),
        )

        return methods

    async def send_2fa_code(
        self,
        context: TwoFactorContext,
        method: str,
        target_value: str | None = None,
    ) -> None:
        selected = context.methods.get(method)
        if not selected:
            raise DigiTwoFactorError(f"2FA method '{method}' is not available")

        payload = dict(selected["send_payload"])

        target_key: str | None = None
        target_options = selected.get("target_options") or []
        default_target = selected.get("default_target")

        if method == "sms" and (target_options or default_target):
            target_key = "phone"

        if target_key:
            resolved_target = (target_value or default_target or "").strip()
            if target_options and not resolved_target:
                if len(target_options) == 1:
                    resolved_target = str(target_options[0].get("value") or "").strip()
                else:
                    raise DigiTwoFactorError("2FA target selection is required")

            if target_options:
                allowed_values = {str(option.get("value") or "").strip() for option in target_options}
                if resolved_target not in allowed_values:
                    raise DigiTwoFactorError("Invalid 2FA target selected")

            if not resolved_target:
                raise DigiTwoFactorError("2FA target could not be determined")

            payload[target_key] = resolved_target
            context.selections[method] = resolved_target
        elif target_value:
            raise DigiTwoFactorError("Selected 2FA target is not supported for this method")

        resp = await self._request(
            "POST",
            selected["send_url"],
            data=payload,
            allow_redirects=True,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        text = await self._read_text(resp)

        if resp.status >= 400:
            raise DigiTwoFactorError(f"Failed to send code: HTTP {resp.status}")

        if text and "error" in text.lower():
            _LOGGER.debug("Digi send 2FA response: %s", text[:400])

    async def validate_2fa_code(self, context: TwoFactorContext, method: str, code: str) -> tuple[str, str]:
        selected = context.methods.get(method)
        if not selected:
            raise DigiTwoFactorError(f"2FA method '{method}' is not available")

        payload = dict(selected["validate_payload"])
        chosen_target = context.selections.get(method) or selected.get("default_target")
        if method == "sms" and chosen_target:
            payload["phone"] = chosen_target
        payload["code"] = code.strip()

        resp = await self._request(
            "POST",
            TWO_FA_VALIDATE_URL,
            data=payload,
            allow_redirects=True,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        raw = await self._read_text(resp)

        data: dict[str, Any] = {}
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            pass

        if resp.status >= 400:
            raise DigiTwoFactorError(f"Failed to validate code: HTTP {resp.status}")

        if data and not data.get("success", True):
            raise DigiTwoFactorError(data.get("message") or "Invalid verification code")

        follow = await self._request("GET", ADDRESS_SELECT_URL, allow_redirects=True)
        html = await self._read_text(follow)
        return str(follow.url), html

    async def get_address_options(self, html: str | None = None) -> list[AddressOption]:
        if html is None:
            resp = await self._request("GET", ADDRESS_SELECT_URL, allow_redirects=True)
            html = await self._read_text(resp)

        options: list[AddressOption] = self._extract_radio_options(html)

        if not options:
            for _, label in RE_ADDRESS_OPTION.findall(html):
                clean = self._clean_text(label)
                if clean and clean.lower() != "toate adresele":
                    options.append(AddressOption(value="", label=clean))

        return options

    async def confirm_address(self, address_id: str) -> None:
        payload = {"address": address_id, "order-btn-id": ""}
        resp = await self._request(
            "POST",
            ADDRESS_CONFIRM_URL,
            data=payload,
            allow_redirects=True,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        text = await self._read_text(resp)

        if resp.status >= 400:
            raise DigiAccountSelectionRequired(f"Address confirmation failed: HTTP {resp.status}")

        if text:
            try:
                data = json.loads(text)
                if not data.get("success", True):
                    raise DigiAccountSelectionRequired(data.get("message") or "Address confirmation failed")
            except json.JSONDecodeError:
                pass

    async def async_fetch_data(self, history_limit: int = 6) -> DigiData:
        resp = await self._request("GET", INVOICES_URL, allow_redirects=True)
        html = await self._read_text(resp)
        final_url = str(resp.url)

        if "/auth/login" in final_url or "/auth/2fa" in final_url or "/auth/address-select" in final_url:
            raise DigiReauthRequired("Session expired")

        parsed = self._parse_invoice_page(html)
        parsed_debug = parsed.get("debug") or {}
        if not parsed["rows"]:
            raise DigiError("No invoices found in Digi page")

        rows: list[InvoiceSummary] = parsed["rows"]

        recent_ids_by_address: dict[str, list[str]] = {}
        for row in rows:
            bucket = recent_ids_by_address.setdefault(row.address_key, [])
            if len(bucket) < history_limit:
                bucket.append(row.invoice_id)

        details: dict[str, InvoiceDetail] = {}
        for invoice_id in {item for values in recent_ids_by_address.values() for item in values}:
            details[invoice_id] = await self._fetch_invoice_details(invoice_id)
            await asyncio.sleep(0.15)

        invoices_by_address: dict[str, AddressInvoices] = {}
        grouped: dict[str, list[dict[str, Any]]] = {}

        for row in rows:
            if row.invoice_id not in details:
                continue

            detail = details[row.invoice_id]
            item = {
                "invoice_id": row.invoice_id,
                "address": row.address,
                "issue_date": detail.issue_date or row.issue_date,
                "due_date": detail.due_date or row.due_date,
                "description": row.description,
                "amount": detail.total if detail.total is not None else row.amount,
                "rest": detail.rest if detail.rest is not None else 0.0,
                "status": detail.status,
                "invoice_number": detail.invoice_number,
                "pdf_url": detail.pdf_url,
                "services": detail.services,
            }
            grouped.setdefault(row.address_key, []).append(item)

        for address_key, items in grouped.items():
            items.sort(key=lambda x: self._parse_date_for_sort(x.get("issue_date")), reverse=True)
            latest = items[0]
            unpaid_count = sum(
                1
                for item in items
                if (item.get("rest") or 0) > 0 or "neach" in (item.get("status") or "").lower()
            )

            invoices_by_address[address_key] = AddressInvoices(
                address_key=address_key,
                address=latest["address"],
                latest=latest,
                history=items,
                unpaid_count=unpaid_count,
            )

        return DigiData(
            account_label=None,
            account_id=None,
            invoices_by_address=invoices_by_address,
            last_update=datetime.utcnow(),
            needs_reauth=False,
        )

    def _parse_invoice_page(self, html: str) -> dict[str, Any]:
        addresses: dict[str, str] = {
            key: self._clean_text(label) for key, label in RE_ADDRESS_OPTION.findall(html)
        }
        debug_info: dict[str, Any] = {
            "addresses_count": len(addresses),
            "current_rows_count": 0,
            "archive_rows_count": 0,
            "cfg_ids_count": 0,
        }

        rows: list[InvoiceSummary] = []

        current_html = self._extract_section(html, "Facturi curente", "Facturi achitate")
        current_invoice_ids: list[str] = []
        if current_html:
            current_matches = RE_CURRENT_ROW.findall(current_html)
            debug_info["current_rows_count"] = len(current_matches)
            for address_key, invoice_id, issue_date, description, due_date, amount_text in current_matches:
                current_invoice_ids.append(str(invoice_id))
                rows.append(
                    InvoiceSummary(
                        invoice_id=str(invoice_id),
                        address_key=address_key,
                        address=addresses.get(
                            address_key,
                            address_key.replace("address-", "").replace("_", " "),
                        ),
                        issue_date=self._clean_text(issue_date),
                        due_date=self._clean_text(due_date),
                        description=self._clean_text(description),
                        amount=self._parse_money(amount_text),
                    )
                )

        archive_html = self._extract_section(html, "Facturi achitate", None)

        cfg_match = RE_SCRIPT_CFG.search(html)
        archive_ids: list[str] = []
        if cfg_match:
            try:
                cfg = json.loads(unescape(cfg_match.group(1).strip()))
                all_ids = [str(item["id"]) for item in cfg if item.get("id")]
                debug_info["cfg_ids_count"] = len(all_ids)

                # Digi include în client-invoices-cfg atât factura/facturile curente,
                # cât și facturile achitate. Pentru arhivă trebuie eliminate mai întâi
                # ID-urile deja folosite în secțiunea "Facturi curente", altfel prima
                # factură achitată poate primi greșit ID-ul facturii curente și ajunge
                # duplicată pe altă adresă.
                current_ids_remaining = list(current_invoice_ids)
                for invoice_id in all_ids:
                    if invoice_id in current_ids_remaining:
                        current_ids_remaining.remove(invoice_id)
                        continue
                    archive_ids.append(invoice_id)
            except json.JSONDecodeError as err:
                raise DigiError("Invalid invoice config JSON") from err

        archive_matches = list(RE_ROW.findall(archive_html if archive_html else html))
        debug_info["archive_rows_count"] = len(archive_matches)

        for idx, match in enumerate(archive_matches):
            if idx >= len(archive_ids):
                break

            address_key, issue_date, description, due_date, amount_text = match
            rows.append(
                InvoiceSummary(
                    invoice_id=archive_ids[idx],
                    address_key=address_key,
                    address=addresses.get(
                        address_key,
                        address_key.replace("address-", "").replace("_", " "),
                    ),
                    issue_date=self._clean_text(issue_date),
                    due_date=self._clean_text(due_date),
                    description=self._clean_text(description),
                    amount=self._parse_money(amount_text),
                )
            )

        if not rows:
            _LOGGER.debug("Digi invoices page parsed but no rows found")

        return {"rows": rows, "addresses": addresses, "debug": debug_info}

    async def _fetch_invoice_details(self, invoice_id: str) -> InvoiceDetail:
        payload = {
            "url": f"/my-account/invoices/details?invoice_id={invoice_id}",
            "id": invoice_id,
        }
        resp = await self._request(
            "POST",
            f"{BASE_URL}/my-account/invoices/details?invoice_id={invoice_id}",
            data=payload,
            allow_redirects=True,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        html = await self._read_text(resp)

        if resp.status >= 400:
            raise DigiError(
                f"Failed to fetch invoice details for {invoice_id}: HTTP {resp.status}"
            )

        html_unescaped = unescape(html)
        title_match = RE_DETAILS_TITLE.search(html_unescaped)
        pdf_match = RE_PDF.search(html_unescaped)

        label_money_matches = RE_LABEL_VALUE_MONEY.findall(html)
        money_map = {
            self._clean_text(label).lower(): self._parse_money(value)
            for label, value in label_money_matches
        }

        status_match = RE_LABEL_VALUE_TEXT.search(html_unescaped)

        services = []
        for raw_name, raw_price in RE_SERVICE_ROW.findall(html_unescaped):
            name = self._clean_text(raw_name)
            price_text = self._clean_text(raw_price)
            services.append(
                {
                    "name": name,
                    "amount": self._parse_money(price_text),
                    "raw_amount": price_text,
                }
            )

        invoice_number = None
        issue_date = None
        if title_match:
            invoice_number = self._clean_text(title_match.group(1))
            issue_date = self._clean_text(title_match.group(2))

        if not invoice_number:
            invoice_number = invoice_id

        total_value = money_map.get("total")
        rest_value = money_map.get("rest")

        return InvoiceDetail(
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            issue_date=issue_date,
            due_date=None,
            total=total_value,
            rest=rest_value,
            status=self._clean_text(status_match.group(1)) if status_match else None,
            pdf_url=urljoin(BASE_URL, unescape(pdf_match.group(1))) if pdf_match else None,
            services=services,
        )

    @staticmethod
    def _parse_money(text: str | None) -> float | None:
        if text is None:
            return None

        clean = unescape(text).strip()
        clean = re.sub(r"[^0-9,.\-]", "", clean)

        if not clean:
            return None

        if "," in clean and "." in clean:
            if clean.rfind(",") > clean.rfind("."):
                clean = clean.replace(".", "").replace(",", ".")
            else:
                clean = clean.replace(",", "")
        elif "," in clean:
            clean = clean.replace(",", ".")
        elif "." in clean:
            pass
        else:
            try:
                intval = int(clean)
                return intval / 100
            except ValueError:
                return None

        try:
            return float(clean)
        except ValueError:
            return None

    @staticmethod
    def _clean_text(text: str) -> str:
        return re.sub(r"\s+", " ", unescape(text)).strip()

    @staticmethod
    def _parse_date_for_sort(value: str | None) -> datetime:
        if not value:
            return datetime.min

        clean = value.strip().replace(".", "-").replace("/", "-")
        parts = clean.split("-")
        if len(parts) != 3:
            return datetime.min

        try:
            day, month, year = [int(part) for part in parts]
            return datetime(year, month, day)
        except ValueError:
            return datetime.min

    @staticmethod
    def _extract_section(html: str, start_marker: str, end_marker: str | None) -> str:
        start_idx = html.find(start_marker)
        if start_idx == -1:
            return ""

        sliced = html[start_idx:]
        if end_marker:
            end_idx = sliced.find(end_marker)
            if end_idx != -1:
                return sliced[:end_idx]
        return sliced
