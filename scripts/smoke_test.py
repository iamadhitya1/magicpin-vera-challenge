#!/usr/bin/env python3
"""Local smoke test -- not part of the submission. Exercises:
  1. compose() directly against all 30 canonical test pairs (bypasses
     suppression-key dedup so every pair gets checked every run).
  2. The full HTTP contract via FastAPI's TestClient: context push,
     idempotency, tick, healthz, metadata.
  3. The five reply scenarios (auto-reply streak, hostile, intent
     transition, curveball, engaged accept).

Run: python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from bot.compose import compose  # noqa: E402
from bot.main import app  # noqa: E402
from bot.store import context_store, conversation_store  # noqa: E402

EXPANDED = ROOT / "expanded"


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_dataset():
    categories = {}
    for f in (EXPANDED / "categories").glob("*.json"):
        d = load_json(f)
        categories[d["slug"]] = d
    merchants = {}
    for f in (EXPANDED / "merchants").glob("*.json"):
        d = load_json(f)
        merchants[d["merchant_id"]] = d
    customers = {}
    for f in (EXPANDED / "customers").glob("*.json"):
        d = load_json(f)
        customers[d["customer_id"]] = d
    triggers = {}
    for f in (EXPANDED / "triggers").glob("*.json"):
        d = load_json(f)
        triggers[d["id"]] = d
    test_pairs = load_json(EXPANDED / "test_pairs.json")["pairs"]
    return categories, merchants, customers, triggers, test_pairs


VALID_CTAS = {"open_ended", "binary_yes_no", "binary_confirm_cancel", "multi_choice_slot", "none"}


def check_compose_on_canonical_pairs(categories, merchants, customers, triggers, test_pairs):
    print(f"\n=== 1. compose() over {len(test_pairs)} canonical test pairs ===")
    failures = []
    for pair in test_pairs:
        tid, mid, cid = pair["trigger_id"], pair["merchant_id"], pair.get("customer_id")
        trigger = triggers.get(tid)
        merchant = merchants.get(mid)
        if trigger is None or merchant is None:
            failures.append((pair["test_id"], "missing trigger/merchant in dataset"))
            continue
        category = categories.get(merchant["category_slug"])
        customer = customers.get(cid) if cid else None
        try:
            msg = compose(category, merchant, trigger, customer)
        except Exception as e:  # noqa: BLE001
            failures.append((pair["test_id"], f"exception: {e!r}"))
            continue
        if not msg.body or not msg.body.strip():
            failures.append((pair["test_id"], "empty body"))
        if msg.cta not in VALID_CTAS:
            failures.append((pair["test_id"], f"invalid cta {msg.cta!r}"))
        if msg.send_as not in {"vera", "merchant_on_behalf"}:
            failures.append((pair["test_id"], f"invalid send_as {msg.send_as!r}"))
        if cid and msg.send_as != "merchant_on_behalf":
            failures.append((pair["test_id"], "customer scoped but send_as != merchant_on_behalf"))
        print(f"  [{pair['test_id']}] kind={trigger['kind']:<28} send_as={msg.send_as:<18} cta={msg.cta:<20} | {msg.body[:110]}")

    print(f"\n  {len(test_pairs) - len(failures)}/{len(test_pairs)} passed")
    if failures:
        print("  FAILURES:")
        for tid, reason in failures:
            print(f"    - {tid}: {reason}")
    return not failures


def check_all_triggers_no_crash(categories, merchants, customers, triggers):
    print(f"\n=== 2. compose() over ALL {len(triggers)} triggers x their merchant (crash sweep) ===")
    failures = []
    for tid, trigger in triggers.items():
        merchant = merchants.get(trigger.get("merchant_id"))
        if not merchant:
            continue
        category = categories.get(merchant["category_slug"])
        if not category:
            continue
        customer = None
        if trigger.get("scope") == "customer" and trigger.get("customer_id"):
            customer = customers.get(trigger["customer_id"])
        try:
            compose(category, merchant, trigger, customer)
        except Exception as e:  # noqa: BLE001
            failures.append((tid, repr(e)))
    print(f"  {len(triggers) - len(failures)}/{len(triggers)} composed without error")
    if failures:
        for tid, err in failures[:20]:
            print(f"    - {tid}: {err}")
    return not failures


def check_http_contract(categories, merchants, customers, triggers):
    print("\n=== 3. HTTP contract (TestClient) ===")
    context_store.clear()
    conversation_store.clear()
    client = TestClient(app)

    r = client.get("/v1/healthz")
    assert r.status_code == 200, r.text
    assert r.json()["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    print("  [ok] healthz zero state")

    r = client.get("/v1/metadata")
    assert r.status_code == 200, r.text
    print("  [ok] metadata:", r.json()["team_name"], "|", r.json()["model"])

    v = 1
    for slug, cat in categories.items():
        r = client.post("/v1/context", json={"scope": "category", "context_id": slug, "version": v, "payload": cat, "delivered_at": "2026-04-26T09:00:00Z"})
        assert r.status_code == 200 and r.json()["accepted"], r.text
    for mid, m in merchants.items():
        r = client.post("/v1/context", json={"scope": "merchant", "context_id": mid, "version": v, "payload": m, "delivered_at": "2026-04-26T09:00:00Z"})
        assert r.status_code == 200 and r.json()["accepted"], r.text
    for cid, c in customers.items():
        r = client.post("/v1/context", json={"scope": "customer", "context_id": cid, "version": v, "payload": c, "delivered_at": "2026-04-26T09:00:00Z"})
        assert r.status_code == 200 and r.json()["accepted"], r.text
    for tid, t in triggers.items():
        r = client.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": v, "payload": t, "delivered_at": "2026-04-26T09:00:00Z"})
        assert r.status_code == 200 and r.json()["accepted"], r.text
    print(f"  [ok] pushed {len(categories)} categories, {len(merchants)} merchants, {len(customers)} customers, {len(triggers)} triggers")

    # idempotency: same version again -> 409
    slug0 = next(iter(categories))
    r = client.post("/v1/context", json={"scope": "category", "context_id": slug0, "version": 1, "payload": categories[slug0], "delivered_at": "x"})
    assert r.status_code == 409, r.text
    print("  [ok] stale version rejected 409")

    # version bump -> 200
    r = client.post("/v1/context", json={"scope": "category", "context_id": slug0, "version": 2, "payload": categories[slug0], "delivered_at": "x"})
    assert r.status_code == 200 and r.json()["accepted"], r.text
    print("  [ok] version bump accepted")

    r = client.get("/v1/healthz")
    counts = r.json()["contexts_loaded"]
    assert counts["category"] == len(categories)
    assert counts["merchant"] == len(merchants)
    assert counts["customer"] == len(customers)
    assert counts["trigger"] == len(triggers)
    print("  [ok] healthz counts match:", counts)

    all_trigger_ids = list(triggers.keys())
    total_actions = 0
    for i in range(0, len(all_trigger_ids), 20):
        batch = all_trigger_ids[i : i + 20]
        r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": batch})
        assert r.status_code == 200, r.text
        actions = r.json()["actions"]
        assert len(actions) <= 20
        for a in actions:
            for key in ("conversation_id", "merchant_id", "send_as", "trigger_id", "template_name", "template_params", "body", "cta", "suppression_key", "rationale"):
                assert key in a, f"missing {key} in action {a}"
            assert a["cta"] in VALID_CTAS
            assert a["body"].strip()
        total_actions += len(actions)
    print(f"  [ok] ticked all {len(all_trigger_ids)} triggers in batches of 20, {total_actions} actions returned")

    # re-tick the same triggers -> suppression should mean 0 new actions
    r = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": all_trigger_ids[:20]})
    assert r.json()["actions"] == [], "suppression_key dedup failed: resent an already-sent trigger"
    print("  [ok] suppression_key dedup prevents resend on repeat tick")

    return True


def check_reply_scenarios():
    print("\n=== 4. /v1/reply scenarios ===")
    context_store.clear()
    conversation_store.clear()
    client = TestClient(app)
    mid = "m_smoketest_merchant"

    # auto-reply streak -> should end by the 3rd canned reply
    ended = False
    for i in range(1, 5):
        r = client.post(
            "/v1/reply",
            json={
                "conversation_id": f"conv_auto_{i}",
                "merchant_id": mid,
                "customer_id": None,
                "from_role": "merchant",
                "message": "Thank you for contacting us! Our team will respond shortly.",
                "received_at": "2026-04-26T10:00:00Z",
                "turn_number": i + 1,
            },
        )
        data = r.json()
        print(f"  auto-reply turn {i}: action={data['action']}")
        if data["action"] == "end":
            ended = True
            break
    assert ended, "auto-reply streak never ended"
    print("  [ok] auto-reply streak ends conversation")

    r = client.post(
        "/v1/reply",
        json={"conversation_id": "conv_intent_1", "merchant_id": "m_intent", "customer_id": None, "from_role": "merchant", "message": "Ok lets do it. Whats next?", "received_at": "x", "turn_number": 2},
    )
    data = r.json()
    body_l = data.get("body", "").lower()
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    assert any(w in body_l for w in actioning) and not any(w in body_l for w in qualifying), data
    print("  [ok] intent transition -> action mode:", data["body"][:90])

    r = client.post(
        "/v1/reply",
        json={"conversation_id": "conv_hostile", "merchant_id": "m_hostile", "customer_id": None, "from_role": "merchant", "message": "Stop messaging me. This is useless spam.", "received_at": "x", "turn_number": 2},
    )
    data = r.json()
    assert data["action"] == "end" or (data["action"] == "send" and any(w in data.get("body", "").lower() for w in ["sorry", "apolog", "won't"])), data
    print("  [ok] hostile handled:", data["action"], data.get("body", "")[:90])

    r = client.post(
        "/v1/reply",
        json={"conversation_id": "conv_curveball", "merchant_id": "m_curve", "customer_id": None, "from_role": "merchant", "message": "Btw can you also help me with my GST filing this month?", "received_at": "x", "turn_number": 2},
    )
    data = r.json()
    assert data["action"] == "send" and data["body"], data
    print("  [ok] curveball handled:", data["body"][:110])

    r = client.post(
        "/v1/reply",
        json={"conversation_id": "conv_engaged", "merchant_id": "m_engaged", "customer_id": None, "from_role": "merchant", "message": "Yes please send the abstract. Also draft the patient WhatsApp.", "received_at": "x", "turn_number": 2},
    )
    data = r.json()
    assert data["action"] == "send" and "abstract" in data["body"].lower() and "draft" in data["body"].lower(), data
    print("  [ok] engaged accept echoes back specifics:", data["body"][:110])

    return True


def main() -> int:
    categories, merchants, customers, triggers, test_pairs = load_dataset()
    print(f"Loaded {len(categories)} categories, {len(merchants)} merchants, {len(customers)} customers, {len(triggers)} triggers, {len(test_pairs)} test pairs")

    ok = True
    ok &= check_compose_on_canonical_pairs(categories, merchants, customers, triggers, test_pairs)
    ok &= check_all_triggers_no_crash(categories, merchants, customers, triggers)
    ok &= check_http_contract(categories, merchants, customers, triggers)
    ok &= check_reply_scenarios()

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
