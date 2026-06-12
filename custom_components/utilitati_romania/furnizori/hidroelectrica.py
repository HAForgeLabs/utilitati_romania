
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..exceptions import EroareAutentificare, EroareConectare
from ..modele import ConsumUtilitate, ContUtilitate, FacturaUtilitate, InstantaneuFurnizor
from .baza import ClientFurnizor
from .hidroelectrica_api import ClientApiHidroelectrica, EroareApiHidroelectrica, EroareAutentificareHidroelectrica
from .hidroelectrica_helper import parse_romanian_amount, safe_get


def _parseaza_data(text: str | None) -> date | None:
    if not text:
        return None
    text = str(text).strip().rstrip('Z')
    if ' ' in text:
        text = text.split(' ')[0]
    for fmt in ('%d/%m/%Y', '%d.%m.%Y', '%d-%m-%Y', '%Y%m%d', '%Y-%m-%d', '%m/%d/%Y', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _float_ro(valoare: Any) -> float | None:
    if valoare in (None, '', 'null'):
        return None
    try:
        if isinstance(valoare, (int, float)):
            return float(valoare)
        return float(parse_romanian_amount(str(valoare)))
    except Exception:
        try:
            return float(str(valoare).replace('.', '').replace(',', '.'))
        except Exception:
            return None


def _este_identificator_criptat(valoare: str | None) -> bool:
    if not valoare:
        return False
    valoare = str(valoare).strip()
    return valoare.endswith('==') or ('+' in valoare and '/' in valoare)


def _alias_din_adresa(adresa: str | None, fallback: str) -> str:
    if not adresa:
        return fallback
    txt = str(adresa).replace(';', ',')
    segmente = [s.strip() for s in txt.split(',') if s.strip()]
    if not segmente:
        return fallback

    # În răspunsurile Hidroelectrica, formatul uzual este de forma:
    #   "14, Aleea Sevis, ..." sau "29, Doamna Stanca, ..."
    # Primul segment este de regulă numărul, iar al doilea este strada.
    strada = segmente[1] if len(segmente) > 1 else segmente[0]
    strada = ' '.join(strada.replace('-', ' ').split()).strip()
    if not strada:
        return fallback

    cuvinte = strada.split()
    prefixe = {'strada', 'str', 'aleea', 'alee', 'al', 'bd', 'bulevardul', 'bulevard', 'sos', 'soseaua', 'calea', 'piata'}
    if cuvinte and cuvinte[0].lower() in prefixe:
        return ' '.join([cuvinte[0].title()] + [c.title() for c in cuvinte[1:]])

    # "Doamna Stanca" trebuie păstrat integral, nu doar ultimul cuvânt.
    return ' '.join(c.title() for c in cuvinte)


def _extrage_numar_factura_lizibil(sursa: dict[str, Any]) -> str | None:
    candidati = [
        sursa.get('exbel'),
        sursa.get('invoiceNo'),
        sursa.get('InvoiceNo'),
        sursa.get('invoiceNumber'),
        sursa.get('invoicenumber'),
        sursa.get('invoiceId'),
    ]
    for candidat in candidati:
        if candidat in (None, ''):
            continue
        text = str(candidat).strip()
        if text and not _este_identificator_criptat(text):
            return text
    return None


def _detecteaza_prosumator_din_factura(factura: dict[str, Any]) -> bool:
    txt = ' '.join(str(factura.get(k, '')) for k in ('invoiceType', 'type', 'channel', 'status', 'exbel', 'invoiceId')).lower()
    suma = _float_ro(factura.get('amount'))
    return (suma is not None and suma < 0) or ('credit' in txt) or ('prosum' in txt) or ('comp' in txt)


def _extrage_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get('result') or {}
    return result if isinstance(result, dict) else {}


def _extrage_lista_facturi(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get('result') or {}
    if not isinstance(result, dict):
        return []
    lista = result.get('objBillingHistoryEntity') or []
    if isinstance(lista, list) and lista:
        return [x for x in lista if isinstance(x, dict)]
    data_inner = result.get('Data') or {}
    if isinstance(data_inner, list):
        return [x for x in data_inner if isinstance(x, dict)]
    if isinstance(data_inner, dict):
        for cheie in ('objBillingHistoryData', 'objBillingData'):
            lista = data_inner.get(cheie) or []
            if isinstance(lista, list) and lista:
                return [x for x in lista if isinstance(x, dict)]
    return []


def _extrage_lista_usage(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data_usage = safe_get(payload, 'result', 'Data', default={})
    if isinstance(data_usage, dict):
        lista = data_usage.get('objUsageGenerationResultSetTwo') or []
        return [x for x in lista if isinstance(x, dict)]
    if isinstance(data_usage, list):
        return [x for x in data_usage if isinstance(x, dict)]
    return []


def _extrage_pod_si_instalare(payload: dict[str, Any] | None) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return '', ''
    data = safe_get(payload, 'result', 'Data', default={})
    lista = []
    if isinstance(data, dict):
        lista = data.get('objPodData') or []
    elif isinstance(data, list):
        lista = data
    if lista and isinstance(lista[0], dict):
        return str(lista[0].get('pod') or ''), str(lista[0].get('installation') or '')
    return '', ''


def _extrage_fereastra(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = safe_get(payload, 'result', 'Data', default={})
    return data if isinstance(data, dict) else {}


def _citire_permisa(window_data: dict[str, Any]) -> bool:
    """Returnează strict starea reală a ferestrei de autocitire din API.

    Nu folosim existența unei citiri anterioare drept fallback pentru că
    `previous_meter_read` există și în afara perioadei active și poate produce
    notificări false.
    """
    flag = window_data.get('Is_Window_Open')
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, (int, float)):
        return int(flag) == 1
    if isinstance(flag, str):
        return flag.strip().lower() in {'true', '1', 'yes', 'da'}
    return False


def _normalizare_registru(valoare: Any) -> str:
    return str(valoare or '').strip().upper()


def _registru_rand(row: dict[str, Any]) -> str:
    for key in ('Registers', 'registers', 'Register', 'register', 'registerCode', 'RegisterCode'):
        registru = _normalizare_registru(row.get(key))
        if registru:
            return registru
    return ''


def _este_registru_productie(registru: str) -> bool:
    return _normalizare_registru(registru) == '1.8.0_P'


def _este_registru_consum(registru: str) -> bool:
    return _normalizare_registru(registru) == '1.8.0'


def _istoric_are_registru_productie(history_payload: dict[str, Any] | None) -> bool:
    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            if _este_registru_productie(_registru_rand(node)):
                return True
            return any(_walk(value) for value in node.values())
        if isinstance(node, list):
            return any(_walk(item) for item in node)
        return False

    return _walk(history_payload or {})


def _index_din_previous(previous_payload: dict[str, Any] | None) -> float | None:
    prev_data = safe_get(previous_payload or {}, 'result', 'Data', default=[])
    if isinstance(prev_data, list) and prev_data and isinstance(prev_data[0], dict):
        return _float_ro(prev_data[0].get('prevMRResult'))
    return None


def _extract_serial_numbers(payload: dict[str, Any] | None) -> list[str]:
    rezultat: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {'serialnumber', 'serialno', 'meterserialnumber', 'serial_number'}:
                    text = str(value or '').strip()
                    if text and text not in rezultat:
                        rezultat.append(text)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload or {})
    return rezultat


def _extract_history_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    randuri: list[dict[str, Any]] = []

    index_keys = (
        'MRResult', 'mrResult', 'prevMRResult', 'Index', 'index', 'meterRead',
        'meterread', 'readValue', 'ReadValue', 'newmeterread', 'NewMeterRead',
        'CurrentRead', 'currentRead', 'readingValue', 'ReadingValue',
    )
    date_keys = (
        'MRDate', 'mrDate', 'Date', 'date', 'readDate', 'ReadDate',
        'meterReadDate', 'MeterReadDate', 'prevMRDate', 'createdOn', 'CreatedOn',
    )

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            data_raw = None
            for key in index_keys:
                if key in node and node.get(key) not in (None, '', 'null'):
                    data_raw = node.get(key)
                    break
            data_date = None
            for key in date_keys:
                if key in node and node.get(key):
                    data_date = node.get(key)
                    break
            if data_raw is not None and data_date:
                randuri.append(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload or {})
    return randuri


def _index_din_istoric(history_payload: dict[str, Any] | None, register_filter: str | None = None) -> float | None:
    candidati: list[tuple[date, float]] = []
    filtru = _normalizare_registru(register_filter) if register_filter else None
    for row in _extract_history_rows(history_payload):
        if filtru and _registru_rand(row) != filtru:
            continue
        data_citire = None
        for key in ('MRDate', 'mrDate', 'Date', 'date', 'readDate', 'ReadDate', 'meterReadDate', 'MeterReadDate', 'prevMRDate', 'createdOn', 'CreatedOn'):
            data_citire = _parseaza_data(row.get(key))
            if data_citire is not None:
                break
        if data_citire is None:
            continue

        valoare = None
        for key in ('MRResult', 'mrResult', 'prevMRResult', 'Index', 'index', 'meterRead', 'meterread', 'readValue', 'ReadValue', 'newmeterread', 'NewMeterRead', 'CurrentRead', 'currentRead', 'readingValue', 'ReadingValue'):
            valoare = _float_ro(row.get(key))
            if valoare is not None:
                break
        if valoare is None:
            continue

        candidati.append((data_citire, valoare))

    if not candidati:
        return None

    candidati.sort(key=lambda item: (item[0], item[1]))
    return candidati[-1][1]


def _construieste_factura_curenta_din_bill(
    bill: dict[str, Any] | None,
    *,
    id_cont: str,
    id_contract: str,
) -> FacturaUtilitate | None:
    if not isinstance(bill, dict) or not bill:
        return None

    numar_factura = _extrage_numar_factura_lizibil(bill)
    suma = _float_ro(bill.get('billamount') or bill.get('amount'))
    rest_plata = _float_ro(
        bill.get('rembalance')
        or bill.get('remainingAmount')
        or bill.get('amount_remaining')
    )
    data_emitere = _parseaza_data(
        bill.get('billdate')
        or bill.get('billDate')
        or bill.get('invoiceDate')
        or bill.get('date')
    )
    data_scadenta = _parseaza_data(
        bill.get('duedate')
        or bill.get('dueDate')
        or bill.get('scadenta')
    )

    if not numar_factura and suma is None and data_emitere is None and data_scadenta is None:
        return None

    este_prosumator = _detecteaza_prosumator_din_factura(bill)
    categorie = 'injectie' if este_prosumator and (suma is None or suma <= 0) else 'consum'
    stare = 'neplatita' if (rest_plata or 0) > 0 else None

    return FacturaUtilitate(
        id_factura=str(numar_factura or ''),
        titlu=str(bill.get('invoiceType') or bill.get('type') or 'Factură'),
        valoare=suma,
        moneda='RON',
        data_emitere=data_emitere,
        data_scadenta=data_scadenta,
        stare=stare,
        categorie=categorie,
        id_cont=id_cont,
        id_contract=id_contract,
        tip_utilitate='curent',
        tip_serviciu='curent',
        este_prosumator=este_prosumator,
        date_brute={**bill, 'rest_plata': rest_plata, '_synthetic_current_bill': True},
    )


class ClientFurnizorHidroelectrica(ClientFurnizor):
    cheie_furnizor = 'hidroelectrica'
    nume_prietenos = 'Hidroelectrica'

    def __init__(self, *, sesiune, utilizator: str, parola: str, optiuni: dict) -> None:
        super().__init__(sesiune=sesiune, utilizator=utilizator, parola=parola, optiuni=optiuni)
        self.api = ClientApiHidroelectrica(sesiune, utilizator, parola)

    async def async_testeaza_conexiunea(self) -> str:
        try:
            await self.api.async_login()
            conturi = await self.api.async_fetch_utility_accounts()
        except EroareAutentificareHidroelectrica as err:
            raise EroareAutentificare(str(err)) from err
        except EroareApiHidroelectrica as err:
            raise EroareConectare(str(err)) from err
        if conturi:
            primul = conturi[0]
            return str(primul.get('contractAccountID') or primul.get('accountNumber') or self.utilizator)
        return self.utilizator.lower()

    async def async_obtine_instantaneu(self) -> InstantaneuFurnizor:
        try:
            await self.api.async_ensure_authenticated()
            conturi_brute = await self.api.async_fetch_utility_accounts()
        except EroareAutentificareHidroelectrica as err:
            raise EroareAutentificare(str(err)) from err
        except EroareApiHidroelectrica as err:
            raise EroareConectare(str(err)) from err

        conturi: list[ContUtilitate] = []
        facturi: list[FacturaUtilitate] = []
        consumuri: list[ConsumUtilitate] = []
        exista_prosumator = False

        azi = datetime.now().date()
        de_la = (azi - timedelta(days=365 * 2)).strftime('%Y-%m-%d')
        pana_la = azi.strftime('%Y-%m-%d')

        for cont in conturi_brute:
            uan = str(cont.get('contractAccountID') or '').strip()
            account_number = str(cont.get('accountNumber') or '').strip()
            if not uan:
                continue

            adresa_cont = str(cont.get('address') or '') or None
            alias_cont = _alias_din_adresa(adresa_cont, account_number or uan)

            try:
                bill_payload = await self.api.async_fetch_bill(uan, account_number)
            except Exception:
                bill_payload = None
            bill = _extrage_result(bill_payload)

            try:
                billing_payload = await self.api.async_fetch_billing_history(uan, account_number, de_la, pana_la)
            except Exception:
                billing_payload = None
            lista_facturi = _extrage_lista_facturi(billing_payload)

            try:
                usage_payload = await self.api.async_fetch_usage(uan, account_number)
            except Exception:
                usage_payload = None
            lista_usage = _extrage_lista_usage(usage_payload)

            try:
                pods_payload = await self.api.async_fetch_pods(uan, account_number)
            except Exception:
                pods_payload = None
            pod, instalare = _extrage_pod_si_instalare(pods_payload)

            try:
                window_payload = await self.api.async_fetch_window_dates(uan, account_number)
            except Exception:
                window_payload = None
            window_data = _extrage_fereastra(window_payload)

            try:
                previous_payload = await self.api.async_fetch_previous_meter_read(uan, instalare, pod, '') if pod else None
            except Exception:
                previous_payload = None

            serial_numbers: list[str] = []
            history_payload = None
            if pod and instalare:
                try:
                    series_payload = await self.api.async_fetch_meter_counter_series(uan, instalare, pod)
                    serial_numbers = _extract_serial_numbers(series_payload)
                except Exception:
                    serial_numbers = []
                try:
                    history_payload = await self.api.async_fetch_meter_read_history(uan, instalare, pod, serial_numbers)
                except Exception:
                    history_payload = None

            id_cont_unic = account_number or uan
            este_prosumator_din_istoric = _istoric_are_registru_productie(history_payload)
            exista_prosumator = exista_prosumator or este_prosumator_din_istoric

            conturi.append(ContUtilitate(
                id_cont=id_cont_unic,
                nume=alias_cont,
                tip_cont='loc_consum',
                id_contract=uan,
                adresa=adresa_cont,
                stare='activ',
                tip_utilitate='curent',
                tip_serviciu='curent',
                este_prosumator=este_prosumator_din_istoric,
                date_brute={**cont, 'account_number': account_number, 'contract_account_id': uan, 'pod': pod, 'instalare': instalare, 'window_data': window_data, 'previous_meter_read': previous_payload, 'meter_read_history': history_payload, 'meter_serial_numbers': serial_numbers, 'este_prosumator': este_prosumator_din_istoric},
            ))

            facturi_cont: list[FacturaUtilitate] = []
            facturi_cont_ids: set[str] = set()
            for intrare in lista_facturi:
                suma = _float_ro(intrare.get('amount'))
                pros = _detecteaza_prosumator_din_factura(intrare)
                exista_prosumator = exista_prosumator or pros
                rest_plata = _float_ro(intrare.get('remainingAmount'))
                factura = FacturaUtilitate(
                    id_factura=str(_extrage_numar_factura_lizibil(intrare) or ''),
                    titlu=str(intrare.get('invoiceType') or 'Factură'),
                    valoare=suma,
                    moneda='RON',
                    data_emitere=_parseaza_data(intrare.get('invoiceDate')),
                    data_scadenta=_parseaza_data(intrare.get('dueDate')),
                    stare='neplatita' if (rest_plata or 0) > 0 else None,
                    categorie='injectie' if pros and (suma is None or suma <= 0) else 'consum',
                    id_cont=id_cont_unic,
                    id_contract=uan,
                    tip_utilitate='curent',
                    tip_serviciu='curent',
                    este_prosumator=pros,
                    date_brute={**intrare, 'rest_plata': rest_plata},
                )
                facturi_cont.append(factura)
                if factura.id_factura:
                    facturi_cont_ids.add(factura.id_factura)

            factura_curenta = _construieste_factura_curenta_din_bill(
                bill,
                id_cont=id_cont_unic,
                id_contract=uan,
            )
            if factura_curenta is not None:
                exista_prosumator = exista_prosumator or factura_curenta.este_prosumator
                if not factura_curenta.id_factura or factura_curenta.id_factura not in facturi_cont_ids:
                    facturi_cont.append(factura_curenta)
                    if factura_curenta.id_factura:
                        facturi_cont_ids.add(factura_curenta.id_factura)

            facturi.extend(facturi_cont)

            rembalance = _float_ro(bill.get('rembalance'))
            billamount = _float_ro(bill.get('billamount'))
            duedate = _parseaza_data(str(bill.get('duedate') or ''))
            numar_factura = _extrage_numar_factura_lizibil(bill)
            este_prosumator_cont = este_prosumator_din_istoric
            consumuri.append(ConsumUtilitate(cheie='este_prosumator', valoare='da' if este_prosumator_cont else 'nu', unitate=None, id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))
            exista_prosumator = exista_prosumator or este_prosumator_cont

            if rembalance is not None:
                consumuri.append(ConsumUtilitate(cheie='sold_curent', valoare=round(rembalance, 2), unitate='RON', id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent', date_brute=bill))
                if este_prosumator_cont and rembalance < 0:
                    consumuri.append(ConsumUtilitate(cheie='sold_prosumator', valoare=round(rembalance, 2), unitate='RON', id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent', date_brute=bill))
            if duedate is not None:
                consumuri.append(ConsumUtilitate(cheie='urmatoarea_scadenta', valoare=duedate.isoformat(), unitate=None, id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))

            facturi_sortate = [f for f in facturi_cont if f.categorie == 'consum' and f.data_emitere is not None]
            if billamount in (None, 0, 0.0, '0', '0.0') and facturi_sortate:
                ultima = sorted(facturi_sortate, key=lambda f: f.data_emitere, reverse=True)[0]
                billamount = ultima.valoare
            if billamount is not None:
                consumuri.append(ConsumUtilitate(cheie='valoare_ultima_factura', valoare=round(billamount, 2), unitate='RON', id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))

            if not numar_factura and facturi_sortate:
                candidati = [f for f in sorted(facturi_sortate, key=lambda f: f.data_emitere, reverse=True) if f.id_factura]
                if candidati:
                    numar_factura = candidati[0].id_factura
            if numar_factura:
                consumuri.append(ConsumUtilitate(cheie='id_ultima_factura', valoare=numar_factura, unitate=None, id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))

            # consum curent / index / citire / factura restanta
            if lista_usage:
                for item in lista_usage:
                    for cheie in ('UsageValue', 'Usage', 'usage', 'Consumption', 'consumption', 'Value', 'value', 'Amount'):
                        val = _float_ro(item.get(cheie))
                        if val is not None:
                            consumuri.append(ConsumUtilitate(cheie='consum_lunar_curent', valoare=round(val, 3), unitate='kWh', id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent', date_brute=item))
                            break
                    else:
                        continue
                    break
            index_curent = _index_din_istoric(history_payload, '1.8.0')
            if index_curent is None and not este_prosumator_cont:
                index_curent = _index_din_istoric(history_payload)
            if index_curent is None and not este_prosumator_cont:
                index_curent = _index_din_previous(previous_payload)
            if index_curent is not None:
                consumuri.append(ConsumUtilitate(cheie='index_energie_electrica', valoare=round(index_curent, 3), unitate='kWh', id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))

            index_productie = _index_din_istoric(history_payload, '1.8.0_P')
            if index_productie is not None:
                consumuri.append(ConsumUtilitate(cheie='index_energie_produsa', valoare=round(index_productie, 3), unitate='kWh', id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))

            consumuri.append(ConsumUtilitate(cheie='citire_permisa', valoare='Da' if _citire_permisa(window_data) else 'Nu', unitate=None, id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))
            are_restanta = (rembalance or 0) > 0
            consumuri.append(ConsumUtilitate(cheie='factura_restanta', valoare='Da' if are_restanta else 'Nu', unitate=None, id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))
            consumuri.append(ConsumUtilitate(cheie='sold_factura', valoare=round(rembalance,2) if rembalance is not None else None, unitate='RON', id_cont=id_cont_unic, tip_utilitate='curent', tip_serviciu='curent'))

        facturi = [f for f in facturi if f.id_factura or f.valoare is not None]
        return InstantaneuFurnizor(
            furnizor=self.cheie_furnizor,
            titlu=self.nume_prietenos,
            conturi=conturi,
            facturi=facturi,
            consumuri=consumuri,
            extra={'este_prosumator': exista_prosumator},
        )
