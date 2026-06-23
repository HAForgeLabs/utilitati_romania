from __future__ import annotations

import re
import unicodedata
from typing import Any


_GENERIC_TOKENS = {
    "romania",
    "românia",
    "jud",
    "judet",
    "judetul",
    "municipiul",
    "oras",
    "oraș",
    "comuna",
    "sat",
    "loc",
    "locul",
    "consum",
    "consumului",
    "punct",
    "punctul",
    "pod",
    "contract",
    "client",
    "adresa",
    "adresae",
    "adresă",
    "cod",
    "postal",
    "poștal",
    "post",
    "tara",
    "țara",
    "country",
}

_STREET_PREFIX_CANONICAL = {
    "str": "str",
    "str.": "str",
    "strada": "str",
    "bd": "bd",
    "bd.": "bd",
    "bulevard": "bd",
    "bulevardul": "bd",
    "calea": "calea",
    "aleea": "aleea",
    "alee": "aleea",
    "al": "aleea",
    "piata": "piata",
    "piața": "piata",
    "piata.": "piata",
    "sos": "sos",
    "sos.": "sos",
    "sosea": "sos",
    "soseaua": "sos",
    "șos": "sos",
    "șos.": "sos",
    "intrarea": "intrarea",
    "intrare": "intrarea",
    "drum": "drum",
    "drumul": "drum",
}

_SECONDARY_ADDRESS_MARKERS = (
    "bl",
    "bloc",
    "sc",
    "scara",
    "sc.",
    "et",
    "etaj",
    "et.",
    "ap",
    "apartament",
    "apt",
    "cam",
    "camera",
    "tronson",
    "tr",
    "corp",
    "cp",
    "cod",
    "postal",
    "poștal",
)

_LOCALITY_HINTS = (
    "sibiu",
    "selimbar",
    "șelimbăr",
    "bucuresti",
    "bucurești",
    "cluj",
    "cluj-napoca",
    "timisoara",
    "timișoara",
    "brasov",
    "brașov",
    "iasi",
    "iași",
    "constanta",
    "constanța",
    "craiova",
    "oradea",
    "ploiesti",
    "ploiești",
)


def _strip_diacritics(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _strip_diacritics(text).lower()
    text = (
        text.replace("ş", "s")
        .replace("ș", "s")
        .replace("ţ", "t")
        .replace("ț", "t")
        .replace("ă", "a")
        .replace("â", "a")
        .replace("î", "i")
    )
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_candidate(value: Any) -> str:
    text = str(value or "").strip(" ,;-")
    if not text:
        return ""
    normalized = normalize_text(text)
    if normalized in {"-", "n/a", "none", "unknown"}:
        return ""
    return text


def _extract_candidates(cont: Any) -> list[str]:
    candidates: list[str] = []

    def add(value: Any) -> None:
        cleaned = _clean_candidate(value)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    if cont is None:
        return candidates

    add(getattr(cont, "adresa", None))

    raw = getattr(cont, "date_brute", None)
    if isinstance(raw, dict):
        for key in (
            "adresa",
            "address",
            "service_address",
            "serviceAddress",
            "site_address",
            "siteAddress",
            "premise_label",
            "premiseLabel",
            "loc_consum",
            "adresa_loc_consum",
            "consumption_place",
            "consumptionPlaceName",
            "consumptionAddress",
            "usageAddress",
            "addressLine",
            "full_address",
            "property_address",
            "nume_loc_consum",
            "denumire_loc_consum",
            "alias_loc_consum",
        ):
            add(raw.get(key))

    add(getattr(cont, "nume", None))
    return candidates


def _canonicalize_prefix(token: str) -> str:
    return _STREET_PREFIX_CANONICAL.get(token, token)


def _remove_secondary_address_parts(text: str) -> str:
    value = normalize_text(text)

    value = re.sub(r"\bjud(?:et(?:ul)?)?\b.*$", "", value)
    value = re.sub(r"\bcod(?:ul)?\s+postal\b.*$", "", value)
    value = re.sub(r"\b\d{6}\b", "", value)

    for marker in _SECONDARY_ADDRESS_MARKERS:
        value = re.sub(rf"\b{re.escape(marker)}\.?\s*[:\-]?\s*[\w\-\/]+\b.*$", "", value)

    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ,-")


def _extract_locality(text: str) -> str:
    normalized = normalize_text(text)

    for locality in _LOCALITY_HINTS:
        candidate = normalize_text(locality)
        if candidate and re.search(rf"\b{re.escape(candidate)}\b", normalized):
            return candidate

    parts = [part.strip(" ,-") for part in normalized.split(",") if part.strip(" ,-")]
    for part in reversed(parts):
        if part in _GENERIC_TOKENS:
            continue
        if any(marker in part for marker in ("str ", "bd ", "calea ", "aleea ", "sos ", "intrarea ", "drum ")):
            continue
        if len(part) >= 3:
            return part

    return ""


def _extract_street_number_pair(text: str) -> tuple[str, str, str]:
    normalized = _remove_secondary_address_parts(text)

    pattern = re.compile(
        r"\b(strada|str\.?|bd\.?|bd|bulevardul|bulevard|calea|aleea|alee|al\.?|piata|sos\.?|soseaua|sosea|intrarea|intrare|drumul|drum)\b"
        r"[\s,]+"
        r"([a-z0-9][a-z0-9\s\-\./]{1,80}?)"
        r"(?:[\s,]+(?:nr\.?|numar(?:ul)?)\s*([\d]+[a-z]?))?"
        r"(?=$|[\s,]+(?:bl|bloc|sc|scara|et|etaj|ap|apartament|cp|cod)\b|,)",
        re.IGNORECASE,
    )

    match = pattern.search(normalized)
    if match:
        prefix = _canonicalize_prefix(normalize_text(match.group(1)))
        street_name = re.sub(r"\s+", " ", match.group(2)).strip(" ,.-")
        street_name = re.sub(
            r"\b(?:nr|numar|numarul|bl|bloc|sc|scara|et|etaj|ap|apartament)\b.*$",
            "",
            street_name,
        ).strip(" ,.-")
        number = (match.group(3) or "").strip(" ,.-")
        return prefix, street_name, number

    pattern_fallback = re.compile(
        r"\b(strada|str\.?|bd\.?|bd|bulevardul|bulevard|calea|aleea|alee|al\.?|piata|sos\.?|soseaua|sosea|intrarea|intrare|drumul|drum)\b"
        r"[\s,]+"
        r"([a-z0-9][a-z0-9\s\-\./]{1,80}?)"
        r"[\s,]+"
        r"([\d]+[a-z]?)\b",
        re.IGNORECASE,
    )
    match = pattern_fallback.search(normalized)
    if match:
        prefix = _canonicalize_prefix(normalize_text(match.group(1)))
        street_name = re.sub(r"\s+", " ", match.group(2)).strip(" ,.-")
        number = match.group(3).strip(" ,.-")
        return prefix, street_name, number

    compact = re.sub(r"[,\-]+", " ", normalized)
    tokens = [token for token in re.split(r"\s+", compact) if token]

    prefix = ""
    street_tokens: list[str] = []
    number = ""

    for idx, token in enumerate(tokens):
        canonical = _canonicalize_prefix(token)
        if canonical in {"str", "bd", "calea", "aleea", "piata", "sos", "intrarea", "drum"}:
            prefix = canonical
            for next_token in tokens[idx + 1 :]:
                if next_token in _GENERIC_TOKENS:
                    continue
                if next_token in _SECONDARY_ADDRESS_MARKERS:
                    break
                if re.fullmatch(r"\d+[a-z]?", next_token):
                    number = next_token
                    break
                if len(street_tokens) < 5:
                    street_tokens.append(next_token)
            break

    street_name = " ".join(street_tokens).strip(" ,.-")
    return prefix, street_name, number




def _extract_secondary_address_values(text: str) -> dict[str, str]:
    normalized = normalize_text(text)
    values: dict[str, str] = {}
    marker_map = {
        "bl": "bl",
        "bloc": "bl",
        "sc": "sc",
        "scara": "sc",
        "et": "et",
        "etaj": "et",
        "ap": "ap",
        "apt": "ap",
        "apartament": "ap",
    }
    pattern = re.compile(
        r"\b(bl|bloc|sc|scara|et|etaj|ap|apt|apartament)\.?\s*[:\-]?\s*([a-z0-9\-/]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(normalized):
        key = marker_map.get(match.group(1).strip().lower())
        value = str(match.group(2) or "").strip(" ,.-").lower()
        if key and value and key not in values:
            values[key] = value
    return values


def _secondary_key_suffix(text: str) -> str:
    values = _extract_secondary_address_values(text)
    parts: list[str] = []
    for key in ("bl", "sc", "et", "ap"):
        value = values.get(key)
        if value:
            parts.extend([key, value])
    if not parts:
        return ""
    suffix = "_".join(parts)
    suffix = re.sub(r"[^a-z0-9]+", "_", suffix)
    suffix = re.sub(r"_+", "_", suffix).strip("_")
    return suffix


def _secondary_label_suffix(text: str) -> list[str]:
    values = _extract_secondary_address_values(text)
    labels = {"bl": "Bl.", "sc": "Sc.", "et": "Et.", "ap": "Ap."}
    result: list[str] = []
    for key in ("bl", "sc", "et", "ap"):
        value = values.get(key)
        if value:
            result.append(f"{labels[key]} {value.upper()}")
    return result

def _normalize_street_for_key(prefix: str, street_name: str) -> str:
    parts: list[str] = []

    if prefix:
        parts.append(prefix)

    if street_name:
        cleaned = normalize_text(street_name)
        cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        if cleaned:
            parts.append(cleaned)

    return "_".join(parts).strip("_")


def _fallback_key_from_text(text: str) -> str:
    normalized = _remove_secondary_address_parts(text)
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "locatie"


def normalize_location_key(cont: Any) -> str:
    candidates = _extract_candidates(cont)

    best_candidate = ""
    best_score = -1

    for candidate in candidates:
        prefix, street_name, number = _extract_street_number_pair(candidate)
        locality = _extract_locality(candidate)

        score = 0
        if street_name:
            score += 20 + len(street_name)
        if prefix:
            score += 4
        if number:
            score += 10
        if locality:
            score += 3
        if "nr" in normalize_text(candidate):
            score += 2

        if score > best_score:
            best_candidate = candidate
            best_score = score

    if not best_candidate:
        raw_id = str(
            getattr(cont, "id_cont", None)
            or getattr(cont, "id_contract", None)
            or getattr(cont, "nume", None)
            or "locatie"
        )
        return _fallback_key_from_text(raw_id)

    prefix, street_name, number = _extract_street_number_pair(best_candidate)

    if street_name:
        street_key = _normalize_street_for_key(prefix, street_name)
        suffix = _secondary_key_suffix(best_candidate)
        key_parts = [street_key]
        if number:
            key_parts.append(normalize_text(number))
        if suffix:
            key_parts.append(suffix)
        key = "_".join(part for part in key_parts if part).strip("_")
        return key or _fallback_key_from_text(best_candidate)

    fallback_key = _fallback_key_from_text(best_candidate)
    suffix = _secondary_key_suffix(best_candidate)
    if suffix and not fallback_key.endswith(f"_{suffix}"):
        return f"{fallback_key}_{suffix}".strip("_")
    return fallback_key


def build_location_label(cont: Any) -> str:
    candidates = _extract_candidates(cont)
    if not candidates:
        return str(
            getattr(cont, "nume", None)
            or getattr(cont, "id_cont", None)
            or "Locație necunoscută"
        )

    best_candidate = ""
    best_score = -1

    for candidate in candidates:
        prefix, street_name, number = _extract_street_number_pair(candidate)
        locality = _extract_locality(candidate)

        score = 0
        if street_name:
            score += 20
        if prefix:
            score += 3
        if number:
            score += 8
        if locality:
            score += 4
        score += min(len(candidate), 120) // 12

        if score > best_score:
            best_candidate = candidate
            best_score = score

    prefix, street_name, number = _extract_street_number_pair(best_candidate)
    locality = _extract_locality(best_candidate)

    if street_name:
        pretty_prefix = {
            "str": "Strada",
            "bd": "Bulevard",
            "calea": "Calea",
            "aleea": "Aleea",
            "piata": "Piața",
            "sos": "Șoseaua",
            "intrarea": "Intrarea",
            "drum": "Drumul",
        }.get(prefix, "Strada")

        street_pretty = " ".join(part.capitalize() for part in street_name.split())
        parts = [f"{pretty_prefix} {street_pretty}"]
        if number:
            parts.append(f"Nr. {number.upper()}")
        secondary_parts = _secondary_label_suffix(best_candidate)
        if secondary_parts:
            parts.append(" ".join(secondary_parts))
        if locality:
            parts.append(locality.title())
        return ", ".join(parts)

    return best_candidate