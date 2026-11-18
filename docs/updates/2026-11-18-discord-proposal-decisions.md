# 2026-11-18 — Discord screening proposal decisions

- **Expiry.** Apply and Discard both enforce the 24-hour expiry while holding the screening rules lock. Clicking an
  expired proposal returns its unresolved reasons to the next daily summary without reading other proposals.
- **Buttons.** A successful decision or a confirmed unavailable proposal clears the controls. A lock conflict or a
  file read error keeps the controls available for retry. If the rules revision changes, Apply asks for a fresh
  preview, and the owner can still discard the current proposal.
- **Verification.** Local tests use a temporary SQLite database and simulated Discord interactions. They cover the
  24-hour boundary, both actions under lock contention and after retry, a stale rules revision, failed reads of the
  selected proposal and an unrelated proposal, and the owner and guild checks.

See the [Discord bot guide](../discord-bot.md) for the owner workflow.
