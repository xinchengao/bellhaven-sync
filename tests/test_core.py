"""Offline tests: matching rules, CHOW SOP, idempotent store, apply engine.

    python -m pytest tests/
"""
import os
import tempfile

import pytest

from bellhaven import store
from bellhaven.apply import StaleProposal, apply_actions
from bellhaven.matcher import build_proposals

BH, HV, MS = "P_BH", "P_HV", "P_MS"


def parent(pid, name):
    return {"account_id": pid, "name": f"{name} (Parent Account)", "parent_id": ""}


def acct(aid, name, parent_id, street, city="Town", state="OH", zip_="44000", rev=0, ar=0,
         phone="", parent_name=""):
    return {"account_id": aid, "name": name, "parent_id": parent_id, "parent_name": parent_name,
            "billing_street": street, "billing_city": city, "billing_state": state,
            "billing_zip": zip_, "care_type": "Assisted Living", "status": "Active",
            "phone": phone, "lifetime_revenue": rev, "outstanding_ar": ar,
            "chow_current_account": "", "duplicate_of_account": "", "note": ""}


def loc(name, street, city="Town", state="OH", zip_="44000", phone=""):
    return {"slug": name.lower().replace(" ", "-"), "name": name, "street": street, "city": city,
            "state": state, "zip": zip_, "care_offerings": "Assisted Living", "phone": phone,
            "url": ""}


PARENTS = [parent(BH, "Bellhaven Senior Living"), parent(HV, "Harborview Care Group"),
           parent(MS, "Millstone Health Partners")]


def kinds(proposals):
    return sorted(p["kind"] for p in proposals)


def test_abbreviated_address_is_confident_match():
    p, _ = build_proposals([loc("Bellhaven of A", "12 Logan Blvd")],
                           PARENTS + [acct("a1", "Bellhaven of A", BH, "12 Logan Boulevard")])
    assert p == []


def test_wrong_parent_without_ar_is_reparented():
    p, _ = build_proposals([loc("Bellhaven of A", "12 Main St")],
                           PARENTS + [acct("a1", "Bellhaven of A", HV, "12 Main Street", rev=500)])
    assert kinds(p) == ["REPARENT"]
    assert p[0]["actions"][0]["set"] == {"parent_id": BH}


def test_wrong_parent_with_revenue_and_ar_uses_chow():
    p, _ = build_proposals([loc("Bellhaven of A", "12 Main St")],
                           PARENTS + [acct("a1", "Bellhaven of A", HV, "12 Main St", rev=500, ar=10)])
    assert kinds(p) == ["CHOW"]
    create, update = p[0]["actions"]
    assert create["op"] == "create" and create["fields"]["parent_id"] == BH
    assert update["set"] == {"chow_current_account": "$new"}  # old account otherwise untouched


def test_duplicates_keep_bellhaven_record():
    p, _ = build_proposals([loc("Bellhaven of A", "12 Main St")],
                           PARENTS + [acct("a1", "Bellhaven of A", BH, "12 Main St"),
                                      acct("a2", "Harbor A", HV, "12 Main Street")])
    assert kinds(p) == ["DUPLICATE"]
    assert p[0]["actions"][0]["set"] == {"duplicate_of_account": "a1", "status": "Inactive"}


def test_same_name_other_city_is_not_a_match():
    p, _ = build_proposals([loc("Bellhaven of Carlisle", "1 Walnut Rd", city="Carlisle", state="PA")],
                           PARENTS + [acct("a1", "Bellhaven of New Carlisle", BH, "9 Elm St",
                                           city="New Carlisle")])
    assert "NEW_ACCOUNT" in kinds(p)


def test_orphan_sold_to_other_operator():
    p, _ = build_proposals([], PARENTS + [
        acct("a1", "Bellhaven of S", BH, "5 Columbus Ave", rev=100, ar=5),
        acct("m1", "Millstone of S", MS, "5 Columbus Ave", parent_name="Millstone Health Partners (Parent Account)")])
    assert kinds(p) == ["CHOW"]
    assert p[0]["actions"][0]["set"] == {"chow_current_account": "m1"}


def test_store_skips_decided_proposals():
    with tempfile.TemporaryDirectory() as d:
        con = store.connect(os.path.join(d, "t.db"))
        prop = {"kind": "RENAME", "title": "t", "account_id": "a1", "location": "x",
                "confidence": "high", "evidence": {},
                "actions": [{"op": "update", "account_id": "a1", "set": {"name": "B"}}]}
        assert store.upsert_proposal(con, prop) == "new"
        key = store.proposal_key("RENAME", "a1", "x", prop["actions"])
        store.record_decision(con, key, "rejected")
        assert store.upsert_proposal(con, prop) == "decided"


class FakeCRM:
    def __init__(self, accounts):
        self.accounts = {a["account_id"]: dict(a) for a in accounts}
        self.n = 0

    def get_account(self, aid):
        return dict(self.accounts[aid])

    def update_account(self, aid, fields):
        self.accounts[aid].update(fields)

    def create_account(self, fields):
        self.n += 1
        aid = f"new{self.n}"
        self.accounts[aid] = {"account_id": aid, **fields}
        return {"account_id": aid}


def test_apply_chow_links_new_account():
    old = acct("a1", "Bellhaven of A", HV, "12 Main St", rev=500, ar=10)
    p, _ = build_proposals([loc("Bellhaven of A", "12 Main St")], PARENTS + [old])
    crm = FakeCRM([old])
    log = apply_actions(crm, p[0]["actions"])
    new_id = log["refs"]["$new"]
    assert crm.accounts["a1"]["chow_current_account"] == new_id
    assert crm.accounts["a1"]["parent_id"] == HV          # old parent preserved
    assert crm.accounts[new_id]["parent_id"] == BH


def test_apply_refuses_when_crm_changed():
    a = acct("a1", "Bellhaven of A", HV, "12 Main St")
    p, _ = build_proposals([loc("Bellhaven of A", "12 Main St")], PARENTS + [a])
    crm = FakeCRM([{**a, "parent_id": "someone-else"}])
    with pytest.raises(StaleProposal):
        apply_actions(crm, p[0]["actions"])
