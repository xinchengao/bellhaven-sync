"""Scrape every Bellhaven community from the public website into data/locations.csv.

The directory lists 34 communities but the homepage says 35, and links a newly
acquired one separately. So we collect detail links from the directory pages AND
from the homepage/About page, then read each detail page (the directory cards
don't always show every care offering).
"""
import csv
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SITE = "https://analyst-assessment-production.up.railway.app"
DETAIL_RE = re.compile(r"^/communities/[a-z0-9-]+/?$")


def soup(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def detail_links(page):
    links = set()
    for a in page.find_all("a", href=True):
        path = a["href"].replace(SITE, "")
        if DETAIL_RE.match(path):
            links.add(urljoin(SITE, path.rstrip("/")))
    return links


def collect_links():
    links, url = set(), f"{SITE}/communities"
    while url:  # follow "Next" until there isn't one
        page = soup(url)
        links |= detail_links(page)
        nxt = page.find("a", string=re.compile(r"Next", re.I))
        url = urljoin(SITE, nxt["href"]) if nxt else None
    for extra in ("/", "/about"):  # catch communities not in the directory
        links |= detail_links(soup(SITE + extra))
    return sorted(links)


def label_value(page, label):
    """Find the text block that follows a label like 'Address' or 'Phone'."""
    node = page.find(string=re.compile(rf"^\s*{label}\s*$"))
    if not node:
        return None
    nxt = node.parent.find_next_sibling()
    return nxt if nxt else node.parent.find_next()


def parse_detail(url):
    page = soup(url)
    name = page.find("h1").get_text(strip=True)

    street = city = state = zip_code = ""
    addr = label_value(page, "Address")
    if addr:
        lines = [l.strip() for l in addr.get_text("\n").split("\n") if l.strip()]
        street = lines[0] if lines else ""
        m = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5})", lines[-1] if lines else "")
        if m:
            city, state, zip_code = m.groups()

    care = label_value(page, "Care Offerings")
    offerings = []
    if care:
        items = [t.get_text(strip=True) for t in care.find_all(["span", "li"])]
        offerings = [i for i in items if i] or [care.get_text(strip=True)]

    def text_of(label):
        el = label_value(page, label)
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


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    links = collect_links()
    print(f"Found {len(links)} community pages")

    rows = [parse_detail(u) for u in links]
    with open("data/locations.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print("Saved data/locations.csv")
    missing = [r["slug"] for r in rows if not (r["street"] and r["zip"])]
    if missing:
        print("WARNING: address not parsed for:", missing)
    for r in rows[:3]:
        print(r)
