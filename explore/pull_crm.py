"""Pull every CRM account into data/accounts.json and print a quick profile."""
import json
import os
from collections import Counter

import requests
from dotenv import load_dotenv

load_dotenv()
BASE = "https://analyst-assessment-production.up.railway.app/api/v1"
HEADERS = {"Authorization": f"Bearer {os.environ['BH_TOKEN']}"}


def extract_items(payload):
    """The list endpoint's shape isn't documented, so accept the common ones."""
    if isinstance(payload, list):
        return payload
    for key in ("items", "data", "results", "accounts"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise ValueError(f"Unknown response shape, keys: {list(payload)}")


def fetch_all_accounts(page_size=50):
    accounts, page = [], 1
    while True:
        r = requests.get(f"{BASE}/accounts", headers=HEADERS,
                         params={"page": page, "page_size": page_size}, timeout=30)
        r.raise_for_status()
        batch = extract_items(r.json())
        accounts.extend(batch)
        if len(batch) < page_size:
            return accounts
        page += 1


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)

    me = requests.get(f"{BASE}/me", headers=HEADERS, timeout=30)
    print("/me ->", me.status_code, me.text[:300], "\n")

    accounts = fetch_all_accounts()
    with open("data/accounts.json", "w") as f:
        json.dump(accounts, f, indent=2)
    print(f"Saved {len(accounts)} accounts to data/accounts.json\n")

    print("Fields on one account:")
    print(json.dumps(accounts[0], indent=2), "\n")

    # Which accounts are parents, and how many children does each have?
    by_id = {a["account_id"]: a for a in accounts}
    parent_counts = Counter(a.get("parent_id") for a in accounts if a.get("parent_id"))
    print("Parents (by number of child accounts):")
    for pid, n in parent_counts.most_common():
        name = by_id.get(pid, {}).get("name", "?? parent id not found in CRM")
        print(f"  {n:3d}  {pid}  {name}")

    # Accounts that would trigger the CHOW rule if they need a new parent
    chow = [a for a in accounts
            if (a.get("lifetime_revenue") or 0) > 0 and (a.get("outstanding_ar") or 0) > 0]
    print(f"\nAccounts with revenue > 0 AND AR > 0: {len(chow)}")
    print("Status values:", Counter(a.get("status") for a in accounts))
