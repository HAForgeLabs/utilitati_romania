from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

from aiohttp import ClientSession

from ..exceptions import EroareAutentificare, EroareConectare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor
from .eon_api import EonApiClient

_LOGGER = logging.getLogger(__name__)


def _log_temporar(*_args, **_kwargs) -> None:
    return None



def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "None"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", "None"):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _parse_date(value: Any) -> date | None:
    if value in (None, "", "None"):
        return None

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("Z", "+00:00")

    for fmt in (
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _normalizeaza_tip_serviciu(cod: str | None) -> str | None:
    cod = _safe_str(cod).lower()
    if cod in ("01", "curent", "energie electrică", "energie electrica"):
        return "curent"
    if cod in ("02", "gaz"):
        return "gaz"
    if cod in ("00", "duo"):
        return "duo"
    return None


def _construieste_adresa(address_obj: dict | None) -> str:
    if not isinstance(address_obj, dict):
        return ""

    street_obj = address_obj.get("street") or {}
    street_type = _safe_str((street_obj.get("streetType") or {}).get("label"))
    street_name = _safe_str(street_obj.get("streetName"))
    street_number = _safe_str(address_obj.get("streetNumber"))
    apartment = _safe_str(address_obj.get("apartment"))

    locality_obj = address_obj.get("locality") or {}
    locality = _safe_str(locality_obj.get("localityName"))
    county_code = _safe_str(locality_obj.get("countyCode"))

    parti: list[str] = []

    strada = " ".join(x for x in [street_type, street_name] if x).strip()
    if strada:
        if street_number:
            strada = f"{strada} {street_number}"
        parti.append(strada)

    if apartment and apartment != "0":
        parti.append(f"ap. {apartment}")

    if locality:
        if county_code:
            parti.append(f"{locality}, jud. {county_code}")
        else:
            parti.append(locality)

    return ", ".join(parti)


def _alias_din_adresa(adresa: str, fallback: str) -> str:
    if not adresa:
        return fallback

    prima_parte = adresa.split(",")[0].strip()
    if not prima_parte:
        return fallback

    bucati = prima_parte.split()
    if bucati and bucati[0].isdigit():
        bucati = bucati[1:]

    alias = " ".join(bucati).strip()
    return alias or fallback


def _tip_utilitate_din_cod(cod: str | None) -> str:
    if cod == "01":
        return "energie electrică"
    if cod == "02":
        return "gaz"
    if cod == "00":
        return "duo"
    return "necunoscut"


def _cheie_sortare_factura(item: dict) -> tuple[int, datetime]:
    if not isinstance(item, dict):
        return (0, datetime.min)

    raw = _safe_str(
        item.get("maturityDate")
        or item.get("dueDate")
        or item.get("scadenceDate")
        or item.get("emissionDate")
        or item.get("issueDate")
        or item.get("invoiceDate")
        or ""
    )
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (1, datetime.strptime(raw[:19], fmt))
        except ValueError:
            pass
    parsed = _parse_date(raw)
    if parsed:
        return (1, datetime.combine(parsed, datetime.min.time()))
    return (0, datetime.min)


def _gaseste_ultima_factura_neachitata(facturi: list[dict] | None) -> dict | None:
    if not facturi or not isinstance(facturi, list):
        return None

    facturi_sortate = sorted(facturi, key=_cheie_sortare_factura, reverse=True)
    return facturi_sortate[0] if facturi_sortate else None


def _gaseste_ultima_factura_achitata(facturi: list[dict] | None) -> dict | None:
    if not facturi or not isinstance(facturi, list):
        return None

    facturi_sortate = sorted(facturi, key=_cheie_sortare_factura, reverse=True)
    return facturi_sortate[0] if facturi_sortate else None


def _factura_are_date_relevante(factura: dict | None) -> bool:
    if not isinstance(factura, dict):
        return False

    return bool(
        factura.get("invoiceNumber")
        or factura.get("number")
        or factura.get("maturityDate")
        or factura.get("dueDate")
        or factura.get("scadenceDate")
        or factura.get("emissionDate")
        or factura.get("issueDate")
        or factura.get("invoiceDate")
        or factura.get("issuedValue")
        or factura.get("invoiceValue")
        or factura.get("amount")
        or factura.get("value")
        or factura.get("balanceValue")
        or factura.get("totalBalance")
    )


def _factura_relevanta(
    invoices_unpaid: list[dict] | None,
    invoices_paid: list[dict] | None,
    invoice_balance: dict | None,
) -> dict | None:
    factura = _gaseste_ultima_factura_neachitata(invoices_unpaid)
    if factura:
        return factura

    factura = _gaseste_ultima_factura_achitata(invoices_paid)
    if factura:
        return factura

    if _factura_are_date_relevante(invoice_balance):
        return invoice_balance

    return None


def _citeste_sold_factura(invoice_balance: dict | None) -> float:
    if not isinstance(invoice_balance, dict):
        return 0.0

    balance_obj = invoice_balance.get("balance")
    if isinstance(balance_obj, dict):
        for cheie in ("balance", "total", "totalBalance", "balancePay", "balanceValue"):
            if balance_obj.get(cheie) not in (None, "", "None"):
                return _to_float(balance_obj.get(cheie), 0.0)

    for cheie in ("balance", "total", "totalBalance", "balancePay", "balanceValue"):
        value = invoice_balance.get(cheie)
        if isinstance(value, dict):
            continue
        if value not in (None, "", "None"):
            return _to_float(value, 0.0)

    return 0.0


def _registre_index(meter_index: dict | None) -> list[dict]:
    if not isinstance(meter_index, dict):
        return []
    rezultat: list[dict] = []
    devices = (((meter_index.get("indexDetails") or {}).get("devices")) or [])
    for device in devices:
        for registru in device.get("indexes") or []:
            if isinstance(registru, dict):
                rezultat.append(registru)
    return rezultat


def _registru_dupa_cod(meter_index: dict | None, cod: str) -> dict | None:
    cod_normalizat = _safe_str(cod).upper()
    for registru in _registre_index(meter_index):
        if _safe_str(registru.get("code")).upper() == cod_normalizat:
            return registru
    return None


def _registru_consum(meter_index: dict | None) -> dict | None:
    return _registru_dupa_cod(meter_index, "R") or (_registre_index(meter_index) or [None])[0]


def _valoare_registru(registru: dict | None) -> int | None:
    if not isinstance(registru, dict):
        return None
    for cheie in ("currentValue", "oldSelfIndexValue", "value", "oldValue"):
        if registru.get(cheie) is not None:
            return _to_int(registru.get(cheie), 0)
    return None


def _citeste_index_curent(meter_index: dict | None) -> int:
    valoare = _valoare_registru(_registru_consum(meter_index))
    return valoare if valoare is not None else 0


def _citeste_index_injectie(meter_index: dict | None) -> int | None:
    return _valoare_registru(_registru_dupa_cod(meter_index, "P"))


def _citeste_index_anterior(meter_index: dict | None) -> int:
    if not isinstance(meter_index, dict):
        return 0

    registru = _registru_consum(meter_index)
    if registru and registru.get("oldValue") is not None:
        return _to_int(registru.get("oldValue"), 0)
    return 0


def _citire_permisa(meter_index: dict | None) -> bool:
    if not isinstance(meter_index, dict):
        return False

    reading_period = meter_index.get("readingPeriod") or {}

    in_period = reading_period.get("inPeriod")
    if in_period is not None:
        return bool(in_period)

    allowed = reading_period.get("allowedReading")
    if allowed is not None:
        return bool(allowed)

    return False


def _fereastra_citire(meter_index: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(meter_index, dict):
        return None, None

    reading_period = meter_index.get("readingPeriod") or {}
    return (
        _safe_str(reading_period.get("startDate")) or None,
        _safe_str(reading_period.get("endDate")) or None,
    )


def _data_ultimului_index(meter_index: dict | None) -> str | None:
    if not isinstance(meter_index, dict):
        return None

    devices = (((meter_index.get("indexDetails") or {}).get("devices")) or [])
    for dev in devices:
        indexes = dev.get("indexes") or []
        if not indexes:
            continue
        registru = _registru_consum(meter_index)
        sent_at = registru.get("sentAt") if registru else None
        if sent_at:
            return _safe_str(sent_at)

    return None


def _id_intern_contor(meter_index: dict | None) -> str | None:
    if not isinstance(meter_index, dict):
        return None

    devices = (((meter_index.get("indexDetails") or {}).get("devices")) or [])
    for dev in devices:
        indexes = dev.get("indexes") or []
        for idx in indexes:
            ablbelnr = idx.get("ablbelnr")
            if ablbelnr:
                return _safe_str(ablbelnr)

    return None


def _consum_total_grafic(graphic_consumption: dict | None) -> float:
    if not isinstance(graphic_consumption, dict):
        return 0.0

    consum = graphic_consumption.get("consumption")
    if not isinstance(consum, list):
        return 0.0

    total = 0.0
    for item in consum:
        total += _to_float(item.get("consumptionValue"), 0.0)
    return round(total, 3)


def _consum_luna_curenta_grafic(graphic_consumption: dict | None) -> float:
    if not isinstance(graphic_consumption, dict):
        return 0.0

    consum = graphic_consumption.get("consumption")
    if not isinstance(consum, list):
        return 0.0

    acum = datetime.now()
    for item in consum:
        if _to_int(item.get("year")) == acum.year and _to_int(item.get("month")) == acum.month:
            return round(_to_float(item.get("consumptionValue"), 0.0), 3)

    return 0.0


def _conventie_consum(convention_list: list[dict] | None) -> dict[str, float]:
    rezultat: dict[str, float] = {}
    if not isinstance(convention_list, list) or not convention_list:
        return rezultat

    linie = convention_list[0].get("conventionLine") or {}
    for luna in range(1, 13):
        cheie = f"valueMonth{luna}"
        rezultat[str(luna)] = _to_float(linie.get(cheie), 0.0)
    return rezultat


def _istoric_plati(payments: list[dict] | None) -> list[dict]:
    if not isinstance(payments, list):
        return []

    rezultat: list[dict] = []
    for p in payments:
        rezultat.append(
            {
                "data": _safe_str(p.get("paymentDate")),
                "valoare": _to_float(p.get("value"), 0.0),
            }
        )
    return rezultat


def _ultima_plata(payments: list[dict] | None) -> dict | None:
    if not isinstance(payments, list) or not payments:
        return None

    def _cheie_sortare(item: dict) -> tuple[int, datetime]:
        raw = _safe_str(item.get("paymentDate") or item.get("date") or "")
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return (1, datetime.strptime(raw[:19], fmt))
            except ValueError:
                pass
        parsed = _parse_date(raw)
        if parsed:
            return (1, datetime.combine(parsed, datetime.min.time()))
        return (0, datetime.min)

    plati_sortate = sorted(payments, key=_cheie_sortare, reverse=True)
    return plati_sortate[0] if plati_sortate else None


def _plati_din_dashboard(invoice_balance: dict | None) -> list[dict]:
    if not isinstance(invoice_balance, dict):
        return []

    last_payment = invoice_balance.get("lastPayment")
    if not isinstance(last_payment, dict):
        return []

    return [
        {
            "paymentDate": last_payment.get("paymentDate"),
            "value": last_payment.get("value"),
            "paymentChannel": last_payment.get("paymentChannel"),
            "bank": last_payment.get("bank"),
        }
    ]


def _istoric_index(meter_history: dict | None) -> list[dict]:
    if not isinstance(meter_history, dict):
        return []

    rezultat: list[dict] = []
    history = meter_history.get("history") or []
    for an in history:
        year = an.get("year")
        for meter in an.get("meters") or []:
            for index in meter.get("indexes") or []:
                for reading in index.get("readings") or []:
                    rezultat.append(
                        {
                            "an": year,
                            "luna": reading.get("month"),
                            "valoare": _to_int(reading.get("value"), 0),
                            "tip": _safe_str(reading.get("readingType")),
                        }
                    )
    return rezultat


def _data_emitere_factura(factura: dict | None) -> str | None:
    if not isinstance(factura, dict):
        return None
    return (
        _safe_str(factura.get("emissionDate"))
        or _safe_str(factura.get("issueDate"))
        or _safe_str(factura.get("invoiceDate"))
        or None
    )


def _ultima_data_scadenta(factura: dict | None) -> str | None:
    if not isinstance(factura, dict):
        return None
    return (
        _safe_str(factura.get("maturityDate"))
        or _safe_str(factura.get("dueDate"))
        or _safe_str(factura.get("scadenceDate"))
        or _safe_str(factura.get("emissionDate"))
        or None
    )


def _valoare_factura(factura: dict | None) -> float:
    if not isinstance(factura, dict):
        return 0.0

    for cheie in (
        "issuedValue",
        "invoiceValue",
        "amount",
        "value",
        "balanceValue",
        "totalBalance",
        "balance",
    ):
        if factura.get(cheie) not in (None, "", "None"):
            return round(_to_float(factura.get(cheie), 0.0), 2)

    return 0.0


def _id_factura(factura: dict | None) -> str | None:
    if not isinstance(factura, dict):
        return None

    valoare = (
        factura.get("invoiceNumber")
        or factura.get("number")
        or factura.get("invoiceNo")
        or factura.get("id")
    )
    rezultat = _safe_str(valoare)
    return rezultat or None



def _detalii_consum_factura_eon(detalii: dict | None) -> dict[str, Any]:
    """Normalizează detaliile de contor din factura E.ON.

    Pentru gaz, E.ON oferă atât consumul în mc, cât și consumul convertit
    energetic în kWh. Pentru calculul costului mediu folosim kWh, deoarece
    factura este detaliată energetic.
    """
    if not isinstance(detalii, dict):
        return {}

    meter_details = detalii.get("meterDetails")
    if not isinstance(meter_details, list) or not meter_details:
        return {}

    consum_kwh = 0.0
    consum_mc = 0.0
    perioade: list[str] = []
    serie_contor = None
    index_vechi = None
    index_nou = None
    factura_id = None

    for item in meter_details:
        if not isinstance(item, dict):
            continue
        val_kwh = _to_float(item.get("energyConsumptionValue") or item.get("energyConsumption"), 0.0)
        val_mc = _to_float(item.get("consumptionMcValue") or item.get("consumptionMc"), 0.0)
        if val_kwh > 0:
            consum_kwh += val_kwh
        if val_mc > 0:
            consum_mc += val_mc
        if not serie_contor and item.get("meterSeries"):
            serie_contor = _safe_str(item.get("meterSeries"))
        if index_vechi is None and item.get("oldIndex") not in (None, ""):
            index_vechi = _safe_str(item.get("oldIndex"))
        if index_nou is None and item.get("newIndex") not in (None, ""):
            index_nou = _safe_str(item.get("newIndex"))
        if not factura_id and item.get("invoiceNumber"):
            factura_id = _safe_str(item.get("invoiceNumber"))
        perioada = _safe_str(item.get("consumptionPeriod"))
        if perioada and perioada not in perioade:
            perioade.append(perioada)

    rezultat: dict[str, Any] = {
        "sursa": "invoice_meter_details",
        "invoice_number": factura_id,
        "unitate_consum": "kWh" if consum_kwh > 0 else None,
        "consum_kwh": round(consum_kwh, 3) if consum_kwh > 0 else None,
        "consum_mc": round(consum_mc, 3) if consum_mc > 0 else None,
        "serie_contor": serie_contor,
        "index_vechi": index_vechi,
        "index_nou": index_nou,
        "perioada_consum": "; ".join(perioade) if perioade else None,
    }
    return {k: v for k, v in rezultat.items() if v not in (None, "")}

def _contract_duo(contract_details: dict | None, account_contract: str) -> str | None:
    if not isinstance(contract_details, dict):
        return None

    collective = _safe_str(
        contract_details.get("collectiveAccountContract")
        or contract_details.get("collectiveContract")
    )
    if not collective or collective == _safe_str(account_contract):
        return None

    product_name = _safe_str(contract_details.get("productName")).upper()
    contract_name = _safe_str(contract_details.get("contractName")).upper()
    is_duo = "DUO" in product_name or "DUO" in contract_name
    if not is_duo:
        return None

    return collective


async def asyncio_gather_eon(*aws):
    rezultate = []
    for coro in aws:
        try:
            rezultate.append(await coro)
        except Exception:
            _LOGGER.exception("Eroare la colectarea datelor E.ON")
            rezultate.append(None)
    return rezultate


class ClientFurnizorEon(ClientFurnizor):
    cheie_furnizor = "eon"
    nume_prietenos = "E.ON România"

    def __init__(
        self,
        *,
        sesiune: ClientSession,
        utilizator: str,
        parola: str,
        optiuni: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni or {})
        self._api = EonApiClient(sesiune, utilizator, parola)
        self.api = self._api

        token_data = self.optiuni.get("token_data") or self.optiuni.get("date_token_eon")
        if isinstance(token_data, dict):
            try:
                self._api.inject_token(token_data)
            except Exception:
                _LOGGER.exception("Nu am putut injecta tokenul E.ON existent.")


    def secunde_pana_la_refresh_sesiune(self) -> float:
        return self._api.seconds_until_refresh()

    async def async_reimprospateaza_sesiunea_fundal(self) -> dict[str, Any] | None:
        """Reimprospateaza sesiunea E.ON fara a porni login/2FA in fundal."""
        if not self._api.has_token:
            return None

        ok = await self._api.async_refresh_token(force=True)
        if not ok:
            return None

        return self._api.export_token_data()

    async def async_testeaza_conexiunea(self) -> str:
        try:
            ok = await self._api.async_login()
        except Exception as err:
            raise EroareConectare(f"Eroare la conectarea către E.ON: {err}") from err

        if not ok and not self._api.mfa_required:
            raise EroareAutentificare("Autentificare E.ON eșuată")

        try:
            contracte = await self._api.async_fetch_contracts_list()
        except Exception as err:
            raise EroareConectare(f"Nu s-au putut obține contractele E.ON: {err}") from err

        if isinstance(contracte, list) and contracte:
            primul = contracte[0]
            return _safe_str(primul.get("accountContract")) or self.utilizator.lower()

        return self.utilizator.lower()

    async def async_colecteaza_date(self) -> dict[str, Any]:
        pornire_colectare = time.monotonic()
        _log_temporar("[EON STARTUP DIAG] colectare_start")
        if not self._api.is_token_likely_valid():
            ok = await self._api.async_ensure_authenticated()
            if not ok:
                raise EroareAutentificare("Nu s-a putut autentifica la E.ON")

        etapa = time.monotonic()
        contracte = await self._api.async_fetch_contracts_list()
        _log_temporar("[EON STARTUP DIAG] lista_contracte durata=%.3fs", time.monotonic() - etapa)
        if getattr(self._api, "reauth_required", False):
            raise EroareAutentificare("Reautentificare E.ON necesara")
        if not isinstance(contracte, list):
            contracte = []

        _log_temporar("[EON DIAG] colectare contracte=%s", len(contracte))

        locuri_consum: list[dict[str, Any]] = []
        toate_intrari: list[dict[str, Any]] = []
        date_financiare_duo: dict[str, dict[str, Any]] = {}

        for contract in contracte:
            if isinstance(contract, dict) and isinstance(contract.get("contractDetails"), dict):
                contract = contract["contractDetails"]
            if not isinstance(contract, dict):
                continue

            utility_type = _safe_str(contract.get("utilityType"))
            is_collective = utility_type == "00" or str(contract.get("type", "")).strip() == "98"

            if not is_collective:
                toate_intrari.append(
                    {
                        "account_contract": _safe_str(contract.get("accountContract")),
                        "utility_type": utility_type,
                        "consumption_address": contract.get("consumptionPointAddress"),
                        "is_collective": False,
                    }
                )
                continue

            etapa = time.monotonic()
            raw_subs = await self._api.async_fetch_contracts_list(
                collective_contract=_safe_str(contract.get("accountContract"))
            )
            _log_temporar("[EON STARTUP DIAG] sub_contracte contract=***%s durata=%.3fs", _safe_str(contract.get("accountContract"))[-4:], time.monotonic() - etapa)
            if isinstance(raw_subs, list):
                for sub in raw_subs:
                    toate_intrari.append(
                        {
                            "account_contract": _safe_str(sub.get("accountContract")),
                            "utility_type": _safe_str(sub.get("utilityType")),
                            "consumption_address": sub.get("consumptionPointAddress"),
                            "is_collective": True,
                            "parent_contract": _safe_str(contract.get("accountContract")),
                        }
                    )

        for intrare in toate_intrari:
            cod_contract = intrare["account_contract"]
            utility_type = intrare.get("utility_type")
            tip_serviciu = _tip_utilitate_din_cod(utility_type)

            etapa = time.monotonic()
            _log_temporar("[EON STARTUP DIAG] date_contract_start contract=***%s", cod_contract[-4:])
            taskuri = await asyncio_gather_eon(
                self._api.async_fetch_contract_details(cod_contract),
                self._api.async_fetch_invoice_balance(cod_contract),
                self._api.async_fetch_invoices_unpaid(cod_contract),
                self._api.async_fetch_invoices_paid(cod_contract, max_pages=6),
                self._api.async_fetch_meter_index(cod_contract),
                self._api.async_fetch_consumption_convention(cod_contract),
                self._api.async_fetch_graphic_consumption(cod_contract),
                self._api.async_fetch_meter_history(cod_contract),
                self._api.async_fetch_payments(cod_contract, max_pages=3),
                self._api.async_fetch_invoice_balance_prosum(cod_contract),
                self._api.async_fetch_invoices_prosum(cod_contract, max_pages=3),
                self._api.async_fetch_rescheduling_plans(cod_contract),
            )

            _log_temporar("[EON STARTUP DIAG] date_contract_done contract=***%s durata=%.3fs", cod_contract[-4:], time.monotonic() - etapa)

            (
                contract_details,
                invoice_balance,
                invoices_unpaid,
                invoices_paid,
                meter_index,
                consumption_convention,
                graphic_consumption,
                meter_history,
                payments,
                invoice_balance_prosum,
                invoices_prosum,
                rescheduling_plans,
            ) = taskuri

            if getattr(self._api, "reauth_required", False):
                raise EroareAutentificare("Reautentificare E.ON necesara")

            contract_duo = _contract_duo(
                contract_details if isinstance(contract_details, dict) else None,
                cod_contract,
            )
            este_reprezentant_duo = bool(contract_duo and utility_type == "01")

            if contract_duo:
                if contract_duo not in date_financiare_duo:
                    rezultate_duo = await asyncio_gather_eon(
                        self._api.async_fetch_invoice_balance(contract_duo),
                        self._api.async_fetch_invoice_balance_details(
                            contract_duo,
                            include_subcontracts=True,
                        ),
                        self._api.async_fetch_invoices_unpaid(
                            contract_duo,
                            include_subcontracts=True,
                        ),
                        self._api.async_fetch_invoices_paid(
                            contract_duo,
                            max_pages=6,
                            include_subcontracts=True,
                        ),
                        self._api.async_fetch_invoice_balance_prosum(
                            contract_duo,
                            include_subcontracts=True,
                        ),
                        self._api.async_fetch_invoices_prosum(
                            contract_duo,
                            max_pages=3,
                            include_subcontracts=True,
                        ),
                        self._api.async_fetch_rescheduling_plans(
                            contract_duo,
                            include_subcontracts=True,
                        ),
                    )
                    (
                        duo_dashboard,
                        duo_balance,
                        duo_unpaid,
                        duo_paid,
                        duo_balance_prosum,
                        duo_prosum,
                        duo_plans,
                    ) = rezultate_duo
                    date_financiare_duo[contract_duo] = {
                        "dashboard": duo_dashboard if isinstance(duo_dashboard, dict) else {},
                        "balance": duo_balance if isinstance(duo_balance, dict) else {},
                        "unpaid": duo_unpaid if isinstance(duo_unpaid, list) else [],
                        "paid": duo_paid if isinstance(duo_paid, list) else [],
                        "balance_prosum": duo_balance_prosum if isinstance(duo_balance_prosum, dict) else {},
                        "prosum": duo_prosum if isinstance(duo_prosum, list) else [],
                        "plans": duo_plans if isinstance(duo_plans, list) else [],
                    }

                date_duo = date_financiare_duo[contract_duo]
                if este_reprezentant_duo:
                    invoice_balance = date_duo["dashboard"] or date_duo["balance"]
                    invoices_unpaid = date_duo["unpaid"]
                    invoices_paid = date_duo["paid"]
                    invoice_balance_prosum = date_duo["balance_prosum"]
                    invoices_prosum = date_duo["prosum"]
                    rescheduling_plans = date_duo["plans"]
                else:
                    # Factura DUO aparține contractului colectiv și se publică o singură dată,
                    # pe serviciul de energie electrică. Indexurile rămân separate pe fiecare serviciu.
                    invoice_balance = {}
                    invoices_unpaid = []
                    invoices_paid = []
                    invoice_balance_prosum = {}
                    invoices_prosum = []
                    rescheduling_plans = []

            address_obj = None
            if isinstance(contract_details, dict):
                address_obj = contract_details.get("consumptionPointAddress")
            if not address_obj:
                address_obj = intrare.get("consumption_address")

            adresa = _construieste_adresa(address_obj)
            alias = _alias_din_adresa(adresa, cod_contract)

            ultima_factura = _factura_relevanta(invoices_unpaid, invoices_paid, invoice_balance)

            if not isinstance(payments, list) or not payments:
                payments = _plati_din_dashboard(invoice_balance)

            istoric_plati = _istoric_plati(payments)
            ultima_plata = _ultima_plata(payments)

            sold_factura = round(_citeste_sold_factura(invoice_balance), 2)
            factura_restanta = bool((isinstance(invoices_unpaid, list) and len(invoices_unpaid) > 0) or sold_factura > 0)

            id_ultima_factura = _id_factura(ultima_factura)
            urmatoarea_scadenta = _ultima_data_scadenta(ultima_factura)
            data_ultima_factura = _data_emitere_factura(ultima_factura)

            detalii_contor_factura: dict[str, Any] = {}
            if id_ultima_factura:
                try:
                    etapa_factura = time.monotonic()
                    detalii_raw = await self._api.async_fetch_invoice_meter_details(id_ultima_factura)
                    _log_temporar("[EON STARTUP DIAG] detalii_factura contract=***%s durata=%.3fs", cod_contract[-4:], time.monotonic() - etapa_factura)
                    detalii_contor_factura = _detalii_consum_factura_eon(detalii_raw)
                except Exception:
                    _LOGGER.debug(
                        "E.ON: nu s-au putut citi detaliile de contor pentru factura %s",
                        id_ultima_factura,
                        exc_info=True,
                    )

            numar_ultima_factura = _safe_str(
                detalii_contor_factura.get("invoice_number")
                or (ultima_factura or {}).get("fiscalInvoiceNumber")
                or (ultima_factura or {}).get("documentNumber")
                or (ultima_factura or {}).get("billingDocumentNumber")
                or (ultima_factura or {}).get("invoiceNo")
                or id_ultima_factura
            ) or None

            if factura_restanta:
                valoare_document = _valoare_factura(ultima_factura)
                valoare_ultima_factura = (
                    round(valoare_document, 2)
                    if este_reprezentant_duo and valoare_document != 0
                    else round(sold_factura, 2)
                )
                tip_ultima_valoare = "factura"
            elif ultima_factura is not None:
                valoare_ultima_factura = _valoare_factura(ultima_factura)
                tip_ultima_valoare = "factura"
            else:
                valoare_ultima_factura = round(_to_float((ultima_plata or {}).get("valoare"), 0.0), 2)
                tip_ultima_valoare = "plata" if valoare_ultima_factura > 0 else "factura"

            sold_prosumator = 0.0
            if isinstance(invoice_balance_prosum, dict):
                sold_prosumator = round(
                    _to_float(
                        invoice_balance_prosum.get("balance")
                        or invoice_balance_prosum.get("totalBalance")
                        or 0.0
                    ),
                    2,
                )

            locuri_consum.append(
                {
                    "id": cod_contract,
                    "cod_contract": cod_contract,
                    "alias": alias,
                    "adresa": adresa,
                    "tip_serviciu": tip_serviciu,
                    "tip_utilitate_cod": utility_type,
                    "este_colectiv": bool(intrare.get("is_collective")),
                    "contract_parinte": contract_duo or intrare.get("parent_contract"),
                    "este_duo": bool(contract_duo),
                    "contract_duo": contract_duo,
                    "reprezentant_factura_duo": este_reprezentant_duo,
                    "date_contract": contract_details if isinstance(contract_details, dict) else {},
                    "de_plata": max(sold_factura, 0.0),
                    "sold_factura": sold_factura,
                    "sold_curent": sold_factura,
                    "factura_restanta": factura_restanta,
                    "id_ultima_factura": id_ultima_factura,
                    "numar_ultima_factura": numar_ultima_factura,
                    "valoare_ultima_factura": valoare_ultima_factura,
                    "tip_ultima_valoare": tip_ultima_valoare,
                    "data_ultima_factura": data_ultima_factura,
                    "detalii_contor_ultima_factura": detalii_contor_factura,
                    "consum_unitate_ultima_factura": detalii_contor_factura.get("consum_kwh"),
                    "unitate_consum_ultima_factura": "kWh" if detalii_contor_factura.get("consum_kwh") else None,
                    "consum_mc_ultima_factura": detalii_contor_factura.get("consum_mc"),
                    "valoare_factura_cost_unitate": _valoare_factura(ultima_factura) or valoare_ultima_factura,
                    "cost_mediu_unitate_ultima_factura": round((_valoare_factura(ultima_factura) or valoare_ultima_factura) / detalii_contor_factura.get("consum_kwh"), 4) if detalii_contor_factura.get("consum_kwh") and (_valoare_factura(ultima_factura) or valoare_ultima_factura) else None,
                    "unitate_cost_mediu": "kWh" if detalii_contor_factura.get("consum_kwh") else None,
                    "data_ultima_factura": data_ultima_factura,
                    "urmatoarea_scadenta": urmatoarea_scadenta,
                    "ultima_plata_data": _safe_str((ultima_plata or {}).get("data")) or None,
                    "ultima_plata_valoare": round(_to_float((ultima_plata or {}).get("valoare"), 0.0), 2),
                    "index_curent": _citeste_index_curent(meter_index),
                    "index_injectie": _citeste_index_injectie(meter_index),
                    "index_anterior": _citeste_index_anterior(meter_index),
                    "registre_index": _registre_index(meter_index),
                    "citire_permisa": _citire_permisa(meter_index),
                    "fereastra_citire_start": _fereastra_citire(meter_index)[0],
                    "fereastra_citire_end": _fereastra_citire(meter_index)[1],
                    "data_ultimului_index": _data_ultimului_index(meter_index),
                    "id_intern_contor": _id_intern_contor(meter_index),
                    "conventie_consum": _conventie_consum(consumption_convention),
                    "consum_total": _consum_total_grafic(graphic_consumption),
                    "consum_luna_curenta": _consum_luna_curenta_grafic(graphic_consumption),
                    "istoric_plati": istoric_plati,
                    "istoric_index": _istoric_index(meter_history),
                    "este_prosumator": bool(
                        (isinstance(contract_details, dict) and contract_details.get("isProsum") is True)
                        or _registru_dupa_cod(meter_index, "P") is not None
                        or (isinstance(invoices_prosum, list) and len(invoices_prosum) > 0)
                        or sold_prosumator != 0
                    ),
                    "sold_prosumator": sold_prosumator,
                    "facturi_prosumator": invoices_prosum if isinstance(invoices_prosum, list) else [],
                    "planuri_esalonare": rescheduling_plans if isinstance(rescheduling_plans, list) else [],
                    "meter_index": meter_index if isinstance(meter_index, dict) else {},
                    "meter_index_raw": meter_index if isinstance(meter_index, dict) else {},
                    "invoice_balance": invoice_balance if isinstance(invoice_balance, dict) else {},
                    "invoice_balance_raw": invoice_balance if isinstance(invoice_balance, dict) else {},
                    "invoices_unpaid_raw": invoices_unpaid if isinstance(invoices_unpaid, list) else [],
                    "invoices_paid_raw": invoices_paid if isinstance(invoices_paid, list) else [],
                }
            )

        total_de_plata = round(sum(_to_float(x.get("de_plata"), 0.0) for x in locuri_consum), 2)
        total_sold_factura = round(sum(_to_float(x.get("sold_factura"), 0.0) for x in locuri_consum), 2)
        numar_facturi = sum(len(x.get("invoices_unpaid_raw", [])) + len(x.get("invoices_paid_raw", [])) for x in locuri_consum)

        _log_temporar("[EON STARTUP DIAG] colectare_final durata_totala=%.3fs locuri=%s", time.monotonic() - pornire_colectare, len(locuri_consum))
        return {
            "rezumat": {
                "numar_locuri_consum": len(locuri_consum),
                "numar_facturi": numar_facturi,
                "total_de_plata": total_de_plata,
                "total_sold_factura": total_sold_factura,
                "are_prosumator": any(bool(x.get("este_prosumator")) for x in locuri_consum),
            },
            "locuri_consum": locuri_consum,
            "token_data": self._api.export_token_data(),
        }

    async def async_trimite_index(self, cod_contract: str, valoare: int | float) -> bool:
        if not self._api.is_token_likely_valid():
            ok = await self._api.async_ensure_authenticated()
            if not ok:
                return False

        meter_index = await self._api.async_fetch_meter_index(cod_contract)
        ablbelnr = _id_intern_contor(meter_index)
        if not ablbelnr:
            return False

        payload = [{"ablbelnr": ablbelnr, "indexValue": int(float(valoare))}]
        rezultat = await self._api.async_submit_meter_index(cod_contract, payload)
        return rezultat is not None

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        date_normalizate = await self.async_colecteaza_date()
        locuri = date_normalizate.get("locuri_consum", []) or []

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []

        are_prosumator = False
        total_sold_curent = 0.0
        total_sold_prosumator = 0.0

        for loc in locuri:
            id_cont = _safe_str(loc.get("id") or loc.get("cod_contract"))
            if not id_cont:
                continue

            tip_serviciu = _normalizeaza_tip_serviciu(loc.get("tip_utilitate_cod")) or _normalizeaza_tip_serviciu(loc.get("tip_serviciu"))
            if tip_serviciu == "duo":
                tip_serviciu = None

            conturi.append(
                ContUtilitate(
                    id_cont=id_cont,
                    nume=_safe_str(loc.get("alias")) or id_cont,
                    tip_cont=_safe_str(loc.get("tip_utilitate_cod")) or None,
                    id_contract=_safe_str(loc.get("cod_contract")) or None,
                    adresa=_safe_str(loc.get("adresa")) or None,
                    stare="activ",
                    tip_utilitate=_tip_utilitate_din_cod(_safe_str(loc.get("tip_utilitate_cod"))) or None,
                    tip_serviciu=tip_serviciu,
                    este_prosumator=bool(loc.get("este_prosumator")),
                    date_brute=loc,
                )
            )

            sold_curent = round(_to_float(loc.get("sold_curent", loc.get("sold_factura")), 0.0), 2)
            sold_prosumator = round(_to_float(loc.get("sold_prosumator"), 0.0), 2)
            are_prosumator = are_prosumator or bool(loc.get("este_prosumator"))
            total_sold_curent += sold_curent
            total_sold_prosumator += sold_prosumator

            consumuri.extend(
                [
                    ConsumUtilitate("de_plata", round(_to_float(loc.get("de_plata"), 0.0), 2), "RON", id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("sold_curent", sold_curent, "RON", id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("id_ultima_factura", _safe_str(loc.get("id_ultima_factura")) or None, None, id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("valoare_ultima_factura", round(_to_float(loc.get("valoare_ultima_factura"), 0.0), 2), "RON", id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("urmatoarea_scadenta", _safe_str(loc.get("urmatoarea_scadenta")) or None, None, id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("citire_permisa", "da" if bool(loc.get("citire_permisa")) else "nu", None, id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("factura_restanta", "da" if bool(loc.get("factura_restanta")) else "nu", None, id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("sold_factura", round(_to_float(loc.get("sold_factura"), 0.0), 2), "RON", id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("ultima_plata_valoare", round(_to_float(loc.get("ultima_plata_valoare"), 0.0), 2), "RON", id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("ultima_plata_data", _safe_str(loc.get("ultima_plata_data")) or None, None, id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("tip_ultima_valoare", _safe_str(loc.get("tip_ultima_valoare")) or None, None, id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("consum_unitate_ultima_factura", loc.get("consum_unitate_ultima_factura"), loc.get("unitate_consum_ultima_factura"), id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                    ConsumUtilitate("cost_mediu_unitate_ultima_factura", loc.get("cost_mediu_unitate_ultima_factura"), f"RON/{loc.get('unitate_cost_mediu')}" if loc.get("unitate_cost_mediu") else None, id_cont=id_cont, tip_serviciu=tip_serviciu, tip_utilitate=loc.get("tip_utilitate_cod"), date_brute=loc),
                ]
            )

            index_curent = _to_float(loc.get("index_curent"), 0.0)
            if loc.get("index_curent") is not None:
                unitate_index = "kWh" if tip_serviciu == "curent" else "m³" if tip_serviciu == "gaz" else None
                cheie_index = "index_energie_electrica" if tip_serviciu == "curent" else "index_gaz" if tip_serviciu == "gaz" else "index_curent"
                consumuri.append(
                    ConsumUtilitate(
                        cheie_index,
                        index_curent,
                        unitate_index,
                        id_cont=id_cont,
                        tip_serviciu=tip_serviciu,
                        tip_utilitate=loc.get("tip_utilitate_cod"),
                        date_brute=loc,
                    )
                )

            if loc.get("index_injectie") is not None:
                consumuri.append(
                    ConsumUtilitate(
                        "index_injectie",
                        _to_float(loc.get("index_injectie"), 0.0),
                        "kWh",
                        id_cont=id_cont,
                        tip_serviciu=tip_serviciu,
                        tip_utilitate=loc.get("tip_utilitate_cod"),
                        date_brute=loc,
                    )
                )

            factura_id = _safe_str(loc.get("id_ultima_factura"))
            sold_factura_loc = round(_to_float(loc.get("sold_factura"), 0.0), 2)
            factura_restanta_loc = bool(loc.get("factura_restanta"))
            valoare_factura_consum = round(_to_float(loc.get("valoare_ultima_factura"), 0.0), 2)

            # Într-un grup DUO, factura comună este publicată doar pe reprezentantul
            # electric. Contractul copil de gaz nu trebuie să genereze o factură
            # placeholder de 0 RON din senzorii de tip „ultima factură”.
            if bool(loc.get("este_duo")) and not bool(loc.get("reprezentant_factura_duo")):
                factura_id = ""
                factura_restanta_loc = False
                valoare_factura_consum = 0.0

            if not factura_id and factura_restanta_loc and sold_factura_loc > 0:
                factura_id = f"sold-{id_cont}"
                valoare_factura_consum = round(sold_factura_loc, 2)

            if factura_id:
                numar_factura_afisat = _safe_str(loc.get("numar_ultima_factura")) or factura_id
                raw_factura = {}
                if isinstance(loc.get("invoices_unpaid_raw"), list) and loc.get("invoices_unpaid_raw"):
                    prima_factura = loc.get("invoices_unpaid_raw")[0]
                    if isinstance(prima_factura, dict):
                        raw_factura.update(prima_factura)
                raw_factura.update(
                    {
                        "numar_factura": numar_factura_afisat,
                        "invoice_id_internal": factura_id,
                        "contract_duo": loc.get("contract_duo"),
                    }
                )
                facturi.append(
                    FacturaUtilitate(
                        id_factura=factura_id,
                        titlu=f"Factura {numar_factura_afisat}",
                        valoare=valoare_factura_consum,
                        moneda="RON",
                        data_emitere=_parse_date(loc.get("data_ultima_factura")),
                        data_scadenta=_parse_date(loc.get("urmatoarea_scadenta")),
                        stare="neplatita" if bool(loc.get("factura_restanta")) else "platita",
                        categorie="consum",
                        id_cont=id_cont,
                        id_contract=_safe_str(loc.get("cod_contract")) or None,
                        tip_utilitate=tip_serviciu,
                        tip_serviciu=tip_serviciu,
                        este_prosumator=False,
                        date_brute=raw_factura,
                    )
                )

            for factura_prosum in loc.get("facturi_prosumator", []) or []:
                if not isinstance(factura_prosum, dict):
                    continue
                id_fact = _id_factura(factura_prosum)
                if not id_fact:
                    continue
                facturi.append(
                    FacturaUtilitate(
                        id_factura=id_fact,
                        titlu=f"Factura prosumator {id_fact}",
                        valoare=round(
                            _to_float(
                                factura_prosum.get("issuedValue")
                                or factura_prosum.get("balanceValue")
                                or factura_prosum.get("value"),
                                0.0,
                            ),
                            2,
                        ),
                        moneda="RON",
                        data_emitere=_parse_date(factura_prosum.get("emissionDate")),
                        data_scadenta=_parse_date(factura_prosum.get("maturityDate") or factura_prosum.get("emissionDate")),
                        stare="prosumator",
                        categorie="injectie",
                        id_cont=id_cont,
                        id_contract=_safe_str(loc.get("cod_contract")) or None,
                        tip_utilitate=tip_serviciu,
                        tip_serviciu=tip_serviciu,
                        este_prosumator=True,
                        date_brute=factura_prosum,
                    )
                )

        consumuri.extend(
            [
                ConsumUtilitate("sold_curent", round(total_sold_curent, 2), "RON"),
                ConsumUtilitate("de_plata", round(max(total_sold_curent, 0.0), 2), "RON"),
                ConsumUtilitate("sold_prosumator", round(total_sold_prosumator, 2), "RON"),
                ConsumUtilitate("este_prosumator", "da" if are_prosumator else "nu", None),
                ConsumUtilitate("numar_facturi", float(len(facturi)), "buc"),
                ConsumUtilitate("numar_puncte_consum", float(len(conturi)), "buc"),
                ConsumUtilitate("numar_conturi_curent", float(sum(1 for c in conturi if c.tip_serviciu == "curent")), "buc"),
                ConsumUtilitate("numar_conturi_gaz", float(sum(1 for c in conturi if c.tip_serviciu == "gaz")), "buc"),
            ]
        )

        facturi.sort(key=lambda x: x.data_emitere or date.min, reverse=True)

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={
                "sumar": date_normalizate.get("rezumat", {}),
                "locuri_consum": locuri,
                "token_data": date_normalizate.get("token_data"),
            },
        )


FurnizorEon = ClientFurnizorEon