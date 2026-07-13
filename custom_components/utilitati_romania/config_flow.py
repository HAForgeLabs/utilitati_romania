from __future__ import annotations

import json

import aiohttp
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACCOUNT_ID,
    CONF_CHEIE_LICENTA,
    CONF_CONTRACT_ACCOUNT_ID,
    CONF_CONTRACT_ID,
    CONF_DATE_TOKEN_EON,
    CONF_DIGI_2FA_METHOD,
    CONF_DIGI_2FA_TARGET,
    CONF_DIGI_COOKIES,
    CONF_DIGI_HISTORY_LIMIT,
    CONF_DIGI_SELECTED_ACCOUNT_ID,
    CONF_DIGI_SELECTED_ACCOUNT_LABEL,
    CONF_FURNIZOR,
    DATE_VERIFICARE_LICENTA,
    FURNIZOR_ADMIN_GLOBAL,
    CONF_INTERVAL_ACTUALIZARE,
    CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
    CONF_PAROLA,
    CONF_PUNCTE_CONSUM_SELECTATE,
    CONF_MOBILE_NOTIFY_SERVICE,
    CONF_PREMISE_LABEL,
    CONF_UTILIZATOR,
    DOMENIU,
    IMPLICIT_DIGI_HISTORY_LIMIT,
    IMPLICIT_INTERVAL_ACTUALIZARE_ORE,
    IMPLICIT_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
    MAXIM_DIGI_HISTORY_LIMIT,
    MAXIM_INTERVAL_ACTUALIZARE_ORE,
    MAXIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
    MINIM_DIGI_HISTORY_LIMIT,
    MINIM_INTERVAL_ACTUALIZARE_ORE,
    MINIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
)
from .exceptions import EroareAutentificare, EroareConectare
from .furnizori.apa_canal import ClientFurnizorApaCanal, OptiuneContractApaCanal
from .furnizori.apa_brasov import ClientFurnizorApaBrasov, OptiuneContractApaBrasov, nume_scurt_locatie_apa_brasov
from .furnizori.myelectrica import ClientApiMyElectrica
from .furnizori.digi_api import (
    AddressOption,
    DigiAccountSelectionRequired,
    DigiApiClient,
    DigiAuthError,
    DigiTwoFactorError,
    DigiTwoFactorRequired,
    TwoFactorContext,
)
from .furnizori.eon_api import EonApiClient
from .furnizori.registru import FURNIZORI, obtine_clasa_furnizor
from .licentiere import (
    async_obtine_licenta_globala,
    async_salveaza_licenta_globala,
    async_valideaza_licenta,
    valideaza_rezultat_licenta,
)
from .naming import build_location_alias

_LOGGER = logging.getLogger(__name__)


def _creeaza_sesiune_dedicata(hass):
    return async_create_clientsession(
        hass,
        auto_cleanup=False,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )


def _elibereaza_sesiune_dedicata(sesiune: aiohttp.ClientSession) -> None:
    if not sesiune.closed:
        sesiune.detach()

FURNIZOR_OPTIONS: list[SelectOptionDict] = [
    {"value": cheie, "label": clasa.nume_prietenos}
    for cheie, clasa in FURNIZORI.items()
]


def _rezumat_debug_eon(**detalii: Any) -> str:
    """Construiește un mesaj de diagnostic E.ON fără date sensibile."""
    valori_sigur_de_afisat: dict[str, Any] = {}
    for cheie, valoare in detalii.items():
        if cheie in {"utilizator", "parola", "token", "cod", "email"}:
            continue
        if isinstance(valoare, str) and len(valoare) > 80:
            valori_sigur_de_afisat[cheie] = f"{valoare[:32]}..."
        else:
            valori_sigur_de_afisat[cheie] = valoare
    return json.dumps(valori_sigur_de_afisat, ensure_ascii=False, default=str)


class FluxConfigurareUtilitatiRomania(config_entries.ConfigFlow, domain=DOMENIU):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return FluxOptiuniUtilitatiRomania(config_entry)

    def __init__(self) -> None:
        self._furnizor: str | None = None
        self._cheie_licenta: str = "TRIAL"

        self._api_eon: EonApiClient | None = None
        self._date_utilizator_curente: dict[str, Any] | None = None
        self._contracte_apa_canal: list[OptiuneContractApaCanal] = []
        self._contracte_apa_brasov: list[OptiuneContractApaBrasov] = []

        self._api_digi: DigiApiClient | None = None
        self._api_myelectrica: ClientApiMyElectrica | None = None
        self._myelectrica_optiuni: list[dict[str, str]] = []
        self._digi_pending: dict[str, Any] = {}
        self._digi_two_factor: TwoFactorContext | None = None
        self._digi_address_options: list[AddressOption] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        licenta_globala = await async_obtine_licenta_globala(self.hass)
        cheie_globala = str(licenta_globala.get(CONF_CHEIE_LICENTA, "")).strip()

        if user_input is not None:
            self._furnizor = str(user_input[CONF_FURNIZOR])
            self._cheie_licenta = str(user_input.get(CONF_CHEIE_LICENTA, cheie_globala or "TRIAL")).strip() or "TRIAL"
            return await self.async_step_credentiale_furnizor()

        schema_items: dict[Any, Any] = {
            vol.Required(CONF_FURNIZOR): SelectSelector(
                SelectSelectorConfig(options=FURNIZOR_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
        }
        if not cheie_globala:
            schema_items[vol.Required(CONF_CHEIE_LICENTA, default="TRIAL")] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            )

        schema = vol.Schema(schema_items)
        return self.async_show_form(step_id="user", data_schema=schema, errors=erori)

    async def async_step_credentiale_furnizor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}

        if not self._furnizor:
            return await self.async_step_user()

        clasa_furnizor = obtine_clasa_furnizor(self._furnizor)

        if user_input is not None:
            self._date_utilizator_curente = user_input

            try:
                rezultat_licenta = await async_valideaza_licenta(
                    self.hass,
                    self._cheie_licenta,
                    user_input[CONF_UTILIZATOR],
                )
                valideaza_rezultat_licenta(rezultat_licenta)
            except Exception:
                erori["base"] = "licenta_invalida"
            else:
                await async_salveaza_licenta_globala(
                    self.hass,
                    self._cheie_licenta,
                    user_input[CONF_UTILIZATOR],
                    rezultat_licenta,
                )
                if self._furnizor == "eon":
                    return await self._proceseaza_flux_eon(user_input, erori)

                if self._furnizor == "digi":
                    return await self._proceseaza_flux_digi(user_input, erori)

                if self._furnizor == "myelectrica":
                    return await self._proceseaza_flux_myelectrica(user_input, erori)

                if self._furnizor == "apa_canal":
                    client_apa_canal = ClientFurnizorApaCanal(
                        sesiune=(async_create_clientsession(self.hass) if self._furnizor == "deo" else async_get_clientsession(self.hass)),
                        utilizator=user_input[CONF_UTILIZATOR],
                        parola=user_input[CONF_PAROLA],
                        optiuni=user_input,
                    )
                    try:
                        self._contracte_apa_canal = await client_apa_canal.async_obtine_contracte_disponibile()
                    except EroareAutentificare:
                        erori["base"] = "autentificare_esuata"
                    except EroareConectare:
                        erori["base"] = "nu_se_poate_conecta"
                    except Exception:
                        _LOGGER.exception("Eroare neașteptată în fluxul de configurare Apă Canal")
                        erori["base"] = "necunoscuta"
                    else:
                        if not self._contracte_apa_canal:
                            erori["base"] = "fara_contracte"
                        else:
                            return await self.async_step_selectare_contract_apa_canal()
                elif self._furnizor == "apa_brasov":
                    client_apa_brasov = ClientFurnizorApaBrasov(
                        sesiune=async_get_clientsession(self.hass),
                        utilizator=user_input[CONF_UTILIZATOR],
                        parola=user_input[CONF_PAROLA],
                        optiuni=user_input,
                    )
                    try:
                        self._contracte_apa_brasov = await client_apa_brasov.async_obtine_contracte_disponibile()
                    except EroareAutentificare:
                        erori["base"] = "autentificare_esuata"
                    except EroareConectare:
                        erori["base"] = "nu_se_poate_conecta"
                    except Exception:
                        _LOGGER.exception("Eroare neașteptată în fluxul de configurare Apă Brașov")
                        erori["base"] = "necunoscuta"
                    else:
                        if not self._contracte_apa_brasov:
                            erori["base"] = "fara_contracte"
                        else:
                            return await self.async_step_selectare_contract_apa_brasov()
                else:
                    sesiune_dedicata = self._furnizor in {"deo", "retele_electrice"}
                    sesiune = (
                        _creeaza_sesiune_dedicata(self.hass)
                        if sesiune_dedicata
                        else async_get_clientsession(self.hass)
                    )
                    client = clasa_furnizor(
                        sesiune=sesiune,
                        utilizator=user_input[CONF_UTILIZATOR],
                        parola=user_input[CONF_PAROLA],
                        optiuni=user_input,
                    )
                    try:
                        unic = await client.async_testeaza_conexiunea()
                    except EroareAutentificare:
                        erori["base"] = "autentificare_esuata"
                    except EroareConectare:
                        erori["base"] = "nu_se_poate_conecta"
                    except Exception:
                        _LOGGER.exception("Eroare neașteptată în fluxul de configurare")
                        erori["base"] = "necunoscuta"
                    else:
                        await self.async_set_unique_id(f"{self._furnizor}::{unic}")
                        self._abort_if_unique_id_configured()
                        titlu_intrare = clasa_furnizor.nume_prietenos
                        if self._furnizor == "apa_galati":
                            cod_client = str(user_input.get(CONF_UTILIZATOR) or unic or "").strip()
                            if cod_client:
                                titlu_intrare = f"Apă Canal Galați - {cod_client}"
                        return self.async_create_entry(
                            title=titlu_intrare,
                            data={
                                CONF_FURNIZOR: self._furnizor,
                                CONF_CHEIE_LICENTA: self._cheie_licenta,
                                **user_input,
                            },
                        )
                    finally:
                        if sesiune_dedicata:
                            _elibereaza_sesiune_dedicata(sesiune)

        schema_items: dict[Any, Any] = {
            vol.Required(CONF_UTILIZATOR): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_PAROLA): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_INTERVAL_ACTUALIZARE,
                default=IMPLICIT_INTERVAL_ACTUALIZARE_ORE,
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MINIM_INTERVAL_ACTUALIZARE_ORE,
                    max=MAXIM_INTERVAL_ACTUALIZARE_ORE,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }

        if self._furnizor == "digi":
            schema_items[vol.Optional(
                CONF_DIGI_HISTORY_LIMIT,
                default=IMPLICIT_DIGI_HISTORY_LIMIT,
            )] = NumberSelector(
                NumberSelectorConfig(
                    min=MINIM_DIGI_HISTORY_LIMIT,
                    max=MAXIM_DIGI_HISTORY_LIMIT,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            )

        return self.async_show_form(
            step_id="credentiale_furnizor",
            data_schema=vol.Schema(schema_items),
            errors=erori,
            description_placeholders={"furnizor": clasa_furnizor.nume_prietenos},
        )

    async def async_step_selectare_contract_apa_canal(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}

        if not self._contracte_apa_canal:
            return await self.async_step_credentiale_furnizor(self._date_utilizator_curente)

        if user_input is not None:
            selectie = str(user_input.get("contract_apa_canal") or "")
            contract = next(
                (
                    item
                    for item in self._contracte_apa_canal
                    if f"{item.account_id}|{item.contract_account_id}|{item.contract_id}" == selectie
                ),
                None,
            )
            if contract is None:
                erori["base"] = "contract_invalid"
            else:
                unique = f"{contract.account_id}_{contract.contract_id}"
                await self.async_set_unique_id(f"{self._furnizor}::{unique}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Apă Canal Sibiu - {build_location_alias(contract.eticheta, contract.contract_id)}",
                    data={
                        CONF_FURNIZOR: self._furnizor,
                        CONF_CHEIE_LICENTA: self._cheie_licenta,
                        CONF_UTILIZATOR: self._date_utilizator_curente[CONF_UTILIZATOR],
                        CONF_PAROLA: self._date_utilizator_curente[CONF_PAROLA],
                        CONF_INTERVAL_ACTUALIZARE: int(
                            self._date_utilizator_curente.get(
                                CONF_INTERVAL_ACTUALIZARE,
                                IMPLICIT_INTERVAL_ACTUALIZARE_ORE,
                            )
                        ),
                        CONF_ACCOUNT_ID: contract.account_id,
                        CONF_CONTRACT_ACCOUNT_ID: contract.contract_account_id,
                        CONF_CONTRACT_ID: contract.contract_id,
                        CONF_PREMISE_LABEL: contract.eticheta,
                    },
                )

        options = [
            {
                "value": f"{item.account_id}|{item.contract_account_id}|{item.contract_id}",
                "label": item.eticheta,
            }
            for item in self._contracte_apa_canal
        ]

        return self.async_show_form(
            step_id="selectare_contract_apa_canal",
            data_schema=vol.Schema(
                {
                    vol.Required("contract_apa_canal"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=erori,
        )
    

    async def async_step_selectare_contract_apa_brasov(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}

        if not self._contracte_apa_brasov:
            return await self.async_step_credentiale_furnizor(self._date_utilizator_curente)

        if user_input is not None:
            selectie = str(user_input.get("contract_apa_brasov") or "")

            if selectie == "__all__":
                utilizator_normalizat = self._date_utilizator_curente[CONF_UTILIZATOR].strip().lower()
                await self.async_set_unique_id(f"{self._furnizor}::{utilizator_normalizat}::toate_locatiile")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Apă Brașov",
                    data={
                        CONF_FURNIZOR: self._furnizor,
                        CONF_CHEIE_LICENTA: self._cheie_licenta,
                        CONF_UTILIZATOR: self._date_utilizator_curente[CONF_UTILIZATOR],
                        CONF_PAROLA: self._date_utilizator_curente[CONF_PAROLA],
                        CONF_INTERVAL_ACTUALIZARE: int(
                            self._date_utilizator_curente.get(
                                CONF_INTERVAL_ACTUALIZARE,
                                IMPLICIT_INTERVAL_ACTUALIZARE_ORE,
                            )
                        ),
                        CONF_ACCOUNT_ID: "__all__",
                        CONF_CONTRACT_ID: "__all__",
                        CONF_PREMISE_LABEL: "Toate locațiile",
                        "apa_brasov_selector_value": "",
                        "apa_brasov_context_id": "",
                    },
                )

            contract = next(
                (
                    item
                    for item in self._contracte_apa_brasov
                    if f"{item.loccons_id}|{item.selector_value}" == selectie
                ),
                None,
            )
            if contract is None:
                erori["base"] = "contract_invalid"
            else:
                unique = contract.loccons_id or contract.selector_value or self._date_utilizator_curente[CONF_UTILIZATOR].lower()
                await self.async_set_unique_id(f"{self._furnizor}::{unique}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Apă Brașov - {nume_scurt_locatie_apa_brasov(contract.eticheta, contract.loccons_id)}",
                    data={
                        CONF_FURNIZOR: self._furnizor,
                        CONF_CHEIE_LICENTA: self._cheie_licenta,
                        CONF_UTILIZATOR: self._date_utilizator_curente[CONF_UTILIZATOR],
                        CONF_PAROLA: self._date_utilizator_curente[CONF_PAROLA],
                        CONF_INTERVAL_ACTUALIZARE: int(
                            self._date_utilizator_curente.get(
                                CONF_INTERVAL_ACTUALIZARE,
                                IMPLICIT_INTERVAL_ACTUALIZARE_ORE,
                            )
                        ),
                        CONF_ACCOUNT_ID: contract.loccons_id,
                        CONF_CONTRACT_ID: contract.loccons_id,
                        CONF_PREMISE_LABEL: contract.eticheta,
                        "apa_brasov_selector_value": contract.selector_value,
                        "apa_brasov_context_id": contract.context_id or "",
                    },
                )

        options = [
            {
                "value": "__all__",
                "label": "Apă Brașov - toate locațiile",
            },
            *[
                {
                    "value": f"{item.loccons_id}|{item.selector_value}",
                    "label": f"Apă Brașov - {nume_scurt_locatie_apa_brasov(item.eticheta, item.loccons_id)}",
                }
                for item in self._contracte_apa_brasov
            ],
        ]

        return self.async_show_form(
            step_id="selectare_contract_apa_brasov",
            data_schema=vol.Schema(
                {
                    vol.Required("contract_apa_brasov"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=erori,
        )
    async def _proceseaza_flux_myelectrica(
        self,
        user_input: dict[str, Any],
        erori: dict[str, str],
    ) -> ConfigFlowResult:
        self._api_myelectrica = ClientApiMyElectrica(
            async_get_clientsession(self.hass),
            user_input[CONF_UTILIZATOR],
            user_input[CONF_PAROLA],
        )
        try:
            ok = await self._api_myelectrica.async_login()
            hierarchy_raw = await self._api_myelectrica.async_get_hierarchy() if ok else None
        except EroareConectare:
            erori["base"] = "nu_se_poate_conecta"
            return self.async_show_form(step_id="credentiale_furnizor", errors=erori)
        except Exception:
            _LOGGER.exception("Eroare neașteptată în fluxul myElectrica")
            erori["base"] = "necunoscuta"
            return self.async_show_form(step_id="credentiale_furnizor", errors=erori)
    
        details = hierarchy_raw.get("details") if isinstance(hierarchy_raw, dict) else None
        if not ok:
            erori["base"] = "autentificare_esuata"
            return self.async_show_form(step_id="credentiale_furnizor", errors=erori)
        if not isinstance(details, list) or not details:
            erori["base"] = "fara_contracte"
            return self.async_show_form(step_id="credentiale_furnizor", errors=erori)
    
        options: list[dict[str, str]] = []
        for client in details:
            client_name = str(client.get("ClientName") or "").strip().title()
            for contract in client.get("to_ContContract", []) or []:
                for loc in contract.get("to_LocConsum", []) or []:
                    nlc = str(loc.get("IdLocConsum") or "")
                    if not nlc:
                        continue
                    parts: list[str] = []
                    street = str(loc.get("Street") or "").strip().title()
                    nr = str(loc.get("HouseNumber") or "").strip()
                    if street and nr:
                        parts.append(f"{street} {nr}")
                    elif street:
                        parts.append(street)
                    city = str(loc.get("City") or "").strip().title()
                    if city:
                        parts.append(city)
                    label = " — ".join([", ".join(parts) if parts else f"NLC {nlc}", nlc])
                    service = str(loc.get("ServiceType") or "").strip()
                    if service:
                        label += f" ({service})"
                    if client_name:
                        label = f"{client_name} | {label}"
                    options.append({"value": nlc, "label": label})
    
        self._myelectrica_optiuni = options
        return await self.async_step_selectare_puncte_myelectrica()
    
    async def async_step_selectare_puncte_myelectrica(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if not self._furnizor or self._furnizor != "myelectrica":
            return await self.async_step_credentiale_furnizor(self._date_utilizator_curente)
    
        if user_input is not None:
            selectie = [str(x) for x in (user_input.get(CONF_PUNCTE_CONSUM_SELECTATE) or []) if str(x).strip()]
            if not selectie:
                erori["base"] = "contract_invalid"
            else:
                unique = (self._date_utilizator_curente or {}).get(CONF_UTILIZATOR, "myelectrica").lower()
                if selectie:
                    unique = selectie[0]
                await self.async_set_unique_id(f"{self._furnizor}::{unique}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=obtine_clasa_furnizor(self._furnizor).nume_prietenos,
                    data={
                        CONF_FURNIZOR: self._furnizor,
                        CONF_CHEIE_LICENTA: self._cheie_licenta,
                        CONF_PUNCTE_CONSUM_SELECTATE: selectie,
                        **(self._date_utilizator_curente or {}),
                    },
                )
    
        return self.async_show_form(
            step_id="selectare_puncte_myelectrica",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PUNCTE_CONSUM_SELECTATE, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._myelectrica_optiuni,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=erori,
        )

    async def _proceseaza_flux_eon(
        self,
        user_input: dict[str, Any],
        erori: dict[str, str],
    ) -> ConfigFlowResult:
        self._date_utilizator_curente = {
            CONF_UTILIZATOR: user_input[CONF_UTILIZATOR],
            CONF_PAROLA: user_input[CONF_PAROLA],
            CONF_INTERVAL_ACTUALIZARE: int(
                user_input.get(CONF_INTERVAL_ACTUALIZARE, IMPLICIT_INTERVAL_ACTUALIZARE_ORE)
            ),
        }
        self._api_eon = EonApiClient(
            async_create_clientsession(self.hass),
            user_input[CONF_UTILIZATOR],
            user_input[CONF_PAROLA],
        )
        try:
            ok = await self._api_eon.async_login()
        except Exception:
            _LOGGER.exception("Eroare neașteptată în autentificarea E.ON")
            erori["base"] = "necunoscuta"
            return self.async_show_form(step_id="credentiale_furnizor", errors=erori)


        if ok:
            token_data = self._api_eon.export_token_data()
            unique = user_input[CONF_UTILIZATOR].lower()
            try:
                contracte = await self._api_eon.async_fetch_contracts_list()
                if isinstance(contracte, list) and contracte:
                    unique = str(contracte[0].get("accountContract") or unique)
            except Exception:
                pass
            await self.async_set_unique_id(f"{self._furnizor}::{unique}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=obtine_clasa_furnizor(self._furnizor).nume_prietenos,
                data={
                    CONF_FURNIZOR: self._furnizor,
                    CONF_CHEIE_LICENTA: self._cheie_licenta,
                    CONF_DATE_TOKEN_EON: token_data,
                    **user_input,
                },
            )

        if self._api_eon and self._api_eon.mfa_required:
            return await self.async_step_eon_cod_email()

        erori["base"] = "autentificare_esuata"
        return self.async_show_form(step_id="credentiale_furnizor", errors=erori)

    def _schema_cod_2fa_eon(self) -> vol.Schema:
        """Returnează schema simplă pentru codul 2FA E.ON.

        Folosim un câmp string simplu, nu selector text, pentru că în anumite
        versiuni/frontend-uri Home Assistant selectorul poate randa formularul
        fără câmp vizibil. Schema simplă este mai robustă și se comportă ca un
        input text normal.
        """
        return vol.Schema({vol.Required("cod_email", default=""): str})

    def _afiseaza_formular_cod_2fa_eon(
        self,
        erori: dict[str, str] | None = None,
        *,
        step_id: str = "eon_cod_email",
    ) -> ConfigFlowResult:
        """Afișează formularul pentru codul 2FA E.ON."""
        destinatar = self._api_eon.pending_email_masked if self._api_eon else "aplicația E.ON"
        schema = self._schema_cod_2fa_eon()
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=erori or {},
            description_placeholders={"destinatar": destinatar},
        )

    async def async_step_eon_cod_email(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}

        if self._api_eon is None:
            erori["base"] = "autentificare_esuata"
            return self._afiseaza_formular_cod_2fa_eon(erori)

        if user_input is not None:
            cod = str(user_input.get("cod_email") or "").strip()
            if not cod:
                erori["base"] = "cod_invalid"
                return self._afiseaza_formular_cod_2fa_eon(erori)

            try:
                ok = await self._api_eon.async_mfa_complete(cod)
            except Exception:
                _LOGGER.exception("Eroare neașteptată la completarea MFA E.ON")
                ok = False


            if ok:
                token_data = self._api_eon.export_token_data()
                unique = (self._date_utilizator_curente or {}).get(CONF_UTILIZATOR, "eon").lower()
                try:
                    contracte = await self._api_eon.async_fetch_contracts_list()
                    if isinstance(contracte, list) and contracte:
                        unique = str(contracte[0].get("accountContract") or unique)
                except Exception:
                    pass
                await self.async_set_unique_id(f"{self._furnizor}::{unique}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=obtine_clasa_furnizor(self._furnizor).nume_prietenos,
                    data={
                        CONF_FURNIZOR: self._furnizor,
                        CONF_CHEIE_LICENTA: self._cheie_licenta,
                        CONF_DATE_TOKEN_EON: token_data,
                        **(self._date_utilizator_curente or {}),
                    },
                )
            erori["base"] = "cod_invalid"

        return self._afiseaza_formular_cod_2fa_eon(erori)

    async def _proceseaza_flux_digi(
        self,
        user_input: dict[str, Any],
        erori: dict[str, str],
    ) -> ConfigFlowResult:
        self._api_digi = DigiApiClient(async_get_clientsession(self.hass))
        self._digi_pending = {
            CONF_UTILIZATOR: user_input[CONF_UTILIZATOR],
            CONF_PAROLA: user_input[CONF_PAROLA],
            CONF_INTERVAL_ACTUALIZARE: int(
                user_input.get(CONF_INTERVAL_ACTUALIZARE, IMPLICIT_INTERVAL_ACTUALIZARE_ORE)
            ),
            CONF_DIGI_HISTORY_LIMIT: int(
                user_input.get(CONF_DIGI_HISTORY_LIMIT, IMPLICIT_DIGI_HISTORY_LIMIT)
            ),
        }

        try:
            final_url, html = await self._api_digi.login(
                user_input[CONF_UTILIZATOR],
                user_input[CONF_PAROLA],
            )
        except DigiAuthError:
            erori["base"] = "autentificare_esuata"
            return self.async_show_form(step_id="credentiale_furnizor", errors=erori)
        except Exception:
            _LOGGER.exception("Eroare neașteptată la autentificarea Digi")
            erori["base"] = "necunoscuta"
            return self.async_show_form(step_id="credentiale_furnizor", errors=erori)

        return await self._proceseaza_rezultat_autentificare_digi(final_url, html)

    async def _proceseaza_rezultat_autentificare_digi(
        self,
        final_url: str,
        html: str,
    ) -> ConfigFlowResult:
        if self._api_digi is None:
            raise RuntimeError("Fluxul Digi nu a fost inițializat")

        if "/auth/2fa" in final_url:
            try:
                self._digi_two_factor = await self._api_digi.get_2fa_context(html)
            except DigiTwoFactorRequired:
                return self.async_show_form(
                    step_id="credentiale_furnizor",
                    errors={"base": "digi_2fa_indisponibil"},
                )
            return await self.async_step_digi_metoda_2fa()

        if "/auth/address-select" in final_url:
            self._digi_address_options = await self._api_digi.get_address_options(html)
            if self._digi_address_options:
                return await self.async_step_digi_selectare_cont()

        return await self._finalizeaza_creare_intrare_digi()

    def _construieste_optiuni_tinta_2fa_digi(self, method: str) -> list[dict[str, str]]:
        if self._digi_two_factor is None:
            return []
        selected = self._digi_two_factor.methods.get(method) or {}
        options = selected.get("target_options") or []
        rezultate: list[dict[str, str]] = []
        for option in options:
            value = str(option.get("value") or "").strip()
            label = str(option.get("label") or value).strip()
            if value:
                rezultate.append({"value": value, "label": label})
        return rezultate

    async def async_step_digi_metoda_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if self._digi_two_factor is None or self._api_digi is None:
            return await self.async_step_credentiale_furnizor(self._date_utilizator_curente)

        available = list(self._digi_two_factor.methods.keys())
        if not available:
            return self.async_show_form(
                step_id="credentiale_furnizor",
                errors={"base": "digi_2fa_indisponibil"},
            )

        default_method = "sms" if "sms" in available else available[0]

        current_method = default_method
        if user_input is not None:
            current_method = str(user_input[CONF_DIGI_2FA_METHOD])
            selected_target = str(user_input.get(CONF_DIGI_2FA_TARGET, "") or "").strip()
            try:
                await self._api_digi.send_2fa_code(
                    self._digi_two_factor,
                    current_method,
                    selected_target or None,
                )
                self._digi_pending[CONF_DIGI_2FA_METHOD] = current_method
                if selected_target:
                    self._digi_pending[CONF_DIGI_2FA_TARGET] = selected_target
                else:
                    self._digi_pending.pop(CONF_DIGI_2FA_TARGET, None)
                return await self.async_step_digi_cod_2fa()
            except DigiTwoFactorError:
                erori["base"] = "digi_trimitere_cod_esuata"

        target_options = self._construieste_optiuni_tinta_2fa_digi(current_method)
        schema_data: dict[Any, Any] = {
            vol.Required(CONF_DIGI_2FA_METHOD, default=current_method): SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": value, "label": value.upper()} for value in available],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
        if current_method == "sms" and len(target_options) > 1:
            implicit_target = str(
                (user_input or {}).get(CONF_DIGI_2FA_TARGET)
                or self._digi_pending.get(CONF_DIGI_2FA_TARGET)
                or target_options[0]["value"]
            )
            schema_data[vol.Required(CONF_DIGI_2FA_TARGET, default=implicit_target)] = SelectSelector(
                SelectSelectorConfig(
                    options=target_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="digi_metoda_2fa",
            data_schema=vol.Schema(schema_data),
            errors=erori,
        )

    async def async_step_digi_cod_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if self._digi_two_factor is None or self._api_digi is None:
            return await self.async_step_credentiale_furnizor(self._date_utilizator_curente)

        if user_input is not None:
            try:
                final_url, html = await self._api_digi.validate_2fa_code(
                    self._digi_two_factor,
                    self._digi_pending[CONF_DIGI_2FA_METHOD],
                    str(user_input["cod_2fa"]),
                )
                return await self._proceseaza_rezultat_autentificare_digi(final_url, html)
            except DigiTwoFactorError:
                erori["base"] = "cod_invalid"
            except Exception:
                _LOGGER.exception("Eroare neașteptată la validarea codului Digi")
                erori["base"] = "necunoscuta"

        return self.async_show_form(
            step_id="digi_cod_2fa",
            data_schema=vol.Schema(
                {
                    vol.Required("cod_2fa"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    )
                }
            ),
            errors=erori,
        )

    async def async_step_digi_selectare_cont(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if not self._digi_address_options or self._api_digi is None:
            return await self._finalizeaza_creare_intrare_digi()

        if user_input is not None:
            account_id = str(user_input[CONF_DIGI_SELECTED_ACCOUNT_ID])
            try:
                await self._api_digi.confirm_address(account_id)
                selected = next(
                    (item for item in self._digi_address_options if item.value == account_id),
                    None,
                )
                self._digi_pending[CONF_DIGI_SELECTED_ACCOUNT_ID] = account_id
                self._digi_pending[CONF_DIGI_SELECTED_ACCOUNT_LABEL] = (
                    selected.label if selected else account_id
                )
                return await self._finalizeaza_creare_intrare_digi()
            except DigiAccountSelectionRequired:
                erori["base"] = "digi_selectare_cont_invalida"

        return self.async_show_form(
            step_id="digi_selectare_cont",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DIGI_SELECTED_ACCOUNT_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": item.value, "label": item.label}
                                for item in self._digi_address_options
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=erori,
        )

    async def _finalizeaza_creare_intrare_digi(self) -> ConfigFlowResult:
        assert self._api_digi is not None
        selected_id = self._digi_pending.get(CONF_DIGI_SELECTED_ACCOUNT_ID)
        unique = f"{self._digi_pending[CONF_UTILIZATOR].lower()}::{selected_id or 'default'}"
        await self.async_set_unique_id(f"{self._furnizor}::{unique}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=obtine_clasa_furnizor(self._furnizor).nume_prietenos,
            data={
                CONF_FURNIZOR: self._furnizor,
                CONF_CHEIE_LICENTA: self._cheie_licenta,
                CONF_UTILIZATOR: self._digi_pending[CONF_UTILIZATOR],
                CONF_PAROLA: self._digi_pending[CONF_PAROLA],
                CONF_INTERVAL_ACTUALIZARE: self._digi_pending[CONF_INTERVAL_ACTUALIZARE],
                CONF_DIGI_HISTORY_LIMIT: self._digi_pending[CONF_DIGI_HISTORY_LIMIT],
                CONF_DIGI_SELECTED_ACCOUNT_ID: selected_id,
                CONF_DIGI_SELECTED_ACCOUNT_LABEL: self._digi_pending.get(CONF_DIGI_SELECTED_ACCOUNT_LABEL),
                CONF_DIGI_COOKIES: self._api_digi.export_cookies(),
            },
        )

    async def async_step_admin_global(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Păstrat doar pentru compatibilitate cu instalări mai vechi."""
        return await self.async_step_admin_bootstrap(user_input)

    async def async_step_admin_bootstrap(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Creează entry-ul global de administrare fără a-l expune ca furnizor normal."""
        erori: dict[str, str] = {}
        licenta_globala = await async_obtine_licenta_globala(self.hass)

        if user_input is None:
            user_input = {
                CONF_UTILIZATOR: str(licenta_globala.get(CONF_UTILIZATOR, "")).strip(),
                CONF_CHEIE_LICENTA: str(licenta_globala.get(CONF_CHEIE_LICENTA, "TRIAL")).strip() or "TRIAL",
            }

        utilizator = str(user_input.get(CONF_UTILIZATOR, "")).strip()
        cheie = str(user_input.get(CONF_CHEIE_LICENTA, "TRIAL")).strip() or "TRIAL"
        if not utilizator:
            erori["base"] = "licenta_invalida"
        else:
            try:
                rezultat_licenta = await async_valideaza_licenta(self.hass, cheie, utilizator)
                valideaza_rezultat_licenta(rezultat_licenta)
            except Exception:
                erori["base"] = "licenta_invalida"
            else:
                await async_salveaza_licenta_globala(
                    self.hass,
                    cheie,
                    utilizator,
                    rezultat_licenta,
                )
                await self.async_set_unique_id(f"{FURNIZOR_ADMIN_GLOBAL}::global")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Administrare integrare",
                    data={
                        CONF_FURNIZOR: FURNIZOR_ADMIN_GLOBAL,
                        CONF_UTILIZATOR: utilizator,
                        CONF_CHEIE_LICENTA: cheie,
                        DATE_VERIFICARE_LICENTA: rezultat_licenta.ca_dict(),
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UTILIZATOR,
                    default=str(licenta_globala.get(CONF_UTILIZATOR, "")).strip(),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Required(
                    CONF_CHEIE_LICENTA,
                    default=str(licenta_globala.get(CONF_CHEIE_LICENTA, "TRIAL")).strip() or "TRIAL",
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            }
        )
        return self.async_show_form(step_id="admin_bootstrap", data_schema=schema, errors=erori)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._furnizor = entry_data[CONF_FURNIZOR]
        self._cheie_licenta = entry_data.get(CONF_CHEIE_LICENTA, "TRIAL")
        return await self.async_step_confirmare_reautentificare()

    def _get_reauth_entry(self):
        entry_id = self.context.get("entry_id")
        if entry_id:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None:
                return entry
            _LOGGER.warning("Reautentificare: entry_id %s nu a fost gasit.", entry_id)

        entries = [
            entry
            for entry in self._async_current_entries()
            if entry.data.get(CONF_FURNIZOR) == self._furnizor
        ]

        if not entries:
            raise RuntimeError("Nu exista intrare pentru reautentificare")

        if len(entries) > 1:
            _LOGGER.warning(
                "Reautentificare: exista mai multe intrari pentru furnizorul %s; "
                "se foloseste prima intrare gasita ca fallback.",
                self._furnizor,
            )

        return entries[0]

    async def async_step_confirmare_reautentificare(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        intrare = self._get_reauth_entry()
        clasa_furnizor = obtine_clasa_furnizor(self._furnizor)
        erori: dict[str, str] = {}

        if user_input is not None:
            self._date_utilizator_curente = {
                CONF_UTILIZATOR: user_input[CONF_UTILIZATOR],
                CONF_PAROLA: user_input[CONF_PAROLA],
            }

            if self._furnizor == "eon":
                self._api_eon = EonApiClient(
                    async_create_clientsession(self.hass),
                    user_input[CONF_UTILIZATOR],
                    user_input[CONF_PAROLA],
                )
                try:
                    ok = await self._api_eon.async_login()
                except Exception:
                    _LOGGER.exception("Eroare neașteptată la reautentificarea E.ON")
                    ok = False
                if ok:
                    return self.async_update_reload_and_abort(
                        intrare,
                        data_updates={
                            CONF_UTILIZATOR: user_input[CONF_UTILIZATOR],
                            CONF_PAROLA: user_input[CONF_PAROLA],
                            CONF_DATE_TOKEN_EON: self._api_eon.export_token_data(),
                        },
                    )
                if self._api_eon and self._api_eon.mfa_required:
                    return await self.async_step_eon_cod_email_reauth()
                erori["base"] = "autentificare_esuata"

            elif self._furnizor == "digi":
                self._api_digi = DigiApiClient(async_get_clientsession(self.hass))
                self._digi_pending = {
                    CONF_UTILIZATOR: user_input[CONF_UTILIZATOR],
                    CONF_PAROLA: user_input[CONF_PAROLA],
                    CONF_DIGI_HISTORY_LIMIT: intrare.data.get(
                        CONF_DIGI_HISTORY_LIMIT,
                        IMPLICIT_DIGI_HISTORY_LIMIT,
                    ),
                    CONF_DIGI_SELECTED_ACCOUNT_ID: intrare.data.get(CONF_DIGI_SELECTED_ACCOUNT_ID),
                    CONF_DIGI_SELECTED_ACCOUNT_LABEL: intrare.data.get(CONF_DIGI_SELECTED_ACCOUNT_LABEL),
                }
                try:
                    final_url, html = await self._api_digi.login(
                        user_input[CONF_UTILIZATOR],
                        user_input[CONF_PAROLA],
                    )
                except DigiAuthError:
                    erori["base"] = "autentificare_esuata"
                except Exception:
                    _LOGGER.exception("Eroare neașteptată la reautentificarea Digi")
                    erori["base"] = "necunoscuta"
                else:
                    if "/auth/2fa" in final_url:
                        try:
                            self._digi_two_factor = await self._api_digi.get_2fa_context(html)
                        except DigiTwoFactorRequired:
                            erori["base"] = "digi_2fa_indisponibil"
                        else:
                            return await self.async_step_digi_reauth_metoda_2fa()
                    elif "/auth/address-select" in final_url:
                        self._digi_address_options = await self._api_digi.get_address_options(html)
                        if self._digi_address_options:
                            return await self.async_step_digi_reauth_selectare_cont()
                    else:
                        return await self._finalizeaza_reauth_digi()

            else:
                sesiune_dedicata = self._furnizor in {"deo", "retele_electrice"}
                sesiune = (
                    _creeaza_sesiune_dedicata(self.hass)
                    if sesiune_dedicata
                    else async_get_clientsession(self.hass)
                )
                client = clasa_furnizor(
                    sesiune=sesiune,
                    utilizator=user_input[CONF_UTILIZATOR],
                    parola=user_input[CONF_PAROLA],
                    optiuni=intrare.data,
                )
                try:
                    await client.async_testeaza_conexiunea()
                except EroareAutentificare:
                    erori["base"] = "autentificare_esuata"
                except EroareConectare:
                    erori["base"] = "nu_se_poate_conecta"
                except Exception:
                    _LOGGER.exception("Eroare neașteptată în reautentificare")
                    erori["base"] = "necunoscuta"
                else:
                    return self.async_update_reload_and_abort(
                        intrare,
                        data_updates={
                            CONF_UTILIZATOR: user_input[CONF_UTILIZATOR],
                            CONF_PAROLA: user_input[CONF_PAROLA],
                        },
                    )
                finally:
                    if sesiune_dedicata:
                        _elibereaza_sesiune_dedicata(sesiune)

        schema = vol.Schema(
            {
                vol.Required(CONF_UTILIZATOR, default=intrare.data.get(CONF_UTILIZATOR, "")): str,
                vol.Required(CONF_PAROLA): str,
            }
        )
        return self.async_show_form(
            step_id="confirmare_reautentificare",
            data_schema=schema,
            errors=erori,
            description_placeholders={"furnizor": clasa_furnizor.nume_prietenos},
        )

    async def async_step_eon_cod_email_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}

        if self._api_eon is None:
            erori["base"] = "autentificare_esuata"
            return self._afiseaza_formular_cod_2fa_eon(erori, step_id="eon_cod_email_reauth")

        if user_input is not None:
            cod = str(user_input.get("cod_email") or "").strip()
            if not cod:
                erori["base"] = "cod_invalid"
                return self._afiseaza_formular_cod_2fa_eon(erori, step_id="eon_cod_email_reauth")

            try:
                ok = await self._api_eon.async_mfa_complete(cod)
            except Exception:
                _LOGGER.exception("Eroare neașteptată la completarea MFA E.ON la reautentificare")
                ok = False

            if ok:
                intrare = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    intrare,
                    data_updates={
                        CONF_UTILIZATOR: self._date_utilizator_curente[CONF_UTILIZATOR],
                        CONF_PAROLA: self._date_utilizator_curente[CONF_PAROLA],
                        CONF_DATE_TOKEN_EON: self._api_eon.export_token_data(),
                    },
                )
            erori["base"] = "cod_invalid"

        return self._afiseaza_formular_cod_2fa_eon(erori, step_id="eon_cod_email_reauth")

    async def async_step_digi_reauth_metoda_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if self._digi_two_factor is None or self._api_digi is None:
            return await self.async_step_confirmare_reautentificare()

        available = list(self._digi_two_factor.methods.keys())
        if not available:
            return self.async_show_form(
                step_id="confirmare_reautentificare",
                errors={"base": "digi_2fa_indisponibil"},
            )

        default_method = "sms" if "sms" in available else available[0]

        current_method = default_method
        if user_input is not None:
            current_method = str(user_input[CONF_DIGI_2FA_METHOD])
            selected_target = str(user_input.get(CONF_DIGI_2FA_TARGET, "") or "").strip()
            try:
                await self._api_digi.send_2fa_code(
                    self._digi_two_factor,
                    current_method,
                    selected_target or None,
                )
                self._digi_pending[CONF_DIGI_2FA_METHOD] = current_method
                if selected_target:
                    self._digi_pending[CONF_DIGI_2FA_TARGET] = selected_target
                else:
                    self._digi_pending.pop(CONF_DIGI_2FA_TARGET, None)
                return await self.async_step_digi_reauth_cod_2fa()
            except DigiTwoFactorError:
                erori["base"] = "digi_trimitere_cod_esuata"

        target_options = self._construieste_optiuni_tinta_2fa_digi(current_method)
        schema_data: dict[Any, Any] = {
            vol.Required(CONF_DIGI_2FA_METHOD, default=current_method): SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": value, "label": value.upper()} for value in available],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
        if current_method == "sms" and len(target_options) > 1:
            implicit_target = str(
                (user_input or {}).get(CONF_DIGI_2FA_TARGET)
                or self._digi_pending.get(CONF_DIGI_2FA_TARGET)
                or target_options[0]["value"]
            )
            schema_data[vol.Required(CONF_DIGI_2FA_TARGET, default=implicit_target)] = SelectSelector(
                SelectSelectorConfig(
                    options=target_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="digi_reauth_metoda_2fa",
            data_schema=vol.Schema(schema_data),
            errors=erori,
        )

    async def async_step_digi_reauth_cod_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if self._digi_two_factor is None or self._api_digi is None:
            return await self.async_step_confirmare_reautentificare()

        if user_input is not None:
            try:
                final_url, html = await self._api_digi.validate_2fa_code(
                    self._digi_two_factor,
                    self._digi_pending[CONF_DIGI_2FA_METHOD],
                    str(user_input["cod_2fa"]),
                )
                if "/auth/address-select" in final_url:
                    self._digi_address_options = await self._api_digi.get_address_options(html)
                    if self._digi_address_options:
                        return await self.async_step_digi_reauth_selectare_cont()
                return await self._finalizeaza_reauth_digi()
            except DigiTwoFactorError:
                erori["base"] = "cod_invalid"
            except Exception:
                _LOGGER.exception("Eroare neașteptată la validarea codului Digi în reauth")
                erori["base"] = "necunoscuta"

        return self.async_show_form(
            step_id="digi_reauth_cod_2fa",
            data_schema=vol.Schema(
                {
                    vol.Required("cod_2fa"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    )
                }
            ),
            errors=erori,
        )

    async def async_step_digi_reauth_selectare_cont(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if not self._digi_address_options or self._api_digi is None:
            return await self._finalizeaza_reauth_digi()

        if user_input is not None:
            account_id = str(user_input[CONF_DIGI_SELECTED_ACCOUNT_ID])
            try:
                await self._api_digi.confirm_address(account_id)
                selected = next(
                    (item for item in self._digi_address_options if item.value == account_id),
                    None,
                )
                self._digi_pending[CONF_DIGI_SELECTED_ACCOUNT_ID] = account_id
                self._digi_pending[CONF_DIGI_SELECTED_ACCOUNT_LABEL] = (
                    selected.label if selected else account_id
                )
                return await self._finalizeaza_reauth_digi()
            except DigiAccountSelectionRequired:
                erori["base"] = "digi_selectare_cont_invalida"

        return self.async_show_form(
            step_id="digi_reauth_selectare_cont",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DIGI_SELECTED_ACCOUNT_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": item.value, "label": item.label}
                                for item in self._digi_address_options
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=erori,
        )

    async def _finalizeaza_reauth_digi(self) -> ConfigFlowResult:
        intrare = self._get_reauth_entry()
        assert self._api_digi is not None
        return self.async_update_reload_and_abort(
            intrare,
            data_updates={
                CONF_UTILIZATOR: self._digi_pending[CONF_UTILIZATOR],
                CONF_PAROLA: self._digi_pending[CONF_PAROLA],
                CONF_DIGI_HISTORY_LIMIT: self._digi_pending[CONF_DIGI_HISTORY_LIMIT],
                CONF_DIGI_SELECTED_ACCOUNT_ID: self._digi_pending.get(CONF_DIGI_SELECTED_ACCOUNT_ID),
                CONF_DIGI_SELECTED_ACCOUNT_LABEL: self._digi_pending.get(CONF_DIGI_SELECTED_ACCOUNT_LABEL),
                CONF_DIGI_COOKIES: self._api_digi.export_cookies(),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FluxOptiuniUtilitatiRomania(config_entry)


class FluxOptiuniUtilitatiRomania(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        erori: dict[str, str] = {}
        if user_input is not None:
            cheie_licenta = str(user_input[CONF_CHEIE_LICENTA]).strip() or "TRIAL"
            utilizator = str(user_input[CONF_UTILIZATOR]).strip()
            try:
                rezultat_licenta = await async_valideaza_licenta(self.hass, cheie_licenta, utilizator)
                valideaza_rezultat_licenta(rezultat_licenta)
            except Exception:
                erori["base"] = "licenta_invalida"
            else:
                await async_salveaza_licenta_globala(self.hass, cheie_licenta, utilizator, rezultat_licenta)
                if self._config_entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
                    data = {
                        CONF_UTILIZATOR: utilizator,
                        CONF_CHEIE_LICENTA: cheie_licenta,
                    }
                    dispozitiv_mobil = self._config_entry.options.get(CONF_MOBILE_NOTIFY_SERVICE)
                    if dispozitiv_mobil:
                        data[CONF_MOBILE_NOTIFY_SERVICE] = dispozitiv_mobil
                    return self.async_create_entry(title="", data=data)

                data = {
                    CONF_UTILIZATOR: utilizator,
                    CONF_PAROLA: str(user_input[CONF_PAROLA]),
                    CONF_CHEIE_LICENTA: cheie_licenta,
                    CONF_INTERVAL_ACTUALIZARE: int(user_input[CONF_INTERVAL_ACTUALIZARE]),
                }
                if self._config_entry.data.get(CONF_FURNIZOR) == "myelectrica":
                    data[CONF_PUNCTE_CONSUM_SELECTATE] = [
                        str(x) for x in (user_input.get(CONF_PUNCTE_CONSUM_SELECTATE) or []) if str(x).strip()
                    ]
                if self._config_entry.data.get(CONF_FURNIZOR) == "digi":
                    data[CONF_DIGI_HISTORY_LIMIT] = int(
                        user_input.get(
                            CONF_DIGI_HISTORY_LIMIT,
                            self._config_entry.options.get(
                                CONF_DIGI_HISTORY_LIMIT,
                                self._config_entry.data.get(CONF_DIGI_HISTORY_LIMIT, IMPLICIT_DIGI_HISTORY_LIMIT),
                            ),
                        )
                    )
                if self._config_entry.data.get(CONF_FURNIZOR) == "retele_electrice":
                    data[CONF_RETELE_INTERVAL_DATE_INSTANTANEE] = int(
                        user_input.get(
                            CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
                            self._config_entry.options.get(
                                CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
                                self._config_entry.data.get(
                                    CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
                                    IMPLICIT_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
                                ),
                            ),
                        )
                    )
                return self.async_create_entry(title="", data=data)

        if self._config_entry.data.get(CONF_FURNIZOR) == FURNIZOR_ADMIN_GLOBAL:
            licenta_globala = await async_obtine_licenta_globala(self.hass)
            util_implicit = self._config_entry.options.get(
                CONF_UTILIZATOR,
                licenta_globala.get(CONF_UTILIZATOR, self._config_entry.data.get(CONF_UTILIZATOR, "")),
            )
            cheie_implicita = str(licenta_globala.get(CONF_CHEIE_LICENTA) or self._config_entry.options.get(
                CONF_CHEIE_LICENTA,
                self._config_entry.data.get(CONF_CHEIE_LICENTA, "TRIAL"),
            ))
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({
                    vol.Required(CONF_UTILIZATOR, default=util_implicit): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                    vol.Required(CONF_CHEIE_LICENTA, default=cheie_implicita): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                }),
                errors=erori,
            )

        util_implicit = self._config_entry.options.get(
            CONF_UTILIZATOR,
            self._config_entry.data.get(CONF_UTILIZATOR, ""),
        )
        parola_implicita = self._config_entry.options.get(
            CONF_PAROLA,
            self._config_entry.data.get(CONF_PAROLA, ""),
        )
        licenta_globala = await async_obtine_licenta_globala(self.hass)
        cheie_implicita = str(licenta_globala.get(CONF_CHEIE_LICENTA) or self._config_entry.options.get(
            CONF_CHEIE_LICENTA,
            self._config_entry.data.get(CONF_CHEIE_LICENTA, "TRIAL"),
        ))
        interval_implicit = self._config_entry.options.get(
            CONF_INTERVAL_ACTUALIZARE,
            self._config_entry.data.get(CONF_INTERVAL_ACTUALIZARE, IMPLICIT_INTERVAL_ACTUALIZARE_ORE),
        )
        try:
            interval_implicit = int(interval_implicit)
        except (TypeError, ValueError):
            interval_implicit = IMPLICIT_INTERVAL_ACTUALIZARE_ORE

        schema_items: dict[Any, Any] = {
            vol.Required(CONF_UTILIZATOR, default=util_implicit): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_PAROLA, default=parola_implicita): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_CHEIE_LICENTA, default=cheie_implicita): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_INTERVAL_ACTUALIZARE, default=interval_implicit): NumberSelector(
                NumberSelectorConfig(
                    min=MINIM_INTERVAL_ACTUALIZARE_ORE,
                    max=MAXIM_INTERVAL_ACTUALIZARE_ORE,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }

        if self._config_entry.data.get(CONF_FURNIZOR) == "myelectrica":
            options_me: list[dict[str, str]] = []
            try:
                api = ClientApiMyElectrica(
                    async_get_clientsession(self.hass),
                    self._config_entry.options.get(CONF_UTILIZATOR, self._config_entry.data.get(CONF_UTILIZATOR, "")),
                    self._config_entry.options.get(CONF_PAROLA, self._config_entry.data.get(CONF_PAROLA, "")),
                )
                if await api.async_login():
                    hierarchy_raw = await api.async_get_hierarchy()
                    details = hierarchy_raw.get("details") if isinstance(hierarchy_raw, dict) else []
                    for client in details or []:
                        client_name = str(client.get("ClientName") or "").strip().title()
                        for contract in client.get("to_ContContract", []) or []:
                            for loc in contract.get("to_LocConsum", []) or []:
                                nlc = str(loc.get("IdLocConsum") or "")
                                if not nlc:
                                    continue
                                parts: list[str] = []
                                street = str(loc.get("Street") or "").strip().title()
                                nr = str(loc.get("HouseNumber") or "").strip()
                                if street and nr:
                                    parts.append(f"{street} {nr}")
                                elif street:
                                    parts.append(street)
                                city = str(loc.get("City") or "").strip().title()
                                if city:
                                    parts.append(city)
                                label = " — ".join([", ".join(parts) if parts else f"NLC {nlc}", nlc])
                                service = str(loc.get("ServiceType") or "").strip()
                                if service:
                                    label += f" ({service})"
                                if client_name:
                                    label = f"{client_name} | {label}"
                                options_me.append({"value": nlc, "label": label})
            except Exception:
                _LOGGER.exception("Nu am putut încărca punctele myElectrica în options flow")

            selected_default = self._config_entry.options.get(
                CONF_PUNCTE_CONSUM_SELECTATE,
                self._config_entry.data.get(CONF_PUNCTE_CONSUM_SELECTATE, []),
            )
            schema_items[vol.Required(CONF_PUNCTE_CONSUM_SELECTATE, default=selected_default)] = SelectSelector(
                SelectSelectorConfig(
                    options=options_me,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )

        if self._config_entry.data.get(CONF_FURNIZOR) == "digi":
            history_default = self._config_entry.options.get(
                CONF_DIGI_HISTORY_LIMIT,
                self._config_entry.data.get(CONF_DIGI_HISTORY_LIMIT, IMPLICIT_DIGI_HISTORY_LIMIT),
            )
            schema_items[vol.Required(CONF_DIGI_HISTORY_LIMIT, default=history_default)] = NumberSelector(
                NumberSelectorConfig(
                    min=MINIM_DIGI_HISTORY_LIMIT,
                    max=MAXIM_DIGI_HISTORY_LIMIT,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            )

        if self._config_entry.data.get(CONF_FURNIZOR) == "retele_electrice":
            interval_contor = self._config_entry.options.get(
                CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
                self._config_entry.data.get(
                    CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
                    IMPLICIT_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
                ),
            )
            try:
                interval_contor = int(interval_contor)
            except (TypeError, ValueError):
                interval_contor = IMPLICIT_RETELE_INTERVAL_DATE_INSTANTANEE_ORE
            schema_items[
                vol.Required(
                    CONF_RETELE_INTERVAL_DATE_INSTANTANEE,
                    default=interval_contor,
                )
            ] = NumberSelector(
                NumberSelectorConfig(
                    min=MINIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
                    max=MAXIM_RETELE_INTERVAL_DATE_INSTANTANEE_ORE,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            )

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_items), errors=erori)
