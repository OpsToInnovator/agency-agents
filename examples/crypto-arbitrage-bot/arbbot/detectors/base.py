from __future__ import annotations

from ..models import Opportunity, Quote
from ..quotes import QuoteBook


class Detector:
    """A detector is called once per quote update and may return opportunities
    that involve the updated market. It must be cheap: it runs inline on the
    feed consumer."""

    name = "detector"

    def on_quote(self, q: Quote, book: QuoteBook, now: float) -> list[Opportunity]:
        raise NotImplementedError
