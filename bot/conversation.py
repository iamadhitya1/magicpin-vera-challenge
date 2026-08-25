"""Handles POST /v1/reply — the merchant/customer's response to a message
we sent, and what we do next (send / wait / end).

Detection order matters: auto-reply first (it pre-empts everything else,
since a canned WhatsApp Business reply isn't really "the merchant" talking),
then hostile/opt-out, then an explicit commitment (intent transition),
then a soft decline, then a plain affirmative, then anything else is
treated as an on-topic question or curveball and gets a redirect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .store import ConversationState, ConversationStore

AUTO_REPLY_MARKERS = [
    "thank you for contacting",
    "will respond shortly",
    "our team will respond",
    "automated assistant",
    "will get back to you",
    "thank you for your message",
    "thanks for reaching out, we'll",
]

OPTOUT_MARKERS = [
    "stop messaging",
    "stop sending",
    "stop contacting",
    "unsubscribe",
    "leave me alone",
    "don't message",
    "do not message",
    "not interested",
]

RUDE_MARKERS = ["useless", "spam", "bothering", "harass", "annoying"]

COMMITMENT_MARKERS = [
    "let's do it",
    "lets do it",
    "let's proceed",
    "lets proceed",
    "go ahead",
    "sounds good",
    "yes let's",
    "yes lets",
    "ok let's",
    "ok lets",
    "okay let's",
    "sure, do it",
    "sure do it",
    "confirmed",
    "let's start",
    "lets start",
    "please proceed",
]

QUALIFYING_TELLS = ["would you", "do you", "can you tell", "what if", "how about"]

MAYBE_LATER_MARKERS = ["not now", "maybe later", "not right now", "remind me later", "busy right now"]

AFFIRMATIVE_SHORT = {"yes", "yes please", "sure", "ok", "okay", "1", "2", "confirm", "yep", "yeah"}
ENGAGED_MARKERS = ["please send", "please share", "please draft", "go for it", "send it", "share it", "yes please"]
ENGAGED_ECHO_WORDS = ["abstract", "draft", "list", "post", "whatsapp", "checklist", "banner", "story"]
CURVEBALL_MARKERS = ["gst", "tax filing", "income tax", "loan", "insurance claim", "legal advice", "visa"]


def _matches_any(message: str, markers: list[str]) -> bool:
    lowered = message.lower()
    return any(m in lowered for m in markers)


@dataclass
class ReplyDecision:
    action: str  # send | wait | end
    body: Optional[str] = None
    cta: Optional[str] = None
    wait_seconds: Optional[int] = None
    rationale: str = ""


def _get_or_create_state(
    store: ConversationStore, conversation_id: str, merchant_id: Optional[str], customer_id: Optional[str]
) -> ConversationState:
    state = store.get(conversation_id)
    if state is None:
        state = ConversationState(
            conversation_id=conversation_id,
            merchant_id=merchant_id or "",
            customer_id=customer_id,
            trigger_id=None,
            kind=None,
            send_as="vera",
        )
        store.create(state)
    return state


def handle_reply(
    store: ConversationStore,
    conversation_id: str,
    merchant_id: Optional[str],
    customer_id: Optional[str],
    message: str,
    turn_number: int,
) -> ReplyDecision:
    state = _get_or_create_state(store, conversation_id, merchant_id, customer_id)
    state.turns.append({"from": "merchant", "message": message, "turn": turn_number})
    mid = state.merchant_id or merchant_id or "unknown"

    # 1. Auto-reply detection (merchant-account-level streak, not per-conversation --
    #    a WhatsApp Business canned reply is a property of the account).
    if _matches_any(message, AUTO_REPLY_MARKERS):
        streak = store.bump_auto_reply_streak(mid)
        if streak >= 3:
            store.mark_status(conversation_id, "ended")
            return ReplyDecision(
                action="end",
                rationale=f"Auto-reply detected {streak}x in a row for this merchant account; no real reply signal. Closing.",
            )
        if streak == 2:
            return ReplyDecision(
                action="wait",
                wait_seconds=14400,
                rationale="Same canned auto-reply twice in a row -- owner likely not at phone. Backing off 4h before retry.",
            )
        body = "Looks like an auto-reply \U0001F642 When the owner sees this, a quick reply here works whenever they're free."
        return ReplyDecision(
            action="send",
            body=body,
            cta="open_ended",
            rationale="First canned auto-reply seen; sending one explicit human-routing prompt rather than repeating the pitch.",
        )

    # Any non-canned message resets the streak -- a real person is typing.
    store.reset_auto_reply_streak(mid)

    # 2. Hostile / opt-out.
    if _matches_any(message, OPTOUT_MARKERS) or _matches_any(message, RUDE_MARKERS):
        store.mark_status(conversation_id, "ended")
        store.opt_out(mid)
        if _matches_any(message, RUDE_MARKERS):
            return ReplyDecision(
                action="send",
                body="Apologies -- won't message again on this. If anything changes, you can always restart with 'Hi Vera'. \U0001F64F",
                cta="none",
                rationale="Merchant expressed frustration; one-line acknowledgment + opt-out path, conversation closes after this send.",
            )
        return ReplyDecision(
            action="end",
            rationale="Merchant explicitly opted out. Closing conversation; suppressing further sends to this merchant.",
        )

    # 3. Explicit intent transition -- switch to action mode immediately,
    #    never re-ask a qualifying question.
    if _matches_any(message, COMMITMENT_MARKERS):
        topic = state.kind.replace("_", " ") if state.kind else None
        what = f"the {topic} follow-through" if topic else "this"
        body = (
            f"Great -- drafting {what} now. I'll have it ready in a couple of minutes. "
            f"Reply CONFIRM once you've seen it and I'll send it live."
        )
        return ReplyDecision(
            action="send",
            body=body,
            cta="binary_confirm_cancel",
            rationale="Merchant gave explicit commitment; switching from qualification to action per intent-transition handling, no further questions asked.",
        )

    # 4. Soft decline -- back off, don't close the door.
    if _matches_any(message, MAYBE_LATER_MARKERS):
        return ReplyDecision(
            action="wait",
            wait_seconds=86400,
            rationale="Merchant asked for time, not a hard no. Backing off 24h before re-approaching.",
        )

    # 5. Plain affirmative / engaged accept -- honor it, echoing back
    #    specifically what the merchant asked for rather than a generic ack.
    lowered = message.lower()
    is_short_affirmative = lowered.strip().strip(".!") in AFFIRMATIVE_SHORT
    is_engaged = bool(re.search(r"\byes\b", lowered)) or _matches_any(lowered, ENGAGED_MARKERS)
    if is_short_affirmative or is_engaged:
        asks = [w for w in ENGAGED_ECHO_WORDS if w in lowered]
        subject = ", ".join(asks) if asks else "that"
        body = f"Sending the {subject} over now -- I'll flag anything else worth doing while I'm in there."
        return ReplyDecision(
            action="send",
            body=body,
            cta="open_ended",
            rationale="Merchant gave a direct affirmative to the prior ask; honoring it and echoing back the specific items they named.",
        )

    # 6. Clear off-topic curveball -- decline politely, redirect to thread.
    topic = (state.kind or "").replace("_", " ")
    if _matches_any(lowered, CURVEBALL_MARKERS):
        topic_line = f" Coming back to the {topic} -- want me to go ahead with that first?" if topic else " Want me to go ahead with what I mentioned earlier?"
        body = f"I'll have to leave that to a specialist -- outside what I can help with directly.{topic_line}"
        return ReplyDecision(
            action="send",
            body=body,
            cta="open_ended",
            rationale="Out-of-scope ask; politely declined and redirected back to the original trigger without losing the thread.",
        )

    # 7. Anything else: treat as a genuine on-topic question and keep going.
    topic_line = f"On the {topic} -- " if topic else ""
    body = f"{topic_line}want me to go ahead, or is there something you'd like changed first?"
    return ReplyDecision(
        action="send",
        body=body,
        cta="open_ended",
        rationale="Message read as an on-topic question rather than a clear accept/decline; checking before proceeding to avoid assuming consent.",
    )
