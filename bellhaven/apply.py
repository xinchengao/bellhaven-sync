"""Execute an approved proposal's operations against the CRM.

Called only from the review app after a human clicks Approve.
"""
from datetime import date


class StaleProposal(Exception):
    pass


def _resolve(value, refs):
    return refs.get(value, value) if isinstance(value, str) else value


def _with_note(existing, note):
    stamped = f"[{date.today().isoformat()}] {note}"
    return f"{existing}\n{stamped}".strip() if existing else stamped


def apply_actions(crm, actions, prior_refs=None):
    """Run operations in order and return a log of what happened.

    prior_refs: ids created by an earlier, partially failed attempt, so a retry
    never creates the same account twice. On failure the exception carries
    `.log` with any refs created so far.
    """
    log = {"refs": dict(prior_refs or {}), "steps": []}
    try:
        for op in actions:
            _run_op(crm, op, log)
    except Exception as exc:
        exc.log = log
        raise
    return log


def _run_op(crm, op, log):
    refs = log["refs"]
    if op["op"] == "create":
        if op["ref"] in refs:
            log["steps"].append(f"reused already-created {refs[op['ref']]}")
            return
        fields = {k: _resolve(v, refs) for k, v in op["fields"].items()}
        if op.get("note"):
            fields["note"] = _with_note("", op["note"])
        resp = crm.create_account(fields)
        new_id = resp.get("account_id") or resp.get("id") or (resp.get("account") or {}).get("account_id")
        if not new_id:
            raise RuntimeError(f"Create returned no account id: {resp}")
        refs[op["ref"]] = new_id
        log["steps"].append(f"created {new_id} '{fields.get('name')}'")

    elif op["op"] == "update":
        current = crm.get_account(op["account_id"])
        for field, expected in (op.get("expect") or {}).items():
            if (current.get(field) or "") != (expected or ""):
                raise StaleProposal(
                    f"{op['account_id']}.{field} is now '{current.get(field)}', "
                    f"expected '{expected}'. Re-run the pipeline.")
        fields = {k: _resolve(v, refs) for k, v in op["set"].items()}
        if op.get("note"):
            fields["note"] = _with_note(current.get("note"), op["note"])
        crm.update_account(op["account_id"], fields)
        log["steps"].append(f"updated {op['account_id']}: {sorted(fields)}")

    else:
        raise ValueError(f"Unknown op {op['op']}")
