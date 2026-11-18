# 2026-11-18 — One job per Discord message

Each bot digest message contains one job card with its four decision buttons directly beneath it. The bot sends up to 30 eligible jobs per check in sponsorship order. It rechecks each job before sending and records notification after Discord acknowledges that message. Previously delivered messages retain their original layout.

Local regression tests cover button IDs, notification records after a send failure, and eligibility changes between messages.

Deployed to `home-ubuntu` on 2026-11-18 at 14:06 PST. The deployed source checksums matched the tested release, and the bot and screening worker were active after deployment. The first 30 new messages in Discord each contained one job card followed by Want to apply, Skip, Applied, and Shouldn't recommend. [Example message](https://discord.com/channels/1572708740254269475/1572709270489923704/1572734903118860360).
