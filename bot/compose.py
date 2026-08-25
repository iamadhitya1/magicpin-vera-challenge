"""compose(category, merchant, trigger, customer=None) -> ComposedMessage

This is the function named in the challenge brief. The HTTP layer
(bot/main.py) calls it once per candidate trigger at each /v1/tick, and
once more (via bot/conversation.py) when building a /v1/reply follow-on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import triggers as trig
from . import voice

LEVER_LABELS = {
    "specificity": "a verifiable number/date from the merchant or category context",
    "loss_aversion": "loss-aversion framing",
    "social_proof": "social proof",
    "effort_externalization": "an externalized-effort offer (I'll draft it)",
    "curiosity": "a curiosity hook",
    "reciprocity": "reciprocity (offering something before asking)",
    "asking_the_merchant": "asking the merchant a low-stakes question",
    "relationship_continuity": "continuity with the customer's history",
    "no_shame": "no-shame re-engagement framing",
    "respect_for_senior": "senior-appropriate register",
}


@dataclass
class ComposedMessage:
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str
    template_params: list[str]


def _template_params(built: trig.BuiltMessage, name_hint: str) -> list[str]:
    sentences = [s.strip() for s in built.body.replace("\n", " ").split(". ") if s.strip()]
    hook = sentences[0] if sentences else built.body
    rest = ". ".join(sentences[1:]) if len(sentences) > 1 else ""
    return [p for p in [name_hint, hook, rest] if p]


def _rationale(trigger: dict[str, Any], built: trig.BuiltMessage, merchant: dict[str, Any]) -> str:
    kind = trigger.get("kind", "unknown")
    lever_text = ", ".join(LEVER_LABELS.get(l, l) for l in built.levers) or "a direct, specific ask"
    scope = trigger.get("scope", "merchant")
    who = "the merchant" if scope == "merchant" else "the merchant's customer, on the merchant's behalf"
    urgency = trigger.get("urgency")
    urgency_line = f" Urgency {urgency}/5." if urgency else ""
    return (
        f"Trigger kind '{kind}' (scope={scope}) prompted this message to {who}. "
        f"Uses {lever_text}.{urgency_line} CTA is {built.cta.replace('_', ' ')} to keep the next step low-friction."
    )


def compose(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> ComposedMessage:
    now = now or datetime.now(timezone.utc)
    kind = trigger.get("kind", "")
    built = trig.build_for_kind(kind, category, merchant, trigger, customer, now)

    # Defensive taboo check: if a builder ever produces a taboo word, fall
    # back to the generic-but-safe builder rather than ship a category-fit
    # violation.
    if voice.contains_taboo(built.body, category):
        built = trig.fallback_builder(category, merchant, trigger, customer, now)

    name_hint = (
        voice.customer_first_name(customer) if customer is not None else voice.salutation(category, merchant)
    )
    suppression_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}:{trigger.get('id')}"
    template_name = f"vera_{kind}_v1" if trigger.get("scope") == "merchant" else f"merchant_{kind}_v1"

    return ComposedMessage(
        body=built.body,
        cta=built.cta,
        send_as=built.send_as,
        suppression_key=suppression_key,
        rationale=_rationale(trigger, built, merchant),
        template_name=template_name,
        template_params=_template_params(built, name_hint),
    )
