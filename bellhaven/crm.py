"""Thin client for the CRM sandbox API."""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get(
    "BH_API_BASE", "https://analyst-assessment-production.up.railway.app/api/v1"
)

# Fields the API lets us PATCH (taken from its own validation message).
MUTABLE_FIELDS = {
    "name", "parent_id", "status", "note", "care_type", "phone",
    "billing_street", "billing_city", "billing_state", "billing_zip",
    "chow_current_account", "duplicate_of_account",
}


class CRM:
    def __init__(self, token=None):
        token = token or os.environ["BH_TOKEN"]
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _check(self, r):
        if r.status_code >= 400:
            raise RuntimeError(f"{r.request.method} {r.url} -> {r.status_code}: {r.text}")
        return r.json()

    def list_accounts(self, page_size=100):
        out, page = [], 1
        while True:
            body = self._check(self.s.get(f"{BASE}/accounts", timeout=30,
                                          params={"page": page, "page_size": page_size}))
            out.extend(body["data"])
            if len(out) >= body["total"] or not body["data"]:
                return out
            page += 1

    def get_account(self, account_id):
        return self._check(self.s.get(f"{BASE}/accounts/{account_id}", timeout=30))

    def update_account(self, account_id, fields):
        bad = set(fields) - MUTABLE_FIELDS
        if bad:
            raise ValueError(f"Not mutable via API: {bad}")
        return self._check(self.s.patch(f"{BASE}/accounts/{account_id}", json=fields, timeout=30))

    def create_account(self, fields):
        return self._check(self.s.post(f"{BASE}/accounts", json=fields, timeout=30))

    def list_contacts(self, page_size=100):
        out, page = [], 1
        while True:
            body = self._check(self.s.get(f"{BASE}/contacts", timeout=30,
                                          params={"page": page, "page_size": page_size}))
            out.extend(body["data"])
            if len(out) >= body["total"] or not body["data"]:
                return out
            page += 1
