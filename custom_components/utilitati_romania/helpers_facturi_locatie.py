from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .naming import normalize_text


_LOCALITY_WORDS = {
    "sibiu",
    "selimbar",
    "șelimbăr",
    "bucuresti",
    "bucurești",
    "cluj",
    "timisoara",
    "timișoara",
    "brasov",
    "brașov",
    "iasi",
    "iași",
    "constanta",
    "constanța",
    "oradea",
    "ploiesti",
    "ploiești",
    "rosu",
    "roșu",
}

_COUNTY_CODES = {
    "sb",
    "cj",
    "bv",
    "bh",
    "b",
    "if",
    "tm",
    "is",
    "ct",
    "ph",
    "ms",
    "ag",
    "ab",
    "mm",
    "sv",
    "nt",
    "bc",
    "vn",
    "bt",
    "db",
    "gl",
    "br",
    "dj",
    "gj",
    "vl",
    "hd",
    "cs",
    "tr",
    "ot",
    "il",
    "cl",
    "tl",
    "cv",
    "hr",
    "sj",
    "sm",
    "bn",
    "bz",
    "gr",
}

_STREET_PREFIXES = {
    "strada",
    "str",
    "str.",
    "aleea",
    "alee",
    "ale",
    "ale.",
    "bulevard",
    "bulevardul",
    "bd",
    "bd.",
    "calea",
    "cal",
    "cal.",
    "soseaua",
    "șoseaua",
    "sos",
    "sos.",
    "piata",
    "piața",
    "p-ta",
    "pta",
    "intrarea",
    "intr",
    "intr.",
    "drumul",
    "drum",
    "dr",
    "dr.",
    "splaiul",
    "spl",
    "spl.",
    "prelungirea",
    "prel",
    "prel.",
}

_STOP_MARKERS = {
    "nr",
    "nr.",
    "numar",
    "numărul",
    "bl",
    "bl.",
    "bloc",
    "sc",
    "sc.",
    "scara",
    "et",
    "et.",
    "etaj",
    "ap",
    "ap.",
    "apt",
    "apartament",
    "jud",
    "jud.",
    "judet",
    "judetul",
    "localitate",
    "loc",
    "consum",
    "adresa",
    "adresă",
    "contract",
    "client",
    "cod",
    "pod",
}

_MARKER_LABELS = {
    "bl": "Bl.",
    "bloc": "Bl.",
    "sc": "Sc.",
    "scara": "Sc.",
    "et": "Et.",
    "etaj": "Et.",
    "ap": "Ap.",
    "apt": "Ap.",
    "apartament": "Ap.",
}

_ADDRESS_MARKER_PATTERN = r"(?:nr|numar|numarul|numărul|bl|bloc|sc|scara|et|etaj|ap|apt|apartament)"
_STREET_PREFIX_PATTERN = (
    r"(?:strada|str\.?|aleea|alee|ale\.?|bulevardul|bulevard|bd\.?|calea|cal\.?|"
    r"soseaua|șoseaua|sos\.?|piata|piața|p-?ta|intrarea|intr\.?|drumul|dr\.?|"
    r"splaiul|spl\.?|prelungirea|prel\.?)"
)


@dataclass(slots=True)
class _AddressComponents:
    street: str
    number: str = ""
    block: str = ""
    stair: str = ""
    floor: str = ""
    apartment: str = ""

    @property
    def score(self) -> int:
        value = 20 + len(self.street)
        if self.number:
            value += 12
        if self.block:
            value += 4
        if self.stair:
            value += 4
        if self.floor:
            value += 3
        if self.apartment:
            value += 10
        return value


def _append_candidate(candidates: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in candidates:
        candidates.append(text)


def extract_location_candidates(cont_or_value: Any) -> list[str]:
    if cont_or_value is None:
        return []

    if isinstance(cont_or_value, str):
        text = cont_or_value.strip()
        return [text] if text else []

    candidates: list[str] = []

    _append_candidate(candidates, getattr(cont_or_value, "adresa", None))
    _append_candidate(candidates, getattr(cont_or_value, "nume", None))

    raw = getattr(cont_or_value, "date_brute", None)
    if isinstance(raw, dict):
        for key in (
            "address",
            "service_address",
            "serviceAddress",
            "site_address",
            "siteAddress",
            "usageAddress",
            "consumptionAddress",
            "full_address",
            "addressLine",
            "premise_label",
            "premiseLabel",
            "loc_consum",
            "adresa_loc_consum",
            "consumption_place",
            "consumptionPlaceName",
            "usage_place",
            "specificIdForUtilityType",
            "adresa",
            "alias",
            "label",
            "name",
            "account_label",
        ):
            _append_candidate(candidates, raw.get(key))

    return candidates


def _parts(value: str) -> list[str]:
    text = normalize_text(value)
    text = re.sub(r"\s*/\s*", ",", text)
    text = re.sub(r"\s*;\s*", ",", text)
    return [part.strip() for part in text.split(",") if part.strip()]


def _clean_token(value: str) -> str:
    return normalize_text(value).lower().strip(" .,:;-/")


def _clean_street_name(value: str) -> str:
    text = normalize_text(value).lower()
    text = re.sub(rf"\b{_STREET_PREFIX_PATTERN}\b\.?", " ", text, flags=re.IGNORECASE)
    text = re.sub(rf"\b{_ADDRESS_MARKER_PATTERN}\b\.?\s*[:\-]?\s*[a-z0-9\-/]+.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bjud(?:et(?:ul)?)?\b.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{4,6}\b", "", text)
    text = re.sub(r"[^a-z0-9ăâîșţșț\- ]+", " ", text)
    tokens: list[str] = []
    for token in text.split():
        token_clean = _clean_token(token)
        if not token_clean:
            continue
        if token_clean in _STREET_PREFIXES or token_clean in _STOP_MARKERS:
            break
        if token_clean in _LOCALITY_WORDS or token_clean in _COUNTY_CODES:
            continue
        if re.fullmatch(r"\d+[a-z]?", token_clean):
            break
        tokens.append(token_clean)
    return " ".join(tokens).strip()


def _extract_secondary_parts(text: str) -> dict[str, str]:
    normalized = normalize_text(text).lower()
    found: dict[str, str] = {}
    marker_map = {
        "bl": "block",
        "bloc": "block",
        "sc": "stair",
        "scara": "stair",
        "et": "floor",
        "etaj": "floor",
        "ap": "apartment",
        "apt": "apartment",
        "apartament": "apartment",
    }
    pattern = re.compile(
        r"\b(bl|bloc|sc|scara|et|etaj|ap|apt|apartament)\.?\s*[:\-]?\s*([a-z0-9\-/]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(normalized):
        key = marker_map.get(_clean_token(match.group(1)))
        value = _clean_token(match.group(2))
        if key and value and key not in found:
            found[key] = value
    return found



def _components_from_technical_brasov_text(text: str) -> _AddressComponents | None:
    """Extrage adresele Apă Brașov venite ca etichete tehnice din portal.

    Unele contracte sunt returnate cu texte lungi de forma:
    ``corespondenta P020/... nedeterminat Apa rece contorizata ... Nu Strada ...``.
    Aceste texte trebuie reduse la strada și număr înainte să ajungă în
    gruparea dashboardului.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None

    normalized_low = normalized.lower()
    if "corespondenta" not in normalized_low and "coresponden" not in normalized_low and "apa rece contorizata" not in normalized_low:
        return None

    street_match = re.search(
        rf"\b{_STREET_PREFIX_PATTERN}\b\.?\s+(.+?)(?:\s*,?\s*(?:loc|jud)\b|$)",
        normalized,
        re.IGNORECASE,
    )
    if not street_match:
        return None

    segment = street_match.group(1).strip(" ,.;:-")
    segment = re.sub(r"\bbrasov\b.*$", "", segment, flags=re.IGNORECASE).strip(" ,.;:-")
    segment = re.sub(r"\((?:exc|excel).*?$", "", segment, flags=re.IGNORECASE).strip(" ,.;:-")

    number = ""
    number_match = re.search(
        r"(?:^|[,\s]+)(?:nr\.?|numar(?:ul)?|numărul)\s*[:\-]?\s*([0-9]+\s*[a-z]?)\b",
        segment,
        re.IGNORECASE,
    )
    if number_match:
        number = _clean_token(number_match.group(1).replace(" ", ""))
        street_segment = segment[: number_match.start()].strip(" ,.;:-")
    else:
        inline_number = re.search(r"^(.*?)[\s,]+([0-9]+\s*[a-z]?)$", segment, re.IGNORECASE)
        if inline_number:
            street_segment = inline_number.group(1).strip(" ,.;:-")
            number = _clean_token(inline_number.group(2).replace(" ", ""))
        else:
            street_segment = segment

    street_segment = re.sub(
        r"\b(?:corespondenta|coresponden[tț]ă|nedeterminat|apa\s+rece\s+contorizata|brasov\s+populatie|populatie|nu)\b",
        " ",
        street_segment,
        flags=re.IGNORECASE,
    )
    street_segment = re.sub(r"\bP\d{3}/\d+\b", " ", street_segment, flags=re.IGNORECASE)
    street_segment = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", " ", street_segment)
    street_segment = re.sub(r"\b\d{4,}\b", " ", street_segment)
    street_segment = re.sub(r"\s+", " ", street_segment).strip(" ,.;:-")

    street = _clean_street_name(street_segment)
    if not street:
        return None

    return _AddressComponents(street=street, number=number)

def _components_from_labeled(text: str) -> _AddressComponents | None:
    normalized = normalize_text(text)
    secondary = _extract_secondary_parts(normalized)

    pattern = re.compile(
        rf"\b{_STREET_PREFIX_PATTERN}\b\.?\s+"
        r"([^,;/]+?)"
        r"(?:\s+(?:nr\.?|numar(?:ul)?|numărul)\s*[:\-]?\s*([0-9]+[a-z]?))?"
        rf"(?=$|[,;/]|\s+\b(?:{_ADDRESS_MARKER_PATTERN})\b)",
        re.IGNORECASE,
    )
    match = pattern.search(normalized)
    if not match:
        return None

    street_segment = match.group(1)
    number = _clean_token(match.group(2) or "")

    inline_number = re.search(r"^(.*?)[\s,]+([0-9]+[a-z]?)$", street_segment.strip(), re.IGNORECASE)
    if inline_number and not number:
        street_segment = inline_number.group(1)
        number = _clean_token(inline_number.group(2))

    street = _clean_street_name(street_segment)
    if not street:
        return None

    return _AddressComponents(
        street=street,
        number=number,
        block=secondary.get("block", ""),
        stair=secondary.get("stair", ""),
        floor=secondary.get("floor", ""),
        apartment=secondary.get("apartment", ""),
    )


def _components_from_parts(text: str) -> _AddressComponents | None:
    parts = _parts(text)
    if not parts:
        return None

    secondary = _extract_secondary_parts(text)

    if len(parts) >= 2 and re.fullmatch(r"\d+[A-Za-z]?", parts[0].strip()):
        street = _clean_street_name(parts[1])
        if street:
            return _AddressComponents(
                street=street,
                number=_clean_token(parts[0]),
                block=secondary.get("block", ""),
                stair=secondary.get("stair", ""),
                floor=secondary.get("floor", ""),
                apartment=secondary.get("apartment", ""),
            )

    for part in parts:
        low = _clean_token(part)
        if low in _LOCALITY_WORDS or low in _COUNTY_CODES or re.fullmatch(r"\d{4,6}", low):
            continue

        labeled = _components_from_labeled(part)
        if labeled:
            if not labeled.block:
                labeled.block = secondary.get("block", "")
            if not labeled.stair:
                labeled.stair = secondary.get("stair", "")
            if not labeled.floor:
                labeled.floor = secondary.get("floor", "")
            if not labeled.apartment:
                labeled.apartment = secondary.get("apartment", "")
            return labeled

        match = re.match(
            r"([A-Za-zĂÂÎȘȚăâîșț][A-Za-zĂÂÎȘȚăâîșț \-]+?)\s+(\d+[A-Za-z]?)$",
            normalize_text(part),
            re.IGNORECASE,
        )
        if match:
            street = _clean_street_name(match.group(1))
            if street:
                return _AddressComponents(
                    street=street,
                    number=_clean_token(match.group(2)),
                    block=secondary.get("block", ""),
                    stair=secondary.get("stair", ""),
                    floor=secondary.get("floor", ""),
                    apartment=secondary.get("apartment", ""),
                )

    return None


def _extract_components(text: str) -> _AddressComponents | None:
    return (
        _components_from_technical_brasov_text(text)
        or _components_from_labeled(text)
        or _components_from_parts(text)
    )


def _best_components(candidates: list[str]) -> _AddressComponents | None:
    best: _AddressComponents | None = None
    for candidate in candidates:
        components = _extract_components(candidate)
        if components and (best is None or components.score > best.score):
            best = components
    return best


def _slugify(text: str) -> str:
    value = normalize_text(text).lower()
    value = "".join(ch if ch.isalnum() else "_" for ch in value)
    while "__" in value:
        value = value.replace("__", "_")
    return value.strip("_")


def _components_key(components: _AddressComponents) -> str:
    parts = [components.street]
    if components.number:
        parts.append(components.number)
    if components.block:
        parts.extend(["bl", components.block])
    if components.stair:
        parts.extend(["sc", components.stair])
    if components.floor:
        parts.extend(["et", components.floor])
    if components.apartment:
        parts.extend(["ap", components.apartment])
    return _slugify(" ".join(parts)) or "locatie"


def _title_words(text: str) -> str:
    return " ".join(word.capitalize() for word in text.split())


def _components_label(components: _AddressComponents) -> str:
    parts = [_title_words(components.street)]
    if components.number:
        parts.append(components.number.upper())
    if components.block:
        parts.append(f"Bl. {components.block.upper()}")
    if components.stair:
        parts.append(f"Sc. {components.stair.upper()}")
    if components.floor:
        parts.append(f"Et. {components.floor.upper()}")
    if components.apartment:
        parts.append(f"Ap. {components.apartment.upper()}")
    return " ".join(parts).strip() or "Locație"


def normalize_facturi_location_key(cont_or_value: Any) -> str:
    candidates = extract_location_candidates(cont_or_value)
    components = _best_components(candidates)
    if components:
        return _components_key(components)

    if hasattr(cont_or_value, "id_cont"):
        fallback = str(
            getattr(cont_or_value, "nume", None)
            or getattr(cont_or_value, "id_cont", None)
            or "locatie"
        )
        return _slugify(fallback) or "locatie"

    text = str(cont_or_value or "").strip()
    return _slugify(text) or "locatie"


def build_facturi_location_label(cont_or_value: Any) -> str:
    candidates = extract_location_candidates(cont_or_value)
    components = _best_components(candidates)
    if components:
        return _components_label(components)

    if hasattr(cont_or_value, "nume"):
        fallback = str(
            getattr(cont_or_value, "nume", None)
            or getattr(cont_or_value, "id_cont", None)
            or "Locație"
        ).strip()
        return fallback or "Locație"

    text = str(cont_or_value or "").strip()
    return text or "Locație"
