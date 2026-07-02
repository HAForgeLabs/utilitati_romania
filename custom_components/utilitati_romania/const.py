from __future__ import annotations

from homeassistant.const import Platform

DOMENIU = "utilitati_romania"
PLATFORME: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.TEXT,
]

CONF_FURNIZOR = "furnizor"
CONF_UTILIZATOR = "utilizator"
CONF_PAROLA = "parola"
CONF_INTERVAL_ACTUALIZARE = "interval_actualizare"
CONF_CHEIE_LICENTA = "cheie_licenta"
CONF_PUNCTE_CONSUM_SELECTATE = "puncte_consum_selectate"
CONF_ACCOUNT_ID = "account_id"
CONF_CONTRACT_ID = "contract_id"
CONF_CONTRACT_ACCOUNT_ID = "contract_account_id"
CONF_PREMISE_LABEL = "premise_label"
DATE_VERIFICARE_LICENTA = "date_verificare_licenta"

IMPLICIT_INTERVAL_ACTUALIZARE_ORE = 6
MINIM_INTERVAL_ACTUALIZARE_ORE = 1
MAXIM_INTERVAL_ACTUALIZARE_ORE = 24
IMPLICIT_ZILE_GRATIE_LICENTA = 7
IMPLICIT_ORE_VERIFICARE_LICENTA = 24

URL_API_LICENTA = "https://license-api.marius-onitiu.workers.dev"
LICENTA_STATUS_ACTIVA = "active"
LICENTA_STATUS_TRIAL = "trial"
LICENTA_STATUS_INVALIDA = "invalid"
LICENTA_STATUS_EXPIRATA = "expired"
LICENTA_STATUS_REVOCATA = "revoked"
LICENTA_STATUS_PRODUS_INVALID = "invalid_product"
LICENTA_STATUS_ACTIVATION_LIMIT = "activation_limit"
LICENTA_STATUS_NECUNOSCUT = "unknown"

ATRIBUT_FURNIZOR = "furnizor"
ATRIBUT_ID_CONT = "id_cont"
ATRIBUT_NUME_CONT = "nume_cont"
ATRIBUT_ID_CONTRACT = "id_contract"
ATRIBUT_DATE_BRUTE = "date_brute"

FURNIZOR_DIGI = "digi"
FURNIZOR_NOVA = "nova"
FURNIZOR_EON = "eon"
FURNIZOR_APA_CANAL = "apa_canal"
FURNIZOR_APA_BRASOV = "apa_brasov"
FURNIZOR_APA_ORADEA = "apa_oradea"
FURNIZOR_APA_GALATI = "apa_galati"
FURNIZOR_APAREGIO = "aparegio"
FURNIZOR_POLARIS = "polaris"
FURNIZOR_HIDROELECTRICA = "hidroelectrica"
FURNIZOR_HIDRO_PRAHOVA = "hidro_prahova"
FURNIZOR_MYELECTRICA = "myelectrica"
FURNIZOR_DEER = "deer"
FURNIZOR_EBLOC = "ebloc"
FURNIZOR_ORANGE = "orange"
FURNIZOR_RERVEST = "rervest"
FURNIZOR_COMPREST = "comprest"
FURNIZOR_ENGIE = "engie"

CONF_DATE_TOKEN_EON = "date_token_eon"

CONF_DIGI_COOKIES = "digi_cookies"
CONF_DIGI_2FA_METHOD = "digi_2fa_method"
CONF_DIGI_2FA_TARGET = "digi_2fa_target"
CONF_DIGI_SELECTED_ACCOUNT_ID = "digi_selected_account_id"
CONF_DIGI_SELECTED_ACCOUNT_LABEL = "digi_selected_account_label"
CONF_DIGI_HISTORY_LIMIT = "digi_history_limit"

IMPLICIT_DIGI_HISTORY_LIMIT = 6
MINIM_DIGI_HISTORY_LIMIT = 1
MAXIM_DIGI_HISTORY_LIMIT = 24

BASE_URL = "https://www.digi.ro"
LOGIN_URL = f"{BASE_URL}/auth/login?redirectTo=%2F"
TWO_FA_URL = f"{BASE_URL}/auth/2fa?redirectTo=%2F"
TWO_FA_SEND_URL = f"{BASE_URL}/api-post-2fa-send-code"
TWO_FA_VALIDATE_URL = f"{BASE_URL}/api-post-2fa-validate-code"
ADDRESS_SELECT_URL = f"{BASE_URL}/auth/address-select?redirectTo=%2F"
ADDRESS_CONFIRM_URL = f"{BASE_URL}/store/address-confirm-existing"
INVOICES_URL = f"{BASE_URL}/my-account/invoices"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

FURNIZOR_ADMIN_GLOBAL = "admin_global"
SERVICIU_RELOAD_ALL = "reload_all"
SERVICIU_OPEN_PROVIDER = "open_provider"
SERVICIU_SET_INVOICE_STATUS = "set_invoice_status"
SERVICIU_SUBMIT_READING = "submit_reading"
SERVICIU_SET_NOTIFICATION_PREFERENCES = "set_notification_preferences"
SERVICIU_SET_CONSUMPTION_POINT_VISIBILITY = "set_consumption_point_visibility"

CONF_MOBILE_NOTIFY_SERVICE = "mobile_notify_service"
CONF_MOBILE_NOTIFICATION_SERVICE = "mobile_notification_service"
