"""The single gate every client-facing price must pass through.

WHY THIS MODULE EXISTS
    The pricing engine is stateless by design: it answers "what does this
    booking cost" from the knowledge base alone and knows nothing about the
    conversation. That is correct for pricing and wrong for *quoting*.

    Concretely: the agent concedes 2 500 ₽/h in message 4. Two messages
    later the orchestrator recalculates — perhaps the client tweaked the
    hours — and the engine faithfully returns the base 3 500 ₽/h, because
    from its point of view nothing about the booking implies a discount.
    The agent then quotes a price *higher* than one it already promised.
    That is the ratchet leaking, and it leaks at the boundary, not inside
    any one concession branch.

RULE
    Any PriceQuote that is about to be turned into a message for the client
    MUST first pass through `apply_dialog_floor`. No exceptions — not for
    "just re-checking availability", not for a different zone in the same
    dialog. If a quote skips this gate, the ratchet is not enforced.

    The concession engine applies its own per-grant ratchet check as well;
    that one guards the moment of granting, this one guards every quote
    afterwards. Both are needed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from app.pricing.engine import Money, PriceQuote

if TYPE_CHECKING:  # avoids a runtime import cycle
    from app.pricing.concessions import DialogConcessionState

RATCHET_WARNING = "Применён храповик диалога: клиенту уже называлась цена ниже."


def apply_dialog_floor(quote: PriceQuote, state: "DialogConcessionState") -> PriceQuote:
    """Clamp `quote` to the lowest price already promised in this dialog.

    Leaves the quote untouched when there is nothing to clamp to (no prior
    concession), when the quote is not priced at all (blocked / invalid /
    needs_input), or when the fresh calculation is already at or below the
    promised price.
    """
    if state.floor_reached is None:
        return quote
    if quote.status != "ok" or quote.total is None:
        return quote
    if quote.total <= state.floor_reached:
        return quote

    clamped: Money = state.floor_reached
    hours = quote.occupied_hours
    text = (
        f"Итого {clamped} ₽ за {hours} ч" if hours is not None else f"Итого {clamped} ₽"
    )

    return replace(
        quote,
        total=clamped,
        warnings=quote.warnings + (RATCHET_WARNING,),
        human_readable=text,
        # The per-hour rate that produced the original lines no longer holds,
        # so we do not keep claiming it.
        base_rate=None,
    )
