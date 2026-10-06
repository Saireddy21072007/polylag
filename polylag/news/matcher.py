"""Headline -> trigger matching.

Deliberately boring: normalise the text, then check the all_of / any_of /
none_of phrase lists from config. No embeddings, no LLM, no fuzzy scoring.

Why so plain? Because every trade must be explainable in one sentence to
yourself at 2am, and because a false positive here spends real money. A rule you
wrote by hand fails in ways you can predict; a similarity score fails in ways
you cannot.

The cost of this choice is real: you will miss paraphrased headlines. That is
the correct trade for a system whose main risk is acting on the wrong story.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from ..config import MarketConfig, TriggerRule
from ..models import NewsEvent

log = logging.getLogger("matcher")

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Padded with spaces so word-boundary checks work at the string edges.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text.lower())
    return " " + _WS.sub(" ", text).strip() + " "


def contains_phrase(haystack_normalized: str, phrase: str) -> bool:
    """Whole-word phrase containment.

    'cut' must not match 'haircut', so we compare against the normalised text
    with explicit space padding rather than a naive `in`.
    """
    needle = normalize(phrase)
    if needle.strip() == "":
        return False
    return needle in haystack_normalized


@dataclass(frozen=True)
class TriggerMatch:
    market: MarketConfig
    trigger: TriggerRule
    matched_terms: list[str]

    @property
    def explanation(self) -> str:
        return (
            f"trigger '{self.trigger.name}' matched {self.matched_terms} "
            f"-> favours {self.trigger.outcome} @ {self.trigger.target_price:.2f} "
            f"(confidence {self.trigger.confidence:.2f})"
        )


def evaluate_trigger(trigger: TriggerRule, text_normalized: str) -> tuple[bool, list[str]]:
    """Return (matched, terms_that_matched)."""
    matched: list[str] = []

    for phrase in trigger.none_of:
        if contains_phrase(text_normalized, phrase):
            return False, []  # veto wins outright

    for phrase in trigger.all_of:
        if not contains_phrase(text_normalized, phrase):
            return False, []
        matched.append(phrase)

    if trigger.any_of:
        hits = [p for p in trigger.any_of if contains_phrase(text_normalized, p)]
        if not hits:
            return False, []
        matched.extend(hits)

    return True, matched

class TriggerMatcher:
    def __init__(self, markets: list[MarketConfig]) -> None:
        self.markets = [m for m in markets if m.enabled]

    def match(self, event: NewsEvent) -> list[TriggerMatch]:
        """Every (market, trigger) pair this headline fires.

        One headline can legitimately hit several markets. The risk manager,
        not this function, decides how many of them we are allowed to act on.
        """
        text = normalize(event.text)
        results: list[TriggerMatch] = []
        for market in self.markets:
            for trigger in market.triggers:
                ok, terms = evaluate_trigger(trigger, text)
                if ok:
                    results.append(TriggerMatch(market, trigger, terms))
                    log.info(
                        "MATCH %s/%s <- %r", market.slug, trigger.name, event.title[:90]
                    )
        return results

