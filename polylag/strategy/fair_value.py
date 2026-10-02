"""A transparent fair-value estimate. One line of arithmetic, fully auditable.

The whole model
---------------
    fair_value = pre_news_mid + confidence * (target_price - pre_news_mid)

Three inputs, all of which you can point at:

  pre_news_mid   the market's own mid immediately BEFORE the headline arrived.
                 We anchor on the market rather than on our opinion, because
                 the market knows vastly more than we do about everything
                 except this one headline.
  target_price   where YOU wrote in config.yaml that this market belongs if
                 this specific headline is true.
  confidence     0..1, how far to travel from the anchor toward the target.
                 0.5 means "I believe this headline moves it halfway there".

This is NOT a probability model and makes no claim to be right. It is a way of
writing your prior down in advance so that you cannot rationalise a trade after
seeing the price. If the number it produces looks silly, your config is wrong --
which is the point: the error is visible and editable.

Explicitly NOT modelled here: correlations between markets, partial/ambiguous
headlines, whether the headline is already priced in from an earlier report,
whether the source is reliable. Those are your judgement, expressed as
`confidence` and as narrow trigger phrases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import TriggerRule
from ..models import Outcome


def clamp_probability(value: float, floor: float = 0.01, cap: float = 0.99) -> float:
    """Keep estimates inside the tradable range."""
    return max(floor, min(cap, value))


def estimate_fair_value(pre_news_mid: float, trigger: TriggerRule) -> float:
    """Blend the market's pre-news anchor toward your configured target."""
    move = trigger.confidence * (trigger.target_price - pre_news_mid)
    return clamp_probability(pre_news_mid + move)


def complement(price: float) -> float:
    """YES and NO prices sum to 1. Used to translate between the two legs."""
    return 1.0 - price


@dataclass(frozen=True)
class FairValueView:
    """Fair value for both legs of one binary market, plus the evidence."""

    outcome: Outcome
    pre_news_mid: float
    current_mid: float
    fair_value: float  # for `outcome`'s token
    drift_since_news: float  # current_mid - pre_news_mid, in probability units

    @property
    def already_moved_toward_target(self) -> float:
        """How much of our expected move the market has ALREADY made.

        If this is large, the lag we hoped to exploit is gone and the remaining
        edge is somebody else's exit liquidity.
        """
        expected = self.fair_value - self.pre_news_mid
        if abs(expected) < 1e-9:
            return 0.0
        return self.drift_since_news / expected


def build_view(
    trigger: TriggerRule,
    pre_news_mid: float,
    current_mid: float,
) -> FairValueView:
    fv = estimate_fair_value(pre_news_mid, trigger)
    return FairValueView(
        outcome=trigger.outcome,  # type: ignore[arg-type]
        pre_news_mid=pre_news_mid,
        current_mid=current_mid,
        fair_value=fv,
        drift_since_news=current_mid - pre_news_mid,
    )


def edge_for_buy(fair_value: float, ask: Optional[float], cost_buffer: float) -> Optional[float]:
    """Edge on a BUY after subtracting the cost haircut. None if no ask."""
    if ask is None:
        return None
    return fair_value - ask - cost_buffer
