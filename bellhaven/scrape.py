"""Scrape every Bellhaven community from the public website.

The directory says 34 communities while the homepage says 35 and links a newly
acquired one separately, so detail links are collected from the directory pages
AND the homepage/About page. Each detail page is read because directory cards
don't always show every care offering.
"""
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SITE = os.environ.get("BH_SITE", "https://analyst-assessment-production.up.railway.app")
DETAIL_RE = re.compile(r"^/communities/[a-z0-9-]+/?$")


def _soup(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def _detail_links(page):
    links = set()
    for a in page.find_all("a", href=True):
        path = a["href"].replace(SITE, "")
        if DETAIL_RE.match(path):
            links.add(urljoin(SITE, path.rstrip("/")))
    return links


def collect_links():
    links, url, seen = set(), f"{SITE}/communities", set()
    while url and url not in seen:  # follow "Next" until there isn't one
        seen.add(url)
        page = _soup(url)
        links |= _detail_links(page)
        nxt = page.find("a", string=re.compile(r"Next", re.I))
        url = urljoin(SITE, nxt["href"]) if nxt else None
    for extra in ("/", "/about"):
        links |= _detail_links(_soup(SITE + extra))
    return sorted(links)


def _after_label(page, label):
    node = page.find(string=re.compile(rf"^\s*{label}\s*$"))
    if not node:
        return None
    return node.parent.find_next_sibling() or node.parent.find_next()


def parse_detail(url):
    page = _soup(url)
    name = page.find("h1").get_text(strip=True)

    street = city = state = zip_code = ""
    addr = _after_label(page, "Address")
    if addr:
        lines = [l.strip() for l in addr.get_text("\n").split("\n") if l.strip()]
        street = lines[0] if lines else ""
        m = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5})", lines[-1] if lines else "")
        if m:
            city, state, zip_code = m.groups()

    care = _after_label(page, "Care Offerings")
    offerings = []
    if care:
        offerings = [t.get_text(strip=True) for t in care.find_all(["span", "li"])]
        offerings = [o for o in offerings if o] or [care.get_text(strip=True)]

    def text_of(label):
        el = _after_label(page, label)
        return el.get_text(" ", strip=True) if el else ""

    return {
        "slug": url.rsplit("/", 1)[-1],
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": "; ".join(offerings),
        "administrator": text_of("Administrator"),
        "phone": text_of("Phone"),
        "url": url,
    }


def scrape_locations():
    locations = [parse_detail(u) for u in collect_links()]
    bad = [l["slug"] for l in locations if not (l["street"] and l["zip"] and l["state"])]
    if bad:
        raise RuntimeError(f"Could not parse address for {bad}; site layout may have changed")
    return locations
