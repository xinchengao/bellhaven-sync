"""Review app: approve or reject proposed CRM changes.

    streamlit run app.py

Nothing is written to the CRM until a reviewer clicks Approve.
"""
import json

import pandas as pd
import streamlit as st

from bellhaven import store
from bellhaven.apply import StaleProposal, apply_actions
from bellhaven.crm import CRM
from bellhaven.normalize import care_type_from_offerings

st.set_page_config(page_title="Bellhaven CRM Review", layout="wide")

KIND_LABEL = {
    "REPARENT": "Wrong parent",
    "CHOW": "Change of ownership",
    "RENAME": "Outdated name",
    "ADDRESS": "Address fix",
    "DUPLICATE": "Duplicate",
    "NEW_ACCOUNT": "New location",
    "NOT_ON_WEBSITE": "Not on website",
}
COMPARE_FIELDS = [("name", "name"), ("street", "billing_street"), ("city", "billing_city"),
                  ("state", "billing_state"), ("zip", "billing_zip"),
                  ("care_offerings", "care_type"), ("phone", "phone")]


@st.cache_resource
def get_crm():
    return CRM()


def con():
    return store.connect()


def load(status_filter, kinds):
    q = "SELECT * FROM proposals WHERE status IN ({}) ORDER BY kind, title".format(
        ",".join("?" * len(status_filter)))
    rows = [dict(r) for r in con().execute(q, status_filter).fetchall()]
    return [r for r in rows if r["kind"] in kinds]


def describe_action(op):
    if op["op"] == "create":
        f = op["fields"]
        return (f"**Create** account '{f['name']}' at {f['billing_street']}, {f['billing_city']} "
                f"{f['billing_state']} {f['billing_zip']} (care type {f['care_type']}, status Active)"
                f" under the Bellhaven parent")
    changes = ", ".join(
        f"`{k}`: '{op.get('expect', {}).get(k, '')}' -> '{v if v != '$new' else '<new account id>'}'"
        for k, v in op["set"].items())
    note = " + append note" if op.get("note") else ""
    return f"**Update** {op['account_id']}: {changes}{note}"


def comparison_table(website, crm):
    rows = []
    for w_key, c_key in COMPARE_FIELDS:
        w, c = (website or {}).get(w_key, "") or "", (crm or {}).get(c_key, "") or ""
        w_cmp = care_type_from_offerings(w) if c_key == "care_type" else w
        same = str(w_cmp).strip().lower() == str(c).strip().lower()
        rows.append({"field": c_key, "website": w, "CRM": c,
                     "": "" if same or not website or not crm else "differs"})
    if crm:
        for k in ("parent_name", "status", "lifetime_revenue", "outstanding_ar", "note"):
            rows.append({"field": k, "website": "", "CRM": crm.get(k), "": ""})
    return pd.DataFrame(rows).astype(str)  # mixed types break the table widget


def show_evidence(p):
    ev = json.loads(p["evidence"])
    website = ev.get("website") if isinstance(ev.get("website"), dict) else None
    st.caption(f"Rule: {ev.get('rule', '')}")

    if p["kind"] == "DUPLICATE" and "survivor" in ev:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Duplicate (will be marked Inactive)**")
            st.dataframe(comparison_table(website, ev["duplicate"]), hide_index=True)
            st.caption("Matched because: " + "; ".join(ev.get("duplicate_match_reasons", [])))
        with c2:
            st.markdown("**Surviving account**")
            st.dataframe(comparison_table(website, ev["survivor"]), hide_index=True)
            st.caption("Matched because: " + "; ".join(ev.get("survivor_match_reasons", [])))
        return

    crm = ev.get("crm")
    if not website:
        st.info(ev.get("website") or "")
    st.dataframe(comparison_table(website, crm), hide_index=True)
    if ev.get("match_reasons"):
        st.caption("Matched because: " + "; ".join(ev["match_reasons"]))
    if ev.get("current_owner_account"):
        st.markdown("**Account for the same address under the new owner**")
        st.dataframe(comparison_table(None, ev["current_owner_account"]), hide_index=True)
    if p["kind"] in ("CHOW", "REPARENT"):
        st.markdown(f"Billing check: lifetime_revenue = **{ev.get('lifetime_revenue')}**, "
                    f"outstanding_ar = **{ev.get('outstanding_ar')}**")
    if ev.get("near_misses"):
        st.markdown("**Closest CRM accounts that were NOT matched**")
        st.dataframe(pd.DataFrame([{**n["account"], "why_not": "; ".join(n["why_not"])}
                                   for n in ev["near_misses"]])[
            ["account_id", "name", "parent_name", "billing_street", "billing_city",
             "billing_state", "billing_zip", "why_not"]], hide_index=True)
    if website and website.get("url"):
        st.markdown(f"[Website page]({website['url']})")


def approve(p):
    actions = json.loads(p["actions"])
    prior = (json.loads(p["result"]) or {}).get("refs") if p["result"] else None
    c = con()
    try:
        log = apply_actions(get_crm(), actions, prior_refs=prior)
    except StaleProposal as e:
        store.record_decision(c, p["key"], "failed", {"error": str(e), **getattr(e, "log", {})})
        st.error(f"Not applied: {e}")
        return
    except Exception as e:  # API error: keep refs so a retry won't double-create
        store.record_decision(c, p["key"], "failed", {"error": str(e), **getattr(e, "log", {})})
        st.error(f"API error: {e}")
        return
    store.record_decision(c, p["key"], "approved", log)
    st.toast("Written to CRM: " + "; ".join(log["steps"]))


def reject(p, reason):
    store.record_decision(con(), p["key"], "rejected", {"reason": reason})
    st.toast("Rejected")


# ------------------------------------------------------------------- layout
st.title("Bellhaven CRM review queue")

counts = dict(con().execute("SELECT status, COUNT(*) FROM proposals GROUP BY status").fetchall())
m = st.columns(5)
for col, s in zip(m, ["pending", "approved", "rejected", "failed", "obsolete"]):
    col.metric(s.capitalize(), counts.get(s, 0))

last = con().execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
if last:
    st.caption(f"Last pipeline run {last['started_at']}: {last['locations']} website locations, "
               f"{last['accounts']} CRM accounts, {last['new_pending']} new proposals, "
               f"{last['skipped_decided']} already-decided proposals skipped.")

with st.sidebar:
    st.header("Filter")
    status_filter = st.multiselect("Status", ["pending", "failed", "approved", "rejected", "obsolete"],
                                   default=["pending", "failed"])
    all_kinds = list(KIND_LABEL)
    kinds = st.multiselect("Type", all_kinds, default=all_kinds, format_func=lambda k: KIND_LABEL[k])

proposals = load(status_filter or ["pending"], kinds)
if not proposals:
    st.success("Nothing to review with these filters.")

for p in proposals:
    label = f"[{KIND_LABEL.get(p['kind'], p['kind'])}] {p['title']}"
    with st.expander(label, expanded=False):
        st.markdown(f"Status: **{p['status']}** · Confidence: **{p['confidence']}** · "
                    f"First proposed {p['first_seen']}")
        show_evidence(p)

        st.markdown("**What approving will do**")
        for op in json.loads(p["actions"]):
            st.markdown("- " + describe_action(op))

        if p["result"]:
            st.markdown("**Result**")
            st.json(json.loads(p["result"]))

        if p["status"] in ("pending", "failed"):
            c1, c2, c3 = st.columns([1, 1, 3])
            reason = c3.text_input("Reject reason (optional)", key=f"r_{p['key']}",
                                   label_visibility="collapsed", placeholder="Reject reason (optional)")
            if c1.button("Approve" if p["status"] == "pending" else "Retry", key=f"a_{p['key']}",
                         type="primary"):
                approve(p)
                st.rerun()
            if c2.button("Reject", key=f"x_{p['key']}"):
                reject(p, reason)
                st.rerun()
