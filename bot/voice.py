"""Voice / language helpers shared by every trigger-kind builder.

Keeps category taboo words out of generated copy and decides when to mix in
Hindi-English phrasing, matching each CategoryContext.voice profile.
"""

from __future__ import annotations

from typing import Any, Optional

HINGLISH_MARKERS = {"hi", "hi-en mix", "hi-en", "mr", "te", "ta", "kn"}


def owner_first_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity", {})
    name = identity.get("owner_first_name")
    if name:
        return name.replace("Dr. ", "").strip()
    return identity.get("name", "there")


def salutation(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity", {})
    owner = identity.get("owner_first_name")
    if owner:
        return owner
    return identity.get("name", "there")


def customer_first_name(customer: dict[str, Any]) -> str:
    name = customer.get("identity", {}).get("name", "there")
    # Some seed names carry a parent annotation, e.g. "Aanya (parent: Sneha)".
    if "(" in name:
        return name.split("(")[0].strip()
    return name


def wants_hinglish(merchant: Optional[dict[str, Any]] = None, customer: Optional[dict[str, Any]] = None) -> bool:
    if customer is not None:
        pref = customer.get("identity", {}).get("language_pref", "")
        if isinstance(pref, str) and ("hi" in pref.lower() or "mix" in pref.lower()):
            return True
        return False
    if merchant is not None:
        langs = merchant.get("identity", {}).get("languages", [])
        return "hi" in langs
    return False


def taboo_words(category: dict[str, Any]) -> list[str]:
    return [w.lower() for w in category.get("voice", {}).get("vocab_taboo", [])]


def contains_taboo(text: str, category: dict[str, Any]) -> bool:
    """Guard used by compose() before returning: our templates never write
    taboo words intentionally, but a category-fit penalty is expensive
    enough to check for it anyway."""
    lowered = text.lower()
    return any(word and word in lowered for word in taboo_words(category))
