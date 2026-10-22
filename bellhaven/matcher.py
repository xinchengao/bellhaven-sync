"""Match website locations to CRM accounts and turn the findings into proposals.

A proposal is a list of API operations plus the evidence a reviewer needs.
Nothing here writes to the CRM.

Operations:
  {"op": "update", "account_id": X, "set": {...}, "expect": {...}, "note": "..."}
  {"op": "create", "ref": "$new", "fields": {...}, "note": "..."}
"expect" holds the values we saw when proposing; apply refuses if the CRM has
since changed. "$new" lets a later op point at an account created earlier in
the same proposal (the CHOW case).
"""
from rapidfuzz import fuzz

from .normalize import (care_type_from_offerings, is_po_box, norm_name, norm_phone,
                        norm_street, norm_zip, street_number)

BELLHAVEN_PARENT = "Bellhaven Senior Living"
TAG = "bellhaven-sync"


# ---------------------------------------------------------------- helpers

def is_parent_account(a):
    return "(parent account)" in a["name"].lower()


def is_resolved(a):
    """Already marked as a duplicate or superseded by CHOW: not a live record."""
    return bool(a.get("duplicate_of_account") or a.get("chow_current_account"))


def has_billing_history(a):
    """SOP: revenue history AND outstanding AR > 0 means billing needs the old account."""
    return (a.get("lifetime_revenue") or 0) > 0 and (a.get("outstanding_ar") or 0) > 0


def parent_label(a):
    return (a.get("parent_name") or "(no parent)").replace(" (Parent Account)", "")


def crm_view(a):
    keys = ["account_id", "name", "parent_name", "billing_street", "billing_city",
            "billing_state", "billing_zip", "care_type", "phone", "status",
            "lifetime_revenue", "outstanding_ar", "note"]
    return {k: a.get(k) for k in keys}


def same_city(loc, a):
    return (loc["city"].lower() == (a.get("billing_city") or "").lower()
            and loc["state"] == a.get("billing_state"))


def address_match(loc, a):
    """Same street number, near-identical street name, and same zip or city/state."""
    if is_po_box(a.get("billing_street")):
        return False
    if street_number(loc["street"]) != street_number(a.get("billing_street")):
        return False
    if fuzz.ratio(norm_street(loc["street"]), norm_street(a.get("billing_street"))) < 90:
        return False
    return norm_zip(loc["zip"]) == norm_zip(a.get("billing_zip")) or same_city(loc, a)


def name_score(loc, a):
    return fuzz.token_sort_ratio(norm_name(loc["name"]), norm_name(a["name"]))


def street_conflict(loc, a):
    """Both have real street numbers and they differ: a different building."""
    n1, n2 = street_number(loc["street"]), street_number(a.get("billing_street"))
    return bool(n1 and n2 and n1 != n2)


def evaluate(loc, a):
    """Return (is_match, reasons, near_miss)."""
    reasons = []
    addr = address_match(loc, a)
    ns = name_score(loc, a)
    phone = norm_phone(loc.get("phone")) and norm_phone(loc.get("phone")) == norm_phone(a.get("phone"))
    if addr:
        reasons.append("street address matches")
    if ns >= 90 and same_city(loc, a):
        reasons.append(f"name matches ({ns:.0f}/100) in same city")
    if phone:
        reasons.append("phone matches")
    if norm_zip(loc["zip"]) == norm_zip(a.get("billing_zip")):
        reasons.append("zip matches")
    elif addr:
        reasons.append(f"zip differs (website {loc['zip']} vs CRM {a.get('billing_zip')})")

    if addr:
        return True, reasons, False
    if ns >= 90 and same_city(loc, a) and not street_conflict(loc, a):
        return True, reasons, False
    near = same_city(loc, a) or ns >= 75
    return False, reasons, near


def note_for(text):
    return f"{TAG}: {text}"


def changed_only(account, fields):
    return {k: v for k, v in fields.items() if (account.get(k) or "") != (v or "")}


def update_op(account, fields, note=None):
    fields = changed_only(account, fields)
    if not fields:
        return None
    op = {"op": "update", "account_id": account["account_id"], "set": fields,
          "expect": {k: account.get(k) or "" for k in fields}}
    if note:
        op["note"] = note_for(note)
    return op


def new_account_fields(loc, parent_id, care_type=None):
    return {
        "name": loc["name"],
        "parent_id": parent_id,
        "billing_street": loc["street"],
        "billing_city": loc["city"],
        "billing_state": loc["state"],
        "billing_zip": loc["zip"],
        "care_type": care_type or care_type_from_offerings(loc.get("care_offerings")),
        "phone": loc.get("phone", ""),
        "status": "Active",
    }


# ---------------------------------------------------------------- main logic

def build_proposals(locations, accounts, contact_counts=None):
    """Return (proposals, report_rows). contact_counts: account_id -> int (optional)."""
    contact_counts = contact_counts or {}
    parents = {a["name"].replace(" (Parent Account)", ""): a for a in accounts if is_parent_account(a)}
    bh = parents[BELLHAVEN_PARENT]
    bh_id = bh["account_id"]

    live = [a for a in accounts if not is_parent_account(a) and not is_resolved(a)]
    proposals, report, matched_ids = [], [], set()

    for loc in locations:
        matches, near_misses = [], []
        for a in live:
            ok, reasons, near = evaluate(loc, a)
            if ok:
                matches.append((a, reasons))
            elif near:
                near_misses.append({"account": crm_view(a), "why_not": reasons or ["no address or name match"]})

        website = {k: loc.get(k) for k in ("name", "street", "city", "state", "zip",
                                            "care_offerings", "phone", "url")}

        # ---- No CRM account: create one under Bellhaven
        if not matches:
            fields = new_account_fields(loc, bh_id)
            proposals.append({
                "kind": "NEW_ACCOUNT",
                "title": f"Create account for {loc['name']} ({loc['city']}, {loc['state']})",
                "location": loc["slug"],
                "confidence": "high" if not near_misses else "medium",
                "actions": [{"op": "create", "ref": "$new", "fields": fields,
                             "note": note_for("created from Bellhaven website listing")}],
                "evidence": {"website": website, "near_misses": near_misses,
                             "rule": "No CRM account matches this location's address, "
                                     "or its name in the same city."},
            })
            report.append({"location": loc["name"], "classification": "new (no CRM account)",
                           "account_id": "", "crm_name": "", "detail": ""})
            continue

        # ---- Pick the surviving account when several records describe this facility
        def rank(item):
            a, reasons = item
            return (
                a.get("parent_id") == bh_id,
                (a.get("lifetime_revenue") or 0) > 0 or (a.get("outstanding_ar") or 0) > 0,
                contact_counts.get(a["account_id"], 0),
                "phone matches" in reasons,
                norm_street(a.get("billing_street")) == norm_street(loc["street"]),
            )
        # Highest rank wins; ties go to the lowest account_id so reruns are stable.
        matches.sort(key=lambda it: it[0]["account_id"])
        matches.sort(key=rank, reverse=True)
        survivor, s_reasons = matches[0]
        for a, _ in matches:
            matched_ids.add(a["account_id"])

        classes = []

        # ---- Duplicates
        for dup, d_reasons in matches[1:]:
            op = update_op(dup, {"duplicate_of_account": survivor["account_id"], "status": "Inactive"},
                           note=f"duplicate of {survivor['account_id']} ({survivor['name']}); "
                                f"same facility as website listing '{loc['name']}'")
            if op:
                proposals.append({
                    "kind": "DUPLICATE",
                    "title": f"Mark '{dup['name']}' ({dup['account_id']}, {parent_label(dup)}) as "
                             f"duplicate of '{survivor['name']}' ({survivor['account_id']}, "
                             f"{parent_label(survivor)})",
                    "account_id": dup["account_id"], "location": loc["slug"],
                    "confidence": "high",
                    "actions": [op],
                    "evidence": {"website": website, "duplicate": crm_view(dup),
                                 "duplicate_match_reasons": d_reasons,
                                 "survivor": crm_view(survivor), "survivor_match_reasons": s_reasons,
                                 "rule": "Survivor chosen by: under Bellhaven parent > has billing "
                                         "history > more contacts > phone matches website > exact "
                                         "street > lowest account id."},
                })
            classes.append("duplicate")

        # ---- Wrong parent: SOP decides between CHOW and a direct re-parent
        if survivor.get("parent_id") != bh_id:
            base_ev = {"website": website, "crm": crm_view(survivor), "match_reasons": s_reasons,
                       "lifetime_revenue": survivor.get("lifetime_revenue"),
                       "outstanding_ar": survivor.get("outstanding_ar")}
            if has_billing_history(survivor):
                fields = new_account_fields(loc, bh_id, care_type=survivor.get("care_type"))
                proposals.append({
                    "kind": "CHOW",
                    "title": f"CHOW: new Bellhaven account for {loc['name']}; keep "
                             f"'{survivor['name']}' [{parent_label(survivor)}] for billing",
                    "account_id": survivor["account_id"], "location": loc["slug"],
                    "confidence": "high",
                    "actions": [
                        {"op": "create", "ref": "$new", "fields": fields,
                         "note": note_for(f"change of ownership; previous account "
                                          f"{survivor['account_id']} kept for billing")},
                        {"op": "update", "account_id": survivor["account_id"],
                         "set": {"chow_current_account": "$new"},
                         "expect": {"chow_current_account": survivor.get("chow_current_account") or "",
                                    "parent_id": survivor.get("parent_id") or ""}},
                    ],
                    "evidence": {**base_ev, "rule": "Revenue > 0 AND AR > 0: old account left "
                                 "exactly as is, new account created under Bellhaven, old "
                                 "account's chow_current_account points to it."},
                })
                classes.append("needs fix: CHOW")
                report.append({"location": loc["name"], "classification": ", ".join(classes),
                               "account_id": survivor["account_id"], "crm_name": survivor["name"],
                               "detail": f"parent {parent_label(survivor)}"})
                continue  # the new account carries the website's name/address
            op = update_op(survivor, {"parent_id": bh_id},
                           note=f"re-parented from {parent_label(survivor)} to {BELLHAVEN_PARENT}")
            proposals.append({
                "kind": "REPARENT",
                "title": f"Move '{survivor['name']}' from {parent_label(survivor)} to Bellhaven",
                "account_id": survivor["account_id"], "location": loc["slug"],
                "confidence": "high", "actions": [op],
                "evidence": {**base_ev, "rule": "No revenue history or no outstanding AR, so the "
                             "existing account is re-parented directly."},
            })
            classes.append(f"needs fix: parent was {parent_label(survivor)}")

        # ---- Name: CRM should carry the operator's current name
        if " ".join(survivor["name"].split()).lower() != " ".join(loc["name"].split()).lower():
            op = update_op(survivor, {"name": loc["name"]},
                           note=f"renamed from '{survivor['name']}' to match Bellhaven website")
            proposals.append({
                "kind": "RENAME",
                "title": f"Rename '{survivor['name']}' -> '{loc['name']}'",
                "account_id": survivor["account_id"], "location": loc["slug"],
                "confidence": "high" if any("address" in r for r in s_reasons) else "medium",
                "actions": [op],
                "evidence": {"website": website, "crm": crm_view(survivor), "match_reasons": s_reasons,
                             "rule": "Matched on address; the website name is the current name."},
            })
            classes.append("needs fix: name")

        # ---- Address: only when it differs after normalization (not formatting)
        addr_fix = {}
        if is_po_box(survivor.get("billing_street")) or (
                norm_street(survivor.get("billing_street")) != norm_street(loc["street"])):
            addr_fix["billing_street"] = loc["street"]
        if norm_zip(survivor.get("billing_zip")) != norm_zip(loc["zip"]):
            addr_fix["billing_zip"] = loc["zip"]
        if (survivor.get("billing_city") or "").lower() != loc["city"].lower():
            addr_fix["billing_city"] = loc["city"]
        if survivor.get("billing_state") != loc["state"]:
            addr_fix["billing_state"] = loc["state"]
        if addr_fix:
            op = update_op(survivor, addr_fix, note="address updated to match Bellhaven website")
            proposals.append({
                "kind": "ADDRESS",
                "title": f"Fix address on '{survivor['name']}': " + ", ".join(
                    f"{k.replace('billing_', '')} '{survivor.get(k)}' -> '{v}'" for k, v in addr_fix.items()),
                "account_id": survivor["account_id"], "location": loc["slug"],
                "confidence": "medium", "actions": [op],
                "evidence": {"website": website, "crm": crm_view(survivor), "match_reasons": s_reasons,
                             "rule": "Street differs after normalizing abbreviations, or is a PO Box, "
                                     "or zip/city/state differ."},
            })
            classes.append("needs fix: address")

        report.append({"location": loc["name"],
                       "classification": ", ".join(classes) or "confident match",
                       "account_id": survivor["account_id"], "crm_name": survivor["name"],
                       "detail": "; ".join(s_reasons)})

    # ---- Accounts under Bellhaven that the website no longer lists
    for a in live:
        if a.get("parent_id") != bh_id or a["account_id"] in matched_ids:
            continue
        others = [o for o in live if o["account_id"] != a["account_id"]
                  and o.get("parent_id") not in (bh_id, None, "")
                  and not is_po_box(a.get("billing_street"))
                  and street_number(o.get("billing_street")) == street_number(a.get("billing_street"))
                  and fuzz.ratio(norm_street(o.get("billing_street")), norm_street(a.get("billing_street"))) >= 90
                  and norm_zip(o.get("billing_zip")) == norm_zip(a.get("billing_zip"))]
        ev = {"crm": crm_view(a), "website": "not listed on any Bellhaven website page"}
        if others:
            owner = others[0]
            ev.update({"current_owner_account": crm_view(owner),
                       "lifetime_revenue": a.get("lifetime_revenue"),
                       "outstanding_ar": a.get("outstanding_ar")})
            if has_billing_history(a):
                proposals.append({
                    "kind": "CHOW",
                    "title": f"CHOW: '{a['name']}' now operated by {parent_label(owner)}; point to "
                             f"existing account '{owner['name']}'",
                    "account_id": a["account_id"], "location": None, "confidence": "medium",
                    "actions": [{"op": "update", "account_id": a["account_id"],
                                 "set": {"chow_current_account": owner["account_id"]},
                                 "expect": {"chow_current_account": a.get("chow_current_account") or "",
                                            "parent_id": a.get("parent_id") or ""}}],
                    "evidence": {**ev, "rule": "Not on Bellhaven's website and another operator's "
                                 "account exists at the same address. Revenue > 0 AND AR > 0, so the "
                                 "old account is kept as is and chow_current_account points to the "
                                 "existing account under the new owner (no new account needed)."},
                })
                report.append({"location": "(not on website)", "classification": "left Bellhaven: CHOW",
                               "account_id": a["account_id"], "crm_name": a["name"],
                               "detail": f"now {parent_label(owner)} ({owner['account_id']})"})
            else:
                op = update_op(a, {"duplicate_of_account": owner["account_id"], "status": "Inactive"},
                               note=f"facility now operated by {parent_label(owner)}; "
                                    f"see {owner['account_id']}")
                proposals.append({
                    "kind": "DUPLICATE",
                    "title": f"'{a['name']}' left Bellhaven; duplicate of '{owner['name']}' "
                             f"[{parent_label(owner)}]",
                    "account_id": a["account_id"], "location": None, "confidence": "medium",
                    "actions": [op],
                    "evidence": {**ev, "rule": "No billing history, and the new owner already has "
                                 "an account for this address, so this one is the duplicate."},
                })
                report.append({"location": "(not on website)", "classification": "left Bellhaven: duplicate",
                               "account_id": a["account_id"], "crm_name": a["name"],
                               "detail": f"now {parent_label(owner)}"})
            continue

        op = update_op(a, {"status": "Needs Review"},
                       note="not listed on Bellhaven website; may be closed or sold. "
                            "Confirm owner before changing parent")
        if op:
            proposals.append({
                "kind": "NOT_ON_WEBSITE",
                "title": f"Flag '{a['name']}' ({a.get('billing_city')}, {a.get('billing_state')}) "
                         f"for review: not on Bellhaven website",
                "account_id": a["account_id"], "location": None, "confidence": "medium",
                "actions": [op],
                "evidence": {**ev, "rule": "Under Bellhaven in CRM but not on the website, and no "
                             "other operator's account at this address. New owner unknown, so flag "
                             "rather than re-parent or deactivate."},
            })
        report.append({"location": "(not on website)", "classification": "not on website",
                       "account_id": a["account_id"], "crm_name": a["name"],
                       "detail": f"{a.get('billing_city')}, {a.get('billing_state')}"})

    proposals = [p for p in proposals if all(op is not None for op in p["actions"])]
    return proposals, report
