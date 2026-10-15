"""Card-level filters: explicit, cheap rules applied before any detail fetch."""

import re

from auto_app.core.config import CardFilters
from auto_app.core.models import Card


def reject_reason(card: Card, filters: CardFilters) -> str | None:
    title = card.title or (card.lines[0] if card.lines else "")
    for word in filters.exclude_title_words:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", title, re.IGNORECASE):
            return f"title word: {word}"
    title_text = title.strip().lower()
    lines = [line.strip().lower() for line in card.lines]
    # The first visible title can retain a parenthesized badge that the source
    # removed from card.title (for example LinkedIn's "Verified job").
    lines = [line for index, line in enumerate(lines)
             if line != title_text and not (index == 0 and line.startswith(title_text + " ("))]
    for company in filters.exclude_companies:
        c = company.lower()
        if any(line == c or line.startswith(c + " ") for line in lines):
            return f"company: {company}"
    return None
