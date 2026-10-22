"""Normalization helpers so '1125 Logan Boulevard' == '1125 Logan Blvd'."""
import re

STREET_ABBR = {
    "street": "st", "road": "rd", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy",
    "pk": "pike", "pke": "pike", "highway": "hwy", "circle": "cir", "terrace": "ter",
    "square": "sq", "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "saint": "st", "suite": "", "ste": "", "unit": "",
}

NAME_ABBR = {
    "rehab": "rehabilitation", "centre": "center", "healthcare": "health care",
    "and": "&",
}

# Website care offerings -> CRM care_type vocabulary
CARE_TYPE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
}


def _tokens(text):
    text = (text or "").lower().replace("&", " & ")
    return [t for t in re.split(r"[^a-z0-9&]+", text) if t]


def norm_street(street):
    toks = [STREET_ABBR.get(t, t) for t in _tokens(street)]
    return " ".join(t for t in toks if t)


def street_number(street):
    m = re.match(r"\s*(\d+)", street or "")
    return m.group(1) if m else None


def is_po_box(street):
    return bool(re.match(r"\s*p\.?\s*o\.?\s*box", street or "", re.I))


def norm_name(name):
    toks = []
    for t in _tokens(name):
        toks.extend(NAME_ABBR.get(t, t).split())
    return " ".join(toks)


def norm_zip(z):
    return (z or "").strip()[:5]


def norm_phone(p):
    return re.sub(r"\D", "", p or "")[-10:]


def care_type_from_offerings(offerings):
    """CRM holds one care_type; take the first website offering."""
    first = (offerings or "").split(";")[0].strip().lower()
    return CARE_TYPE_MAP.get(first, offerings.split(";")[0].strip() if offerings else "")
