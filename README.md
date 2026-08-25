# Vera Challenge Bot

A submission to magicpin's [VERA AI Challenge](https://partners.magicpin.com/vera/ai-challenge/). Implements the 5-endpoint HTTP contract (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) and a `compose(category, merchant, trigger, customer?)` message engine.

## Approach

**Deterministic, rule-based composer — no LLM call in the request path.** `compose()` dispatches on `trigger.kind` to one of ~26 hand-written builder functions (`bot/triggers.py`), each of which assembles the message body entirely from fields actually present in the pushed `category` / `merchant` / `trigger` / `customer` context objects. Nothing is templated with a placeholder that could read as filler, and nothing is invented: every number, date, offer title, or quote in an output message traces back to a real field in the pushed contexts.

Why no LLM, given the brief explicitly allows one:
- **Determinism is a hard requirement** ("must be deterministic given the same inputs"). A rule-based composer is deterministic by construction; an LLM needs `temperature=0` and even then isn't guaranteed bit-identical across runs/providers.
- **Latency/rate budget.** 30s timeout, 10 req/s from the judge, up to 20 actions/tick. Pure Python template assembly is sub-millisecond per call — there's no risk of a slow provider costing operational penalty points.
- **Zero-cost, zero-dependency.** No API key to manage, no quota to run out of mid-test, nothing that can 401/500 independently of my own code.
- **It's directly calibratable.** The challenge package ships 10 fully-scored case studies (`examples/case-studies.md`). I wrote the builders to hit the same shape those cases were scored 47-50/50 on (source citation, merchant-specific numbers, one CTA, compulsion levers), rather than hoping a prompt reliably reproduces that shape.

The tradeoff is genuine: an LLM-backed composer would generalize better to genuinely novel phrasing needs than a fixed set of templates can. I mitigate that with a **fallback builder** (`fallback_builder` in `bot/triggers.py`) used for any `trigger.kind` without a dedicated builder — including kinds the judge injects after submission that I've never seen. It never fabricates: it pulls whatever real signal is available (CTR vs. peer, a performance delta, a customer-aggregate number) and asks a low-friction question rather than guessing at kind-specific detail it doesn't have.

## Architecture

```
bot/
  store.py        in-memory context store (idempotent by scope+version) + conversation state
  specificity.py   fact-extraction helpers — every function reads a real field, invents nothing
  voice.py         category voice / language-mix helpers, taboo-word guard
  triggers.py      one builder per trigger.kind + the fallback builder
  compose.py       compose() — dispatches, builds rationale, assembles template_params
  conversation.py  /v1/reply state machine: auto-reply, hostile/opt-out, intent-transition,
                   curveball, plain-affirmative — in that priority order
  main.py          FastAPI app wiring the 5 endpoints
```

**Reply handling** (`conversation.py`) is also pattern-based, not an LLM call, for the same determinism/latency reasons. Auto-reply detection is keyed per **merchant account** (not per conversation) with an escalating streak: 1st canned reply → one human-routing nudge, 2nd → back off 4h, 3rd+ → end. That mirrors how a real WhatsApp Business auto-reply actually behaves (it's a property of the account, not of one thread). Hostile/opt-out messages end the conversation and mark the merchant suppressed for the rest of the run. An explicit commitment ("let's do it", "go ahead") switches straight to action mode — no re-qualification, per the intent-transition anti-pattern called out in the brief.

## What I'd add with more time

- A learned/LLM composer as a **second opinion** on top of the deterministic one, with the rule-based output as the fallback if the LLM path fails or disagrees with taboo/fabrication guards.
- Persisting state to Redis/SQLite instead of in-memory, so the bot survives a process restart mid-test (not required per the brief, but would remove a single point of failure on a free host).
- A real slot-availability/offer-catalog lookup instead of using whatever offer happens to be marked `active` first.

## Running locally

```bash
pip install -r requirements.txt
uvicorn bot.main:app --host 0.0.0.0 --port 8080
```

`scripts/smoke_test.py` (not part of the submission surface) exercises `compose()` against all 30 canonical test pairs and all 100 dataset triggers, plus the full HTTP contract (idempotency, suppression dedup, tick batching) and all five `/v1/reply` scenarios (auto-reply streak, hostile, intent-transition, curveball, engaged accept):

```bash
python dataset/generate_dataset.py --seed-dir dataset --out expanded
python scripts/smoke_test.py
```

To run the official `judge_simulator.py` against a live instance, set `BOT_URL` and an LLM provider key at the top of that file, then `python judge_simulator.py`.

## Deployment

Deployed on Render's free tier (`render.yaml` included) — chosen over serverless (e.g. Vercel functions) specifically because this bot is **stateful**: context pushed via `/v1/context` has to persist in memory across `/v1/tick` and `/v1/reply` calls for the whole test window, which a stateless/serverless function model doesn't guarantee.
