# bellhaven-sync

Keeps the CRM's picture of which facilities Bellhaven Senior Living owns in line
with Bellhaven's own website. A daily job scrapes the website, matches each
community to CRM accounts, and queues proposed fixes. A reviewer approves or
rejects each one in a small web app; only approved changes are written to the
CRM through its API.

```
website ──scrape──┐
                  ├─ match ─> proposals (SQLite) ─> review app ─approve─> CRM API
CRM API ──pull────┘
```

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "BH_TOKEN=<your token>" > .env

python run_pipeline.py      # scrape + match + queue proposals (never writes to CRM)
streamlit run app.py        # review queue at http://localhost:8501
python -m pytest tests/     # offline tests for matching, SOP and idempotency
```

## Layout

| File | Purpose |
|---|---|
| `bellhaven/scrape.py` | Pulls all communities: directory pages (paginated) **plus** links on the home and About pages, then each detail page for address and care offerings |
| `bellhaven/normalize.py` | Street/name/zip/phone normalization (`Boulevard`→`blvd`, `Pk`→`pike`, `Centre`→`center` …) |
| `bellhaven/matcher.py` | Matching and classification; turns findings into proposals with evidence |
| `bellhaven/store.py` | SQLite proposal store with deterministic keys (idempotency) |
| `bellhaven/apply.py` | Executes an approved proposal; checks the CRM hasn't changed first |
| `app.py` | Streamlit review app |
| `run_pipeline.py` | Daily entry point |
| `.github/workflows/daily-sync.yml`, `deploy/crontab.txt` | Schedule |

## Matching

For each website location, every live CRM account (not a parent, not already
marked duplicate/CHOW) is checked under **any** parent, since misparented
accounts are part of the problem.

* **Match on address**: same street number, street name ≥ 90 similar after
  normalization, and same zip or same city/state (catches zip typos).
* **Match on name**: name ≥ 90 similar in the same city, as long as the street
  numbers don't conflict. This catches a record whose street is a PO Box.
* Same name in a different city is **not** a match ("Bellhaven of Carlisle, PA"
  vs "Bellhaven of New Carlisle, OH"; "Amberly Manor" Hudson OH vs Colorado
  Springs). Same city with a different street number is **not** a match
  ("Bellhaven at Union Square", 118 Union Square Dr vs "Union Square Senior
  Living", 240 Market St). These near misses are shown as evidence on the
  new-account proposal so the reviewer can double check.
* Phone is shown as supporting evidence only; most CRM phones are stale.

Outcomes per location:

| Case | Proposal |
|---|---|
| Matches one account, all fields right | none (confident match) |
| Account under Harborview / Cedar Trail / no parent | `REPARENT` or `CHOW` (see SOP) |
| Name differs from the website | `RENAME` (website is the operator's current name) |
| Street is a PO Box, or zip/city/state differ | `ADDRESS` (formatting-only differences are ignored) |
| Several accounts for one facility | `DUPLICATE` on the losers |
| No account | `NEW_ACCOUNT` under Bellhaven |
| Under Bellhaven but not on the website | see below |

**Choosing the surviving record among duplicates** (in order): already under
the Bellhaven parent; has revenue or AR; more contacts; phone matches the
website; exact street match; lowest account id (so reruns pick the same one).
Losers get `duplicate_of_account = survivor`, status `Inactive`, and a note.

**CHOW SOP.** Before moving an account to a different parent: if
`lifetime_revenue > 0` **and** `outstanding_ar > 0`, the old account is left
exactly as is (no rename, no note), a new account is created under Bellhaven
from the website data, and the old account's `chow_current_account` is set to
the new id. Otherwise the existing account is re-parented directly. This
applies to Harborview and Cedar Trail accounts and to the one with no parent.

**Accounts under Bellhaven that the website no longer lists.** If another
operator already has an account at the same address, the facility was sold:
with revenue and AR the old account gets `chow_current_account` pointing at the
new owner's existing account (no second account is created); without it, the
old account is a duplicate of the new owner's. If there is no evidence of a
new owner, the account is set to `Needs Review` with a note rather than made
Inactive, because we don't know whether it closed or was sold, and an Inactive
account drops out of the sales team's view.

## Safety and reruns

* The pipeline never writes to the CRM. Only the app's Approve button does.
* Each proposal's key is a hash of its type, account, location and the exact
  changes. On rerun, a key that was already approved or rejected is skipped, so
  decided items are never re-proposed. Applied changes also simply stop showing
  up, because the CRM now matches the website.
* Notes are date-stamped at apply time, not in the proposal, so the key does
  not change from day to day.
* Before writing, the app re-reads each account and refuses if the fields it is
  about to change no longer hold the values seen when proposing (stale guard).
* If a CHOW fails after creating the new account, the created id is saved and a
  retry reuses it instead of creating a second account.
* Pending proposals that a later run no longer produces are marked obsolete.

## Not handled / next steps

* Care type: CRM holds a single `care_type`; website communities can list
  several offerings. New accounts take the first offering. Existing values are
  not changed because the two vocabularies already map 1:1 for the primary
  offering.
* Phones are not synced; they disagree almost everywhere and look unreliable
  on both sides.
* Contacts on duplicate accounts are not moved to the survivor.
