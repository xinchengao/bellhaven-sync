"""Daily job: scrape website, pull CRM, match, queue proposals for review.

Never writes to the CRM. Safe to re-run: proposals that were already approved
or rejected are recognised by their key and not queued again.

    python run_pipeline.py
"""
import csv
import json
import os
from collections import Counter

from bellhaven import store
from bellhaven.crm import CRM
from bellhaven.matcher import build_proposals
from bellhaven.scrape import scrape_locations


def save_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    os.makedirs("data", exist_ok=True)
    crm = CRM()

    locations = scrape_locations()
    accounts = crm.list_accounts()
    contacts = Counter(c["account_id"] for c in crm.list_contacts())

    # Snapshots for debugging / audit
    save_csv("data/locations.csv", locations)
    with open("data/accounts.json", "w") as f:
        json.dump(accounts, f, indent=2)

    proposals, report = build_proposals(locations, accounts, contacts)
    save_csv("data/match_report.csv", report)

    con = store.connect()
    outcome, seen = Counter(), set()
    for p in proposals:
        outcome[store.upsert_proposal(con, p)] += 1
        seen.add(store.proposal_key(p["kind"], p.get("account_id"), p.get("location"), p["actions"]))
    obsolete = store.expire_stale(con, seen)

    summary = {"by_kind": Counter(p["kind"] for p in proposals),
               "classification": Counter(r["classification"] for r in report)}
    con.execute("INSERT INTO runs (started_at, locations, accounts, new_pending, skipped_decided, summary)"
                " VALUES (?,?,?,?,?,?)",
                (store.now(), len(locations), len(accounts), outcome["new"], outcome["decided"],
                 json.dumps(summary)))
    con.commit()

    print(f"Website locations: {len(locations)}   CRM accounts: {len(accounts)}")
    print(f"Proposals this run: {len(proposals)}  "
          f"(new: {outcome['new']}, still pending: {outcome['pending']}, "
          f"already decided/skipped: {outcome['decided']}, now obsolete: {obsolete})")
    for kind, n in sorted(summary["by_kind"].items()):
        print(f"  {kind:15s} {n}")


if __name__ == "__main__":
    main()
