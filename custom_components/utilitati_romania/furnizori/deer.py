from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..exceptions import EroareAutentificare, EroareConectare, EroareParsare
from ..modele import ConsumUtilitate, ContUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor

_LOGGER = logging.getLogger(__name__)

URL_BAZA = "https://datemasura.distributie-energie.ro/date_ee/do"
HEADERS_BROWSER = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "Origin": "https://datemasura.distributie-energie.ro",
    "Referer": "https://datemasura.distributie-energie.ro/date_ee/do?action=loginForm",
}


def _safe_text(value: Any) -> str:
    return html.unescape(re.sub(r"\s+", " ", str(value or "").strip()))


def _strip_tags(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return _safe_text(value)


def _clean_html(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    return value


def _normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", _safe_text(value)).strip(" :")


def _extract_label_value(html_text: str, label: str) -> str | None:
    label_norm = _normalize_key(label).lower()
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_text, flags=re.I | re.S):
        cells_raw = re.findall(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", row, flags=re.I | re.S)
        if len(cells_raw) < 2:
            continue
        cells = [_normalize_key(_strip_tags(cell)) for cell in cells_raw]
        first = cells[0].lower() if cells else ""
        if first != label_norm and label_norm not in first:
            continue
        for value in cells[1:]:
            if not value or value.lower() in {"(sap)", "sap"}:
                continue
            return value
    return None


def _extract_label_map(html_text: str) -> dict[str, str]:
    rezultat: dict[str, str] = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_text, flags=re.I | re.S):
        cells_raw = re.findall(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", row, flags=re.I | re.S)
        if len(cells_raw) < 2:
            continue
        cells = [_normalize_key(_strip_tags(cell)) for cell in cells_raw]
        label = cells[0] if cells else ""
        if not label or label.lower() in {"(sap)", "sap"}:
            continue
        value = next((cell for cell in cells[1:] if cell and cell.lower() not in {"(sap)", "sap"}), "")
        if not value:
            continue
        rezultat[label] = value
    return rezultat


def _extract_current_pod(html_text: str) -> str | None:
    patterns = [
        r"id=\"hrefPodSelect\"[^>]*title=\"\s*([0-9]{10,})",
        r"id=\"hrefPodSelect\"[^>]*>\s*<strong>\s*([0-9]{10,})",
        r"Loc\s+de\s+consum\s*:\s*<a[^>]*>\s*<strong>\s*([0-9]{10,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.I | re.S)
        if match:
            return match.group(1).strip()
    return None


def _extract_selected_pods(xml_text: str) -> list[dict[str, str]]:
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        raise EroareParsare(f"Răspuns XML POD invalid: {err}") from err

    pods: list[dict[str, str]] = []
    for row in root.findall(".//row"):
        row_attr_id = _safe_text(row.attrib.get("id"))
        cells = [_safe_text(cell.text or "") for cell in row.findall("./cell")]

        selectie_id = cells[0] if len(cells) > 0 and re.fullmatch(r"\d+", cells[0]) else row_attr_id
        pod_code = ""
        adresa = ""

        if len(cells) >= 3:
            pod_code = cells[1]
            adresa = cells[2]
        elif len(cells) == 2:
            if re.fullmatch(r"\d{12,}", cells[0]):
                pod_code = cells[0]
                adresa = cells[1]
            else:
                selectie_id = cells[0] or row_attr_id
                pod_code = cells[1]
        elif len(cells) == 1:
            pod_code = cells[0]

        if not pod_code and re.fullmatch(r"\d{12,}", row_attr_id):
            pod_code = row_attr_id
        if not selectie_id:
            selectie_id = row_attr_id or pod_code

        if pod_code:
            pods.append(
                {
                    "row_id": selectie_id,
                    "pod": pod_code,
                    "adresa": adresa,
                }
            )
    return pods


def _parse_date(value: str | None) -> date | None:
    text = _safe_text(value)
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _extract_number(value: Any) -> float | None:
    text = _safe_text(value)
    if not text or text == "-":
        return None
    text = text.replace(" ", "")
    match = re.search(r"-?\d+(?:[\.,]\d+)?", text)
    if not match:
        return None
    raw = match.group(0)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_history_rows(html_text: str) -> list[dict[str, Any]]:
    """Extrage istoricul din tabelul infoCP dedicat indexurilor.

    Pagina DEER contine tabele imbricate. Parsarea tuturor randurilor din pagina
    cu o expresie regulata simpla poate pierde randurile tabelului interior.
    Identificam mai intai tabelul infoCP care contine antetele istoricului, apoi
    extragem numai randurile sale.
    """
    table_html = ""
    for candidate in re.findall(
        r"<table\b[^>]*class=[\"'][^\"']*\binfoCP\b[^\"']*[\"'][^>]*>(.*?)</table>",
        html_text,
        flags=re.I | re.S,
    ):
        header = _normalize_key(_strip_tags(candidate)).lower()
        if (
            "zi citire" in header
            and ("registri contor" in header or "registru contor" in header)
            and "unitate masura" in header
        ):
            table_html = candidate
            break

    if not table_html:
        return []

    rows: list[dict[str, Any]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S):
        cells = [
            _normalize_key(_strip_tags(cell))
            for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
        ]
        if len(cells) < 8:
            continue
        if not re.fullmatch(r"\d{12,}", cells[0] or ""):
            continue
        if not re.fullmatch(r"\d{3}", cells[4] or ""):
            continue

        citire = _extract_number(cells[6])
        rows.append(
            {
                "pod": cells[0],
                "serie_contor": cells[1],
                "constanta_facturare": _extract_number(cells[2]),
                "zi_citire": cells[3],
                "registru": cells[4],
                "motiv_citire": cells[5],
                "citire": citire if citire is not None else cells[6],
                "unitate_masura": cells[7],
            }
        )

    rows.sort(
        key=lambda item: (
            _parse_date(str(item.get("zi_citire"))) or date.min,
            str(item.get("registru")),
        ),
        reverse=True,
    )
    return rows


def _build_monthly_history(rows: list[dict[str, Any]], register: str) -> list[dict[str, Any]]:
    selected = [row for row in rows if str(row.get("registru") or "").strip() == register]
    selected.sort(key=lambda row: _parse_date(str(row.get("zi_citire") or "")) or date.min)

    monthly: list[dict[str, Any]] = []
    for previous, current in zip(selected, selected[1:]):
        previous_value = _extract_number(previous.get("citire"))
        current_value = _extract_number(current.get("citire"))
        current_date = _parse_date(str(current.get("zi_citire") or ""))
        if previous_value is None or current_value is None or current_date is None:
            continue
        if str(previous.get("serie_contor") or "") != str(current.get("serie_contor") or ""):
            continue

        difference = current_value - previous_value
        if difference < 0:
            continue

        multiplier = _extract_number(current.get("constanta_facturare"))
        if multiplier is None or multiplier <= 0:
            multiplier = 1.0

        monthly.append(
            {
                "luna": current_date.strftime("%Y-%m"),
                "data_citire": current_date.isoformat(),
                "index_initial": previous_value,
                "index_final": current_value,
                "valoare": round(difference * multiplier, 3),
                "registru": register,
            }
        )

    return list(reversed(monthly[-12:]))


def _history_latest_by_register(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        registru = str(row.get("registru") or "").strip()
        if registru and registru not in latest:
            latest[registru] = row
    return latest


class ClientFurnizorDeer(ClientFurnizor):
    cheie_furnizor = "deer"
    nume_prietenos = "Distribuție Energie Electrică România"

    def __init__(self, *, sesiune: aiohttp.ClientSession, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self._autentificat = False

    async def _request(
        self,
        *,
        method: str = "GET",
        action: str | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        allow_redirects: bool = True,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, str | None]:
        query = dict(params or {})
        if action:
            query["action"] = action
        url = URL_BAZA
        if query:
            url = f"{url}?{urlencode(query)}"

        merged_headers = dict(HEADERS_BROWSER)
        if headers:
            merged_headers.update(headers)

        try:
            async with self.sesiune.request(
                method,
                url,
                data=data,
                headers=merged_headers,
                allow_redirects=allow_redirects,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                text = await response.text(errors="ignore")
                location = response.headers.get("Location")
                return response.status, text, location
        except aiohttp.ClientError as err:
            raise EroareConectare(f"Eroare conectare DEER: {err}") from err
        except TimeoutError as err:
            raise EroareConectare("Timeout la comunicarea cu portalul DEER") from err

    async def _asigura_autentificare(self) -> None:
        if self._autentificat:
            return

        status, text, location = await self._request(
            method="POST",
            action="login",
            data={"login": self.utilizator, "password": self.parola},
            allow_redirects=False,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _LOGGER.debug("[DEER] login status=%s location=%s", status, location)

        if status not in (302, 303):
            mesaj = _safe_text(text)
            if "parola" in mesaj.lower() or "utilizator" in mesaj.lower() or "autentific" in mesaj.lower():
                raise EroareAutentificare("Autentificare DEER eșuată")
            raise EroareConectare(f"Login DEER a răspuns neașteptat cu HTTP {status}")

        if not location or "checkSelectPODAndPartner" not in location:
            raise EroareAutentificare("Portalul DEER nu a confirmat autentificarea")

        status, text, _ = await self._request(action="checkSelectPODAndPartner")
        _LOGGER.debug("[DEER] checkSelectPODAndPartner status=%s", status)
        if status >= 400:
            raise EroareConectare(f"Nu s-a putut deschide pagina principală DEER (HTTP {status})")
        if "loginForm" in text or "action=login" in text:
            raise EroareAutentificare("Sesiunea DEER nu a fost acceptată")
        self._autentificat = True

    async def _obtine_lista_poduri(self) -> list[dict[str, str]]:
        await self._asigura_autentificare()
        await self._request(action="editPodNeselectat")
        status, text, _ = await self._request(
            action="loadPODDataAction",
            params={
                "roleClient": "-1",
                "_search": "false",
                "rows": "50",
                "page": "1",
                "sidx": "POD",
                "sord": "desc",
                "nd": str(int(datetime.now().timestamp() * 1000)),
                "uniq": str(int(datetime.now().timestamp() * 1000) + 5),
            },
            headers={"Accept": "application/xml, text/xml, */*; q=0.01"},
        )
        _LOGGER.debug("[DEER] loadPODDataAction status=%s", status)
        if status >= 400:
            raise EroareConectare(f"Nu s-a putut încărca lista de POD-uri DEER (HTTP {status})")
        pods = _extract_selected_pods(text)
        if not pods:
            raise EroareParsare("Nu am putut extrage niciun POD din răspunsul DEER")
        return pods

    async def _obtine_html_pagina(self, action: str, params: dict[str, Any] | None = None) -> str:
        status, text, _ = await self._request(action=action, params=params)
        _LOGGER.debug("[DEER] %s status=%s", action, status)
        if status >= 400:
            raise EroareConectare(f"Pagina DEER {action} a răspuns cu HTTP {status}")
        if "loginForm" in text or "action=login" in text:
            self._autentificat = False
            raise EroareAutentificare("Sesiunea DEER a expirat")
        return text

    async def _selecteaza_pod(self, pod_item: dict[str, str]) -> str:
        await self._asigura_autentificare()
        expected_pod = _safe_text(pod_item.get("pod"))
        candidates = [expected_pod, _safe_text(pod_item.get("row_id"))]
        tried: list[str] = []

        for candidate in [c for c in candidates if c and c not in tried]:
            tried.append(candidate)
            status, _, _ = await self._request(
                method="POST",
                action="encryptPodId",
                data={"selectedPOD": candidate},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Accept": "text/html, */*; q=0.01",
                },
            )
            _LOGGER.debug("[DEER] encryptPodId status=%s selectedPOD=%s expected=%s", status, candidate, expected_pod)
            if status >= 400:
                continue

            status, _, _ = await self._request(
                method="POST",
                action="setSelectedPOD",
                data={},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Accept": "text/html, */*; q=0.01",
                },
            )
            _LOGGER.debug("[DEER] setSelectedPOD status=%s selectedPOD=%s expected=%s", status, candidate, expected_pod)
            if status >= 400:
                continue

            html_cont = await self._obtine_html_pagina("editInfoContClient")
            pod_activ = _extract_current_pod(html_cont)
            if pod_activ != expected_pod:
                html_pod = await self._obtine_html_pagina("editInfoPodClient")
                pod_activ = _extract_current_pod(html_pod) or pod_activ
            _LOGGER.debug("[DEER] POD activ după selectare=%s expected=%s", pod_activ, expected_pod)
            if not expected_pod or pod_activ == expected_pod:
                return html_cont

        raise EroareParsare(f"Nu am putut selecta corect POD-ul {expected_pod}")

    async def _obtine_conturi_minime(self) -> list[ContUtilitate]:
        pods = await self._obtine_lista_poduri()

        conturi: list[ContUtilitate] = []
        for item in pods:
            pod_code = _safe_text(item.get("pod"))
            adresa = _safe_text(item.get("adresa"))
            nume_cont = adresa or f"POD {pod_code}"

            conturi.append(
                ContUtilitate(
                    id_cont=pod_code,
                    nume=nume_cont,
                    tip_cont="pod",
                    id_contract=pod_code,
                    adresa=adresa or None,
                    stare="necunoscut",
                    tip_utilitate="energie",
                    tip_serviciu="distribuție energie electrică",
                    este_prosumator=False,
                    date_brute={
                        "pod": pod_code,
                        "adresa_loc_consum": adresa or None,
                        "incarcare_initiala": True,
                    },
                )
            )

        return conturi

    async def async_testeaza_conexiunea(self) -> str:
        pods = await self._obtine_lista_poduri()
        if not pods:
            raise EroareParsare("Nu există POD-uri disponibile în contul DEER")
        return self.utilizator.lower()

    async def async_obtine_instantaneu_minim(self) -> InstantaneuFurnizor:
        conturi = await self._obtine_conturi_minime()

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=[],
            consumuri=[
                ConsumUtilitate("numar_conturi", len(conturi), None, tip_utilitate="energie", tip_serviciu="distribuție"),
                ConsumUtilitate("este_prosumator", "nu", None, tip_utilitate="energie", tip_serviciu="distribuție"),
            ],
            extra={
                "suport_transmitere_index": False,
                "incarcare_initiala": True,
            },
        )

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        return await self.async_obtine_instantaneu_complet()

    async def async_obtine_instantaneu_complet(self) -> InstantaneuFurnizor:
        pods = await self._obtine_lista_poduri()
        conturi: list[ContUtilitate] = []
        consumuri: list[ConsumUtilitate] = []

        cod_client_general: str | None = None
        nume_client_general: str | None = None
        este_prosumator_global = False

        for item in pods:
            pod_code = _safe_text(item.get("pod"))
            html_cont = await self._selecteaza_pod(item)
            html_pod = await self._obtine_html_pagina("editInfoPodClient")
            html_pv = await self._obtine_html_pagina("editPVCitireClient")

            full_cont = _clean_html(html_cont)
            full_pod = _clean_html(html_pod)
            full_pv = _clean_html(html_pv)

            cont_map = _extract_label_map(full_cont)
            pod_map = _extract_label_map(full_pod)
            pv_map = _extract_label_map(full_pv)
            istoric_indici = _extract_history_rows(full_pod)
            istoric_registru_001 = [row for row in istoric_indici if str(row.get("registru") or row.get("registri_contor") or "").strip() == "001"][:10]
            istoric_registru_002 = [row for row in istoric_indici if str(row.get("registru") or row.get("registri_contor") or "").strip() == "002"][:10]
            istoric_lunar_consum = _build_monthly_history(istoric_indici, "001")
            istoric_lunar_injectie = _build_monthly_history(istoric_indici, "002")
            latest_by_register = _history_latest_by_register(istoric_indici)

            cod_client = cont_map.get("Cod client") or pod_map.get("Cod consumator")
            nume_client = cont_map.get("Client") or pod_map.get("Denumire consumator")
            adresa_client = cont_map.get("Adresa") or pod_map.get("Adresa consumator")
            adresa_loc_consum = pod_map.get("Adresa loc consum") or item.get("adresa") or adresa_client
            tip_loc_consum = pod_map.get("Tip loc de consum")
            profil_pod = pod_map.get("Profil")
            validitate_contract = (
                pod_map.get("Valabilitate contract")
                or pod_map.get("Validabilitate contract")
                or pod_map.get("Validitate contract")
                or _extract_label_value(full_pod, "Valabilitate contract")
                or _extract_label_value(full_pod, "Validabilitate contract")
                or _extract_label_value(full_pod, "Validitate contract")
            )
            if not validitate_contract:
                match_validitate = re.search(
                    r"(?:Valabilitate|Validabilitate|Validitate)\s+contract.*?</(?:td|th)>.*?<t[dh][^>]*>(.*?)</t[dh]>",
                    full_pod,
                    flags=re.I | re.S,
                )
                if match_validitate:
                    validitate_contract = _strip_tags(match_validitate.group(1)) or None
            programare_sfarsit_contract = pod_map.get("Programare sfarsit contract")
            profil_consum = pod_map.get("Profil de consum")
            denumire_furnizor = pod_map.get("Denumire furnizor")
            denumire_pre = pod_map.get("Denumire PRE")
            furnizor_pre = pod_map.get("Furnizor / PRE")
            putere_aprobata_consum = _extract_number(pod_map.get("Putere aprobata consum (kW)"))
            putere_aprobata_producere = _extract_number(pod_map.get("Putere aprobata producere (kW)"))
            valabilitate_atr = pod_map.get("Valabilitate ATR")
            numar_atr = pod_map.get("Numar ATR")
            data_inregistrare_atr = pod_map.get("Data inregistrare ATR")
            cod_punct_masurare = pod_map.get("Cod punct de masurare (POD + locatie dispozitiv)")
            punct_racordare = pod_map.get("Punct de racordare")
            punct_delimitare_patrimoniala = pod_map.get("Punct de delimitare patrimoniala")
            tensiune_delimitare = pod_map.get("Tensiunea in punctul de delimitare")
            stare_instalatiei = pod_map.get("Starea instalatiei")
            serie_contor = pod_map.get("Serie contor")
            tip_contor = pod_map.get("Tip contor")
            masurare_orara = pod_map.get("Masurare orara")
            masurare_zone_orare = pod_map.get("Masurare zone orare")
            clasa_precizie = pod_map.get("Clasa de precizie")
            data_instalare_contor = pod_map.get("Ziua si ora instalare contor")

            row_001 = latest_by_register.get("001")
            row_002 = latest_by_register.get("002")
            index_001 = row_001.get("citire") if row_001 else None
            index_002 = row_002.get("citire") if row_002 else None
            latest_row = next(iter(latest_by_register.values()), None)
            data_ultima_citire = (latest_row or {}).get("zi_citire")
            index_energie_electrica = index_001 if index_001 is not None else (latest_row or {}).get("citire")

            citire_permisa = "nu"
            text_perm = " ".join([full_pv, jsonish(pv_map)])
            if re.search(r"nu\s+se\s+poate|nu\s+este\s+permis|perioada.*inchis", text_perm, flags=re.I):
                citire_permisa = "nu"
            elif re.search(r"transmit|autocitir|citire", text_perm, flags=re.I):
                citire_permisa = "da"

            nume_cont = adresa_loc_consum or f"POD {pod_code}"
            conturi.append(
                ContUtilitate(
                    id_cont=pod_code,
                    nume=nume_cont,
                    tip_cont="pod",
                    id_contract=cod_client or pod_code,
                    adresa=adresa_loc_consum,
                    stare=stare_instalatiei or "activ",
                    tip_utilitate="energie",
                    tip_serviciu="distribuție energie electrică",
                    este_prosumator=(str(tip_loc_consum or "").upper() == "PROSUMATOR") or bool((putere_aprobata_producere or 0) > 0),
                    date_brute={
                        "pod": pod_code,
                        "cod_client": cod_client,
                        "nume_client": nume_client,
                        "adresa_client": adresa_client,
                        "adresa_loc_consum": adresa_loc_consum,
                        "tip_loc_consum": tip_loc_consum,
                        "profil_pod": profil_pod,
                        "profil": profil_pod,
                        "validitate_contract": validitate_contract,
                        "programare_sfarsit_contract": programare_sfarsit_contract,
                        "profil_consum": profil_consum,
                        "denumire_furnizor": denumire_furnizor,
                        "loc_consum": pod_code,
                        "denumire_pre": denumire_pre,
                        "furnizor_pre": furnizor_pre,
                        "putere_aprobata_consum": putere_aprobata_consum,
                        "putere_aprobata_producere": putere_aprobata_producere,
                        "valabilitate_atr": valabilitate_atr,
                        "numar_atr": numar_atr,
                        "data_inregistrare_atr": data_inregistrare_atr,
                        "cod_punct_masurare": cod_punct_masurare,
                        "punct_racordare": punct_racordare,
                        "punct_delimitare_patrimoniala": punct_delimitare_patrimoniala,
                        "tensiune_delimitare": tensiune_delimitare,
                        "stare_instalatiei": stare_instalatiei,
                        "serie_contor": serie_contor,
                        "tip_contor": tip_contor,
                        "masurare_orara": masurare_orara,
                        "masurare_zone_orare": masurare_zone_orare,
                        "clasa_precizie": clasa_precizie,
                        "data_instalare_contor": data_instalare_contor,
                        "istoric_indici": istoric_indici,
                        "istoric_registru_001": istoric_registru_001,
                        "istoric_registru_002": istoric_registru_002,
                        "istoric_lunar_consum": istoric_lunar_consum,
                        "istoric_lunar_injectie": istoric_lunar_injectie,
                        "index_registru_001": index_001,
                        "index_registru_002": index_002,
                        "data_ultima_citire": data_ultima_citire,
                        "citire_permisa": citire_permisa,
                        "pagina_cont": html_cont,
                        "pagina_pod": html_pod,
                        "pagina_pv": html_pv,
                        "incarcare_initiala": False,
                    },
                )
            )

            consum_ultima_perioada = istoric_lunar_consum[0].get("valoare") if istoric_lunar_consum else None
            injectie_ultima_perioada = istoric_lunar_injectie[0].get("valoare") if istoric_lunar_injectie else None
            consum_ultimele_12_luni = (
                round(sum(float(item.get("valoare") or 0) for item in istoric_lunar_consum), 3)
                if istoric_lunar_consum
                else None
            )
            injectie_ultimele_12_luni = (
                round(sum(float(item.get("valoare") or 0) for item in istoric_lunar_injectie), 3)
                if istoric_lunar_injectie
                else None
            )
            data_ultima_citire_consum = istoric_lunar_consum[0].get("data_citire") if istoric_lunar_consum else None
            data_ultima_citire_injectie = istoric_lunar_injectie[0].get("data_citire") if istoric_lunar_injectie else None

            deer_items: list[tuple[str, Any, str | None]] = [
                ("client", nume_client, None),
                ("cod_client", cod_client, None),
                ("adresa_loc_consum", adresa_loc_consum, None),
                ("loc_consum", pod_code, None),
                ("profil", profil_pod, None),
                ("validitate_contract", validitate_contract, None),
                ("denumire_furnizor", denumire_furnizor, None),
                ("putere_aprobata_consum", putere_aprobata_consum, "kW"),
                ("putere_aprobata_producere", putere_aprobata_producere, "kW"),
                ("numar_atr", numar_atr, None),
                ("data_inregistrare_atr", data_inregistrare_atr, None),
                ("cod_punct_masurare", cod_punct_masurare, None),
                ("punct_racordare", punct_racordare, None),
                ("tensiune_delimitare", tensiune_delimitare, None),
                ("stare_instalatiei", stare_instalatiei, None),
                ("serie_contor", serie_contor, None),
                ("tip_contor", tip_contor, None),
                ("masurare_orara", masurare_orara, None),
                ("masurare_zone_orare", masurare_zone_orare, None),
                ("clasa_precizie", clasa_precizie, None),
                ("index_registru_001", index_001, "kWh"),
                ("index_registru_002", index_002, "kWh"),
                ("consum_ultima_perioada", consum_ultima_perioada, "kWh"),
                ("consum_ultimele_12_luni", consum_ultimele_12_luni, "kWh"),
                ("injectie_ultima_perioada", injectie_ultima_perioada, "kWh"),
                ("injectie_ultimele_12_luni", injectie_ultimele_12_luni, "kWh"),
                ("data_ultima_citire_consum", data_ultima_citire_consum, None),
                ("data_ultima_citire_injectie", data_ultima_citire_injectie, None),
            ]
            for cheie, valoare, unitate in deer_items:
                if valoare not in (None, "", "-"):
                    consumuri.append(
                        ConsumUtilitate(
                            cheie,
                            valoare,
                            unitate,
                            id_cont=pod_code,
                            tip_utilitate="energie",
                            tip_serviciu="distribuție",
                        )
                    )

            if index_energie_electrica not in (None, "", "-"):
                consumuri.append(
                    ConsumUtilitate(
                        "index_energie_electrica",
                        index_energie_electrica,
                        "kWh",
                        id_cont=pod_code,
                        tip_utilitate="energie",
                        tip_serviciu="distribuție",
                    )
                )

            if citire_permisa not in (None, "", "-"):
                consumuri.append(
                    ConsumUtilitate(
                        "citire_permisa",
                        citire_permisa,
                        None,
                        id_cont=pod_code,
                        tip_utilitate="energie",
                        tip_serviciu="distribuție",
                    )
                )

            if not cod_client_general and cod_client:
                cod_client_general = cod_client
            if not nume_client_general and nume_client:
                nume_client_general = nume_client
            if str(tip_loc_consum or "").upper() == "PROSUMATOR" or bool((putere_aprobata_producere or 0) > 0):
                este_prosumator_global = True

        consumuri.append(ConsumUtilitate("numar_conturi", len(conturi), None, tip_utilitate="energie", tip_serviciu="distribuție"))
        if cod_client_general:
            consumuri.append(ConsumUtilitate("cod_client", cod_client_general, None, tip_utilitate="energie", tip_serviciu="distribuție"))
        if nume_client_general:
            consumuri.append(ConsumUtilitate("nume_client", nume_client_general, None, tip_utilitate="energie", tip_serviciu="distribuție"))
        consumuri.append(ConsumUtilitate("este_prosumator", "da" if este_prosumator_global else "nu", None, tip_utilitate="energie", tip_serviciu="distribuție"))

        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=[],
            consumuri=consumuri,
            extra={
                "suport_transmitere_index": False,
                "incarcare_initiala": False,
            },
        )


def jsonish(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in data.items():
        if value not in (None, "", "-"):
            parts.append(f"{key}: {value}")
    return " | ".join(parts)