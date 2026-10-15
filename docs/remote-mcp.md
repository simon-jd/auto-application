# Remote MCP and control tools

**Current state (2026-10-14):** source is deployed on Ubuntu with database schema
`8d4e0f2a5b31`. The existing private MCP service is active with `--control-tools`.
The Grok MCP and named-tunnel units are installed and passed Linux systemd verification,
but remain disabled/inactive. Only `.example` files are staged; real Grok configuration,
Bearer token and tunnel credentials have not been installed. Dependencies include MCP
2.3.0 and checksum-verified cloudflared 2026.10.0. No public tunnel or Grok call has run.
Use the [Linux connector setup checklist](linux-connectors-setup.md) to fill values on Linux
and then choose activation. Grok's static Bearer compatibility remains unverified.

**Historical state (2026-10-12):** the second listener and authentication had local code/tests;
no Linux deployment, token installation, public tunnel or Grok connection had been attempted.

## Two listeners

| Listener | Intended client | Authentication | Network path |
|---|---|---|---|
| Existing `auto-app-agent.service`, port 8767 | Dot through the private outbound OpenAI tunnel | No MCP token; loopback bind only | Private tunnel; never publish this listener |
| `auto-app-agent-public.service`, port 8769 | Grok or an explicitly configured personal agent | A mode-600 Bearer token file | Loopback bind behind one public HTTPS tunnel |

Both use Streamable HTTP at `/mcp` and the same saved job database. The public listener
binds `127.0.0.1:8769`; it requires `--auth-token-file` and a matching HTTPS public origin
(host only, no `/mcp` path). The installed [service template](../deploy/auto-app-agent-public.service)
reads `AUTO_APP_MCP_PUBLIC_URL` from `runtime/config/grok.env`; edit that runtime file,
not a URL literal in the unit. Its token stays in the mode-600 regular file
`runtime/credentials/mcp-token`. Missing files prevent startup. Owner fill/edit/chmod/start
commands are in the [current setup guide](linux-connectors-setup.md).
Unknown HTTP Hosts are rejected; local unauthenticated/wrong-token requests returned 401
in tests. This has not been checked through a real public tunnel. The installed template
enables control tools but does not enable handoff tools.

The token is read at service start. To rotate it, replace the private file atomically, update each authorized client, then restart the service. For example, run this from `~/Desktop/job-search`:

```bash
python3 -c 'import os,pathlib,secrets,tempfile; p=pathlib.Path("runtime/credentials/mcp-token"); fd,name=tempfile.mkstemp(dir=p.parent,prefix=".mcp-token."); os.fchmod(fd,0o600); os.write(fd,(secrets.token_urlsafe(48)+"\n").encode()); os.close(fd); os.replace(name,p)'
systemctl --user restart auto-app-agent-public.service
```

Old tokens cease to work after that restart. Do not paste either token into chat or logs. The app password or Discord token cannot substitute for the MCP token.

## Public HTTPS tunnel options

The installed option is a Cloudflare named tunnel; configuration and activation remain
owner actions. Forward only `http://127.0.0.1:8769`. The installed binary is
`~/Desktop/job-search/bin/cloudflared`, and `job-search-grok-tunnel.service` reads
`runtime/config/grok-tunnel.yml`. That YAML must name the tunnel UUID, its matching
`runtime/credentials/<UUID>.json`, the chosen hostname and a final 404 rule. The service
checks the referenced credential path/type/mode without reading its contents.
Follow the [current guide](linux-connectors-setup.md#2-准备-named-tunnel-与-grok-地址) for
DNS routing, ingress validation and startup; installing the binary/unit did not create or run a tunnel.
Cloudflare documents [named tunnel creation](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)
and [ingress configuration](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/).

Tailscale Funnel remains an alternative, not the installed connector plan. Tailscale
[documents Funnel](https://tailscale.com/docs/reference/tailscale-cli/funnel) as a public HTTPS
reverse proxy. After separately installing/signing in to Tailscale on Linux:

```bash
tailscale funnel --bg 8769
tailscale funnel status
# Public origin: https://<machine>.<tailnet>.ts.net
```

The client endpoint is the selected `https://` origin plus `/mcp`. A temporary quick tunnel
is not the planned durable endpoint. Neither public tunnel option has been activated or tested.

## Grok connector acceptance

The [current xAI connector instructions](https://docs.x.ai/grok/connectors) say to open `grok.com/connectors` → **New Connector** → **Custom**, enter a publicly reachable MCP server URL, and complete any required authentication. They do **not** establish that the Grok UI accepts this server's static Bearer token. Once the owner elects a tunnel and configures the real endpoint, use `https://<tunnel-host>/mcp`, inspect the authentication flow without disclosing the token, and ask Grok to call `get_job_context` and `search_jobs`. Confirm separately that an unauthenticated request receives 401 and a valid authorized call returns the saved context. Record the result in [P1 acceptance](updates/2026-10-12-p1-coverage-and-control.md).

If Grok supports the required Bearer credential, configure it there. If it only offers OAuth for this connector, stop the Grok hookup and design a separate single-user OAuth authorization service; do not infer that this Bearer endpoint is compatible. Dot's private listener and other clients supporting Bearer tokens can continue independently. The xAI [tunneling guide](https://docs.x.ai/grok/connectors/custom-mcp-tunneling) confirms that localhost/private URLs are not reachable by Grok and that a tunnel does not replace authentication.

## Control tools and confirmation

Start either listener with `--control-tools` only where owner-controlled actions are intended. The service publishes `get_pipeline_status`, `pause_pipeline`, `resume_pipeline`, `get_screening_rules`, `propose_screening_change`, `apply_screening_change`, `record_filter_feedback`, and `get_digest`, alongside the existing library tools. There is no shell, SQL, arbitrary file-write or application-submission tool. `get_digest` reads pending digest items without marking them notified and uses the same country preferences as library searches, including an explicit `--preferences` file. `record_filter_feedback` labels a filter verdict; it does not change an application decision.

On 2026-10-14 the private backend was refreshed: its UI displays all 18 deployed tools
as **Read 8 / Write 10**, and plugin 0.3.0 was uploaded and read back. Actual connected-cloud
calls to `get_job_context`, `search_jobs` and `get_job` succeeded on the new code. The running
Codex session still exposes its older six-tool catalog, so this does not establish that a new
control tool has been called through the cloud. [Exact current inventory and boundaries](dot-setup.md#tools).

`pause_pipeline` and `resume_pipeline` control the screening/enrichment/notification worker **and the bot's digest loop**; the repository and mailbox ingest worker keeps collecting on its own schedule. Pause can carry an ISO deadline. `get_pipeline_status` reports the two workers' recent state and source health without exposing credentials.

For screening rules, the client must **propose → show the user the diff and preview counts → obtain the user's confirmation → apply**. `propose_screening_change(changes)` stores a proposal but does not activate it. `apply_screening_change(change_id, expected_revision)` writes only if the saved rule revision still matches; a concurrent change requires a fresh preview. A proposal expires after 24 hours: applying it then fails with "expired", and expired proposals are removed the next time one is proposed. Do not treat a proposal or a model's preference as user approval. Manual application status remains a separate explicit decision and never submits a form.


## P2/P3 interface additions (2026-10-13)

Base MCP now includes `record_application_event(job_id, kind, occurred_at=None, note=None)` alongside
manual status recording. `get_job` returns its timeline. With `--control-tools`, the listener also
exposes `list_unmatched_updates`, `assign_update(update_id, job_id)` and `dismiss_update(update_id)`.
Only explicit owner review assigns ambiguous mail. [Tracking contract](application-tracking.md).

Application handoff is separately opt-in via `--handoff-tools`. The CLI rejects this flag unless
`--auth-token-file` and `--public-url` are supplied together with Streamable HTTP. The existing private
unauthenticated Dot listener does not gain handoff tools. The flag publishes queue, claim, token-gated
packet, reported attempt, release, account metadata and resume registration; it does not execute an
agent or submit forms. There is no MCP requeue tool; the owner uses CLI `handoff requeue`.
[Handoff contract and exact mapping](agent-handoff.md).

```bash
# Configuration example only; owner chooses activation/network exposure separately.
uv run auto-app-agent serve --db /srv/job-search/runtime/data/auto_app.db \
  --transport streamable-http --host 127.0.0.1 --port 8769 \
  --auth-token-file /srv/job-search/runtime/credentials/mcp-token \
  --public-url https://mcp.example.com --control-tools --handoff-tools
```

Claim tokens are short-lived capabilities, never persistent client credentials. Tool schemas must
be refreshed in each connected client, then verified through actual calls; local help/tests do not
prove cloud availability. At the 2026-10-13 P2/P3 implementation checkpoint, no new listener,
public tunnel, client connection or deployment had been activated. The 2026-10-14 private
deployment and still-inactive public connector installation are recorded above.
