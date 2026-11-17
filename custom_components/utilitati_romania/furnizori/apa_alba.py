"""Client web pentru serviciile Apa CTTA Alba."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin

from aiohttp import ClientError, ClientResponseError, ClientSession

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)
_BASE = "https://contulmeu.apaalba.ro"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.7",
}


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["_Element | str"] = field(default_factory=list)

    def search(self, tag: str | None = None, css_class: str | None = None) -> list["_Element"]:
        matches = []
        for ch in self.children:
            if isinstance(ch, _Element):
                if (tag is None or ch.tag == tag) and (css_class is None or css_class in ch.attrs.get("class", "").split()):
                    matches.append(ch)
                matches.extend(ch.search(tag, css_class))
        return matches

    def content(self) -> str:
        return re.sub(r"\s+", " ", " ".join(ch.content() if isinstance(ch, _Element) else ch for ch in self.children)).strip()

    def first(self, css_class: str) -> str:
        found = self.search(css_class=css_class)
        return found[0].content() if found else ""


class _Page(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("root")
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Element(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].children.append(data)


def _suma(text: str) -> float | None:
    value = str(text or "").strip().replace("\xa0", "")
    match = re.search(r"-?\d[\d., ]*", value)
    if not match:
        return None
    value = match.group(0).replace(" ", "")
    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".") if value.rfind(",") > value.rfind(".") else value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return round(float(value), 2)
    except ValueError:
        return None


def _data(text: str) -> date | None:
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text or "")
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            return None
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if match:
        try:
            d, m, y = map(int, match.groups())
            return date(y, m, d)
        except ValueError:
            return None
    return None


def _randuri(source: str) -> list[_Element]:
    page = _Page(source).root
    return [row for row in page.search(css_class="tr") if "headings" not in row.attrs.get("class", "").split()]


def _locatii(source: str) -> list[dict[str, str]]:
    locations = []
    for row in _randuri(source):
        code = row.first("cod_locatie")
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,90}", code):
            continue
        locations.append({"code": code, "contract": row.first("nr_contract"), "address": row.first("locatie")})
    return locations


def _facturi(source: str, location: str) -> list[FacturaUtilitate]:
    items = []
    for row in _randuri(source):
        number = row.first("nr_factura")
        amount = _suma(row.first("total_factura"))
        due = _suma(row.first("rest_plata"))
        if not number or amount is None or due is None:
            continue
        due = max(0.0, due)
        status = "plătită" if due < 0.005 else ("parțial plătită" if due < max(amount, 0.0) - 0.005 else "neplătită")
        links = [a.attrs.get("href", "") for a in row.search(tag="a")]
        pdf = next((v for v in links if re.search(r"/download(?:\?|$)", v)), None)
        items.append(FacturaUtilitate(
            id_factura=number, titlu=f"Factură apă {number}", valoare=amount,
            moneda="RON", data_emitere=_data(row.first("data_fact")),
            data_scadenta=_data(row.first("data_scadenta")), stare=status,
            categorie="factura", id_cont=location, tip_utilitate="apă", tip_serviciu="apă",
            date_brute={"rest_plata": due, "pdf_url": pdf} if pdf else {"rest_plata": due},
        ))
    return items


def _plati(source: str) -> list[dict[str, Any]]:
    result = []
    for row in _randuri(source):
        number = row.first("nr_factura2")
        value = _suma(row.first("amount"))
        if number and value is not None:
            result.append({"invoice": number, "date": row.first("pay_at"), "amount": value, "status": row.first("status2")})
    return result


def _selected_location(source: str) -> str | None:
    """Identifică locația selectată, nu simpla prezență în lista de opțiuni."""
    document = _Page(source).root
    for field in document.search(tag="select"):
        if field.attrs.get("name") == "location_code":
            selected = [option.attrs.get("value", "") for option in field.search(tag="option") if "selected" in option.attrs]
            return next((value for value in selected if value), None)
    return None


def _meter_readings(source: str) -> dict[str, dict[str, Any]]:
    """Citiri raportate pe factura curentă, grupate după codul locului de consum."""
    readings: dict[str, dict[str, Any]] = {}
    account: str | None = None
    for row in _randuri(source):
        name = row.first("w-100")
        found = re.search(r"\b([A-Z0-9]+LC_[0-9]+)\b", name)
        if found:
            account = found.group(1)
        if not account or not row.first("index_nou"):
            continue
        current = _suma(row.first("index_nou"))
        consumption = _suma(row.first("cantitate"))
        if current is None:
            continue
        readings[account] = {
            "index_contor": current,
            "ultim_consum": consumption,
            "serie_contor": row.first("serie_contor") or None,
            "perioada_consum": row.first("data_citire") or None,
        }
    return readings


def _logged_in(source: str) -> bool:
    return "locuri-de-consum" in source and ("logout" in source or "Facturile mele" in source)


class ClientFurnizorApaAlba(ClientFurnizor):
    cheie_furnizor = "apa_alba"
    nume_prietenos = "Apă Alba (Apa CTTA)"

    def __init__(self, *, sesiune: ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)

    async def _get(self, path: str) -> str:
        try:
            async with self.sesiune.get(urljoin(_BASE, path), headers=_HEADERS, timeout=30) as response:
                response.raise_for_status()
                return await response.text()
        except (ClientError, TimeoutError) as err:
            raise EroareConectare("Nu se poate accesa portalul Apă Alba") from err

    async def _login(self) -> None:
        page = await self._get("/")
        doc = _Page(page).root
        csrf = next((x.attrs.get("value", "") for x in doc.search(tag="input") if x.attrs.get("name") == "_token"), "")
        if not csrf and _logged_in(page):
            return
        if not csrf:
            raise EroareParsare("Lipsește token-ul CSRF din pagina de autentificare Apă Alba")
        try:
            async with self.sesiune.post(
                urljoin(_BASE, "/login"),
                data={"_token": csrf, "email": self.utilizator, "password": self.parola},
                headers={**_HEADERS, "Referer": f"{_BASE}/", "Origin": _BASE},
                allow_redirects=True, timeout=30,
            ) as response:
                response.raise_for_status()
                page = await response.text()
                destination = str(response.url)
        except (ClientError, TimeoutError) as err:
            raise EroareConectare("Autentificarea Apă Alba nu a putut fi efectuată") from err
        if "/login" in destination or not _logged_in(page):
            raise EroareAutentificare("Autentificare respinsă de portalul Apă Alba")

    async def async_descarca_factura_pdf(self, invoice_id: str) -> bytes:
        """Descarcă numai PDF-uri cu identificatori validați de integrare."""
        if not re.fullmatch(r"[0-9]{1,20}", invoice_id):
            raise EroareParsare("Identificator de factură Apă Alba invalid")
        await self._login()
        try:
            async with self.sesiune.get(
                urljoin(_BASE, f"/facturile-mele/{invoice_id}/download"),
                headers={**_HEADERS, "Accept": "application/pdf", "Referer": f"{_BASE}/facturile-mele"},
                timeout=45,
            ) as response:
                response.raise_for_status()
                bucati = []
                dimensiune = 0
                async for bucata in response.content.iter_chunked(65536):
                    dimensiune += len(bucata)
                    if dimensiune > 25 * 1024 * 1024:
                        raise EroareParsare("Factura PDF depășește dimensiunea permisă")
                    bucati.append(bucata)
                content = b"".join(bucati)
        except (ClientError, TimeoutError) as err:
            raise EroareConectare("Descărcarea facturii Apă Alba a eșuat") from err
        if not content.startswith(b"%PDF-"):
            raise EroareParsare("Portalul nu a returnat un document PDF valid")
        return content

    async def async_testeaza_conexiunea(self) -> str:
        await self._login()
        page = await self._get("/locuri-de-consum")
        locations = _locatii(page)
        if not locations:
            raise EroareParsare("Nu există locuri de consum detectabile în portalul Apă Alba")
        return self.utilizator.strip().lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        await self._login()
        page = await self._get("/locuri-de-consum")
        locations = _locatii(page)
        if not locations:
            raise EroareParsare("Portalul Apă Alba nu a returnat locuri de consum")
        accounts: list[ContUtilitate] = []
        invoices: list[FacturaUtilitate] = []
        consumptions: list[ConsumUtilitate] = []
        payments: list[dict[str, Any]] = []
        for loc in locations:
            code = loc["code"]
            accounts.append(ContUtilitate(
                id_cont=code, nume=loc["address"] or code,
                id_contract=loc["contract"] or None, adresa=loc["address"] or None,
                tip_utilitate="apă", tip_serviciu="apă",
            ))
            switch = await self._get("/locuri-de-consum/select?location_code=" + quote(code, safe=""))
            if not _logged_in(switch):
                # Unele implementări redirecționează la lista locațiilor, nu la home.
                check = await self._get("/locuri-de-consum")
                if not _logged_in(check):
                    raise EroareAutentificare("Sesiune Apă Alba expirată")
            invoice_page = await self._get("/facturile-mele")
            if not _logged_in(invoice_page):
                raise EroareAutentificare("Sesiune Apă Alba expirată la preluarea facturilor")
            selected = _selected_location(invoice_page)
            if selected != code:
                raise EroareParsare("Portalul Apă Alba nu a confirmat schimbarea locației selectate")
            invoices.extend(_facturi(invoice_page, code))
            payment_page = await self._get("/istoric-plati")
            if _logged_in(payment_page):
                payments.extend([{**row, "account": code} for row in _plati(payment_page)])
            location_invoices = [item for item in invoices if item.id_cont == code]
            if location_invoices:
                recent = max(location_invoices, key=lambda item: item.data_emitere or date.min)
                pdf_url = recent.date_brute.get("pdf_url")
                if isinstance(pdf_url, str):
                    matched = re.search(r"/facturile-mele/(\d+)/download", pdf_url)
                    if matched:
                        reading_page = await self._get(f"/facturile-mele/{matched.group(1)}/date-contor")
                        reading = _meter_readings(reading_page).get(code)
                        if reading:
                            accounts[-1].date_brute.update(reading)
                            for key, val in reading.items():
                                if key in {"index_contor", "ultim_consum"} and val is not None:
                                    consumptions.append(ConsumUtilitate(
                                        cheie=key, valoare=val, unitate="m³", id_cont=code,
                                        tip_utilitate="apă", tip_serviciu="apă",
                                    ))
                for key, value, unit in (
                    ("numar_facturi", len(location_invoices), None),
                    ("numar_facturi_neachitate", sum(
                        float(item.date_brute.get("rest_plata") or 0) > 0 for item in location_invoices
                    ), None),
                    ("id_ultima_factura", recent.id_factura, None),
                    ("data_ultima_factura", recent.data_emitere.isoformat() if recent.data_emitere else None, None),
                    ("urmatoarea_scadenta", min(
                        (item.data_scadenta for item in location_invoices
                         if item.data_scadenta and float(item.date_brute.get("rest_plata") or 0) > 0),
                        default=None,
                    ), None),
                ):
                    consumptions.append(ConsumUtilitate(
                        cheie=key, valoare=value.isoformat() if isinstance(value, date) else value,
                        unitate=unit, id_cont=code, tip_utilitate="apă", tip_serviciu="apă",
                    ))
                consumptions.append(ConsumUtilitate(
                    cheie="sold_factura", valoare=recent.date_brute.get("rest_plata"),
                    unitate="RON", id_cont=code, tip_utilitate="apă", tip_serviciu="apă",
                ))
                consumptions.append(ConsumUtilitate(
                    cheie="valoare_ultima_factura", valoare=recent.valoare,
                    unitate="RON", id_cont=code, tip_utilitate="apă", tip_serviciu="apă",
                ))
                consumptions.append(ConsumUtilitate(
                    cheie="factura_restanta", valoare="Da" if any(
                        float(item.date_brute.get("rest_plata") or 0) > 0
                        and item.data_scadenta is not None and item.data_scadenta < date.today()
                        for item in location_invoices
                    ) else "Nu", unitate=None, id_cont=code,
                ))
            location_payments = [item for item in payments if item["account"] == code]
            if location_payments:
                recent_payment = max(location_payments, key=lambda item: _data(item["date"]) or date.min)
                for key, value, unit in (
                    ("numar_plati", len(location_payments), None),
                    ("data_ultima_plata", recent_payment["date"], None),
                    ("valoare_ultima_plata", recent_payment["amount"], "RON"),
                ):
                    consumptions.append(ConsumUtilitate(
                        cheie=key, valoare=value, unitate=unit, id_cont=code,
                        tip_utilitate="apă", tip_serviciu="apă",
                    ))
        facturi_unice: dict[tuple[str, str], FacturaUtilitate] = {}
        contracte = {cont.id_cont: (cont.id_contract or cont.id_cont) for cont in accounts}
        for factura in invoices:
            key = (contracte.get(factura.id_cont, factura.id_cont or ""), factura.id_factura)
            precedenta = facturi_unice.get(key)
            if precedenta is not None:
                if (precedenta.valoare != factura.valoare
                        or precedenta.date_brute.get("rest_plata") != factura.date_brute.get("rest_plata")):
                    raise EroareParsare(f"Date financiare contradictorii pentru factura {factura.id_factura}")
                continue
            facturi_unice[key] = factura

        sold_contracte: dict[str, float] = {}
        for (contract, _), factura in facturi_unice.items():
            sold_contracte[contract] = sold_contracte.get(contract, 0.0) + max(
                0.0, float(factura.date_brute.get("rest_plata") or 0.0)
            )
        total_de_plata = round(sum(sold_contracte.values()), 2)
        for cheie in ("de_plata", "sold_curent"):
            consumptions.append(ConsumUtilitate(
                cheie=cheie, valoare=total_de_plata, unitate="RON",
                tip_utilitate="apă", tip_serviciu="apă",
            ))
        if not invoices:
            _LOGGER.debug("Apă Alba: niciuna dintre locații nu are facturi în lista curentă")
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor, titlu=self.nume_prietenos,
            conturi=accounts, facturi=invoices, consumuri=consumptions,
            extra={
                "plati_preview": payments[:20],
                "locatii_count": len(locations),
                "facturi_unice_count": len(facturi_unice),
                "sold_contracte": {contract: round(sold, 2) for contract, sold in sold_contracte.items()},
            },
        )
