<p>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="netagent/web/brand/logo-dark.png">
    <img src="netagent/web/brand/logo-light.png" alt="BlackHawk Data" width="360">
  </picture>
</p>

# BlackHawk NetAgent

A self-hosted multi-agent AI platform. Agents take work from Microsoft Teams, email, a REST API or the web
console, answer with small fast models where they can, and escalate to larger models only when the task
needs it. The first agent profiles are network engineers for **Cisco**, **Fortinet** and **Arista**.

- **Runs anywhere** - one Python service with an embedded database. Windows, Linux or Docker.
- **Local and cloud models** - Ollama and any OpenAI-compatible endpoint (LM Studio, vLLM, llama.cpp,
  gateways) alongside Claude, mixed freely in one escalation ladder.
- **Escalation ladder** - a small model triages every task and answers the easy ones; low confidence, an
  explicit "this is beyond me", or an outage hands the task up a tier with the evidence already gathered.
- **Teams, email and API** in; results and notifications back out the same way.
- **Web console** - spawn agents from profiles, watch tasks live, chat, approve changes, manage models,
  devices and integrations, and read completion reports.
- **Agents work together** - a coordinator routes multi-vendor problems and agents consult or delegate
  to each other; every hand-off is a tracked sub-task.
- **Safe by construction for network devices** - agents run read-only commands themselves; every
  configuration change is a change request a human approves in the console before anything is pushed.

```
 Teams ─┐                         ┌──────────── Agents (spawned from profiles) ────────────┐
 Email ─┤   ┌──────────────┐      │  Network Lead ─ consult/delegate ─► Cisco Expert       │
 API  ──┼──►│ Task queue + │─────►│  (coordinator)                     ► Fortinet Expert   │
 Web  ──┘   │ router       │      │                                    ► Arista Expert     │
            └──────────────┘      └───────────────┬────────────────────────────────────────┘
                                                  │ each task
                         ┌────────────────────────▼─────────────────────────┐
                         │ Escalation ladder                                │
                         │ triage ► tier 1 (local, small) ► tier 2 ► tier 3 │──► tools: devices (read-only),
                         │          escalate on low confidence / failure    │    change requests, playbooks,
                         └──────────────────────────────────────────────────┘    subnet/config utilities
```

## Quick start

### Try it in two minutes (no model, no API key)

Demo mode uses offline mock models and three simulated devices, so you can click through everything.

```bash
# Linux / macOS
./scripts/install.sh
NETAGENT_DEMO=1 ./scripts/run.sh
```
```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$env:NETAGENT_DEMO = 1; .\scripts\run.ps1
```
```bash
# Docker
docker build -t netagent .
docker run -p 8080:8080 -e NETAGENT_DEMO=1 -e NETAGENT_ADMIN_TOKEN=change-me -v netagent-data:/data netagent
```

Open <http://localhost:8080> and sign in with the admin token (printed at first start, or
`python -m netagent token`). Create a task such as *"Add VLAN 120 named GUEST on sw-core-01"* and watch the
triage, the tool calls and the change request appear.

### Run it for real

1. Copy `.env.example` to `.env`.
2. Give it at least one model (see [Models](#models-and-the-escalation-ladder)):
   - cloud: set `ANTHROPIC_API_KEY`;
   - local: install [Ollama](https://ollama.com), then `ollama pull llama3.2:3b` (and optionally
     `ollama pull qwen2.5:14b`).
3. Start it (`./scripts/run.sh`, `.\scripts\run.ps1`, or `docker compose up -d`).

`docker compose --profile local up -d` also starts an Ollama container next to NetAgent.

| Run as a service | How |
|---|---|
| Linux | `scripts/netagent.service` (systemd unit, instructions inside) |
| Windows | `scripts\install-windows-service.ps1` (Task Scheduler, starts at boot) |
| Docker | `docker compose up -d` (restarts automatically, data in the `netagent-data` volume) |

Put a TLS-terminating reverse proxy in front for anything beyond a lab, and set `NETAGENT_PUBLIC_URL` to
the external address (it is used in Teams/email links and webhook URLs).

## Models and the escalation ladder

Models and ladders are managed in the console under **Models & Ladders** (seeded on first start from
`netagent/defaults.yaml`, or from `config/config.yaml` if you create one).

| Provider | Use it for | Needs |
|---|---|---|
| `ollama` | Local models | Base URL (default `http://localhost:11434`) |
| `openai_compatible` | LM Studio, vLLM, llama.cpp server, LocalAI, hosted gateways | Base URL, optional key |
| `anthropic` | Claude (via the official SDK) | `ANTHROPIC_API_KEY` or a key stored on the model |
| `openai`, `azure_openai` | Hosted OpenAI-format APIs | Key (and base URL for Azure) |

The default **Standard** ladder:

| Tier | Model | Starts tasks up to complexity | Escalates below confidence |
|---|---|---|---|
| 1 | Local small (Ollama `llama3.2:3b`) | 1 | 80% |
| 2 | Claude Haiku 5.5 | 2 | 75% |
| 3 | Claude Sonnet 5.5 | 4 | 70% |
| 4 | Claude Opus 5.5 | 5 | final tier |

How a task moves through it:

1. **Triage** - the smallest available model classifies the task: complexity 1-5, category, risk.
2. **Start tier** - the first tier allowed to start that complexity. High-risk work and configuration
   changes never start below the ladder's *high-risk minimum tier*.
3. **Attempt** - the model works the task with its tools and ends with a self-rated confidence, or
   replies `ESCALATE: <reason>`.
4. **Escalate** - confidence under the tier's threshold, no self-assessment, an explicit escalate, or a
   model error hands the task to the next tier, together with the draft and the tool results already
   gathered, so commands are not re-run.
5. **Review** (optional, on by default for high-risk work) - the next tier up checks a confident answer
   before release; a rejection escalates.
6. **Final tier** - answers regardless. If it is still unsure, the task completes **flagged for human
   review**.

Tiers whose model is unavailable (no key, runtime down) are skipped and temporarily circuit-broken, so
one ladder works local-only, cloud-only or mixed. `local-only` (air-gapped) and `cloud-only` ladders are
included; agents and profiles can each be pinned to a ladder. Per-model token prices feed the cost
figures in reports.

## Agents and profiles

A **profile** is a personality, a brief, a set of skills, knowledge packs and routing keywords. An
**agent** is a named instance of a profile with its own queue. Spawn as many as you need from
**Agents > Spawn agent**.

Built-in profiles (`profiles/*.yaml`):

| Profile | Role |
|---|---|
| Network Lead | Coordinator: takes unaddressed and multi-vendor requests, consults the specialists |
| Cisco Network Engineer | IOS, IOS-XE, NX-OS, IOS-XR, ASA |
| Fortinet Security Engineer | FortiGate / FortiOS: policy, NAT, IPsec/SSL VPN, SD-WAN, HA |
| Arista Network Engineer | EOS: leaf/spine, MLAG, BGP EVPN/VXLAN, CloudVision |
| General Assistant | Non-network starting point for your own profiles |

Create your own under **Profiles > New profile** (or duplicate a built-in one), or drop a YAML file in
`profiles/`. Skills decide which tools an agent gets:

| Skill | Tools |
|---|---|
| `network-core` | `subnet_calc`, `analyze_config` (config lint), `compare_configs`, `vendor_command_reference` (Cisco/NX-OS/EOS/FortiOS equivalents), `lookup_playbook` |
| `device-access` | `list_devices`, `run_show_command` (read-only), `propose_config_change` (human approval) |
| `collaboration` | `list_agents`, `consult_agent`, `delegate_task`, `check_task` |
| `task-tracking` (always on) | `add_task_note` |

**Knowledge packs** are folders of Markdown playbooks under `knowledge/<pack>/`. Fourteen ship with the
product (OSPF, BGP, L2, vPC for Cisco; IPsec, policy/flow debugging, HA, SD-WAN for Fortinet; MLAG,
EVPN/VXLAN, config sessions, BGP for Arista; method and change safety for all). Add your own runbooks as
`.md` files - no restart needed.

**Routing** of unaddressed tasks: if the text matches one specialist profile's keywords it goes to that
profile's least-busy agent; if it matches several (a multi-vendor request) or none, it goes to the
coordinator. People can also address an agent directly (`Cisco Expert: ...`).

## Devices and change control

Add devices under **Devices** (SSH via Netmiko; platforms: Cisco IOS, IOS-XE, NX-OS, IOS-XR, ASA, Arista
EOS, FortiOS). Passwords are stored encrypted and never returned by the API.

- **Read-only by default.** Agents can run `show ...`, `ping`, `traceroute` (FortiOS: `get`, `show`,
  `execute ping`, and a fixed list of safe `diagnose` commands). Chaining, redirection and anything else
  is refused before it reaches the device. Use a read-only device account where you can.
- **Credentials are redacted** from device output before any model sees it (hashes, SNMP communities,
  pre-shared keys, FortiOS `ENC` values).
- **Changes need a human.** An agent can only *propose* a change: commands, rollback, verification, risk.
  It appears under **Approvals**. On approval NetAgent takes a configuration backup, pushes the lines,
  runs the verification commands and records the output. Pushes are possible only for devices with
  *Allow configuration changes* enabled.

## Integrations

Configure under **Integrations**; secrets are encrypted at rest.

### Microsoft Teams

| Mode | What you get | Setup |
|---|---|---|
| **Bot** (recommended) | Two-way: message the bot, it acknowledges and posts the result in the same conversation | Create an Azure Bot resource (single-tenant), set its messaging endpoint to `https://<your-host>/hooks/teams/messages`, enable the Teams channel, add the bot to a team or chat. Enter App ID, client secret and tenant ID, and restrict *allowed tenant IDs*. |
| **Outgoing webhook** | @mention in a channel, no Azure resources | In Teams: *Manage team > Apps > Create an outgoing webhook*, URL `https://<your-host>/hooks/teams/outgoing`. Paste the security token Teams shows. Quick answers return inline; longer ones arrive via the notifications webhook. |
| **Notifications** | A card in a channel when tasks finish/fail or a change needs approval | Create an incoming webhook (Workflows: "Post to a channel when a webhook request is received") and paste its URL under *Notifications*. |

Bot requests are authenticated by validating the Bot Framework token; outgoing-webhook requests by their
HMAC signature. In chat: `help`, `agents`, `tasks`, `status <id>`, or `<agent name>: <request>`.

### Email

Send a mail to the agent mailbox; the answer comes back as a reply. A subject prefix `[Agent name]`
addresses an agent. **Only senders on the allow-list create tasks**; auto-replies are ignored.

- `graph` - Microsoft 365. Register an Entra ID application with `Mail.ReadWrite` and `Mail.Send`
  application permissions (ideally scoped to the one mailbox with an application access policy).
- `imap_smtp` - any other mailbox.

### REST API

Interactive reference at `/api/docs`. Create keys under **Integrations > API keys** (`tasks` scope can
only submit and read tasks; `admin` can manage everything).

```bash
# Create a task (asynchronous; optional callback when it finishes)
curl -X POST http://localhost:8080/api/v1/tasks \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"title": "BGP neighbor down on leaf-dc1-01", "description": "...", "agent": "Arista Expert",
       "priority": 2, "callback_url": "https://itsm.example.com/hooks/netagent"}'

# Ask and wait for the answer
curl -X POST http://localhost:8080/api/v1/ask \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"question": "What is the wildcard mask for a /22?"}'

curl -H "Authorization: Bearer $KEY" http://localhost:8080/api/v1/tasks/42          # status, result, timeline
curl -H "Authorization: Bearer $KEY" http://localhost:8080/api/v1/tasks/42/report   # Markdown report
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/api/v1/reports/summary?days=7"
```

## Task tracking and reporting

Every request - from any channel, and every agent-to-agent hand-off - is a task with a status
(`queued`, `running`, `completed`, `failed`, `cancelled`), a priority, an owner and a full timeline:
triage, each tier attempt, tool calls, escalations, reviews, collaboration, approvals, delivery.

- **On completion** the requester is answered where they asked (Teams conversation, email reply, API
  callback), and the notification targets get a card/mail. Failures and low-confidence results are called
  out.
- **Reports** page: completion and success rate, time to complete, escalation rate, share answered
  locally, estimated model cost, breakdowns by agent / model tier / channel, CSV export, and an optional
  daily summary to Teams or email.
- Per-task Markdown report: `GET /api/v1/tasks/{id}/report`.

Tasks interrupted by a restart are put back on the queue.

## Configuration reference

Environment variables (or `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `NETAGENT_ADMIN_TOKEN` | generated | Console sign-in and master API token |
| `NETAGENT_HOST` / `NETAGENT_PORT` | `0.0.0.0` / `8080` | Listen address |
| `NETAGENT_PUBLIC_URL` | `http://localhost:<port>` | External URL used in links and webhook addresses |
| `NETAGENT_DATA_DIR` | `./data` | Database, generated token and encryption key |
| `NETAGENT_SECRET_KEY` | generated | Key that encrypts stored credentials - back it up with the database |
| `NETAGENT_CONFIG` | `config/config.yaml` if present | Seed file for models and ladders (first start only) |
| `NETAGENT_PROFILES_DIR` / `NETAGENT_KNOWLEDGE_DIR` | `./profiles` / `./knowledge` | Profile and playbook folders |
| `NETAGENT_DEMO` | off | Mock models and simulated devices |
| `ANTHROPIC_API_KEY` | - | Claude tiers |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local tiers |

Behind a TLS-inspecting proxy: build with `docker build --secret id=ca,src=corp-ca.pem .` and run with
`SSL_CERT_FILE` pointing at the same bundle.

## Security notes

- Sign-in is a single admin token; API keys are stored hashed and can be limited to task operations.
  Browser sessions use an HttpOnly cookie plus a required custom header on writes.
- Task text from Teams, email and the API is untrusted input to a model. The design assumes it can be
  adversarial: agents cannot change a device without human approval, device commands pass a read-only
  allow-list, inbound channels are restricted (tenant allow-list, sender allow-list, signatures), and
  agent-to-agent delegation has a depth limit.
- Task content is sent to whichever model tier handles it. Use the `local-only` ladder when nothing may
  leave the network.
- `callback_url` makes the server POST to a URL chosen by an API-key holder; issue keys accordingly.
- Review anything flagged *needs review*, and every change request, before acting on it.

## Development

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest          # 112 tests, no network or API key needed
```

```
netagent/
  ladder.py        escalation ladder (triage, attempt, escalate, review)
  agent.py         prompt assembly and the tool loop for one model attempt
  orchestrator.py  task queue, routing, execution, restart recovery
  models/          provider adapters: anthropic, OpenAI-compatible (Ollama etc.), mock
  tools/           network toolkit, device access + change requests, collaboration, tracking
  channels/        Teams, email, notifications, API callbacks
  api/             REST API, live event stream, webhooks
  web/             console (static HTML/CSS/JS, no build step)
  reports.py       summaries, per-task reports, CSV
profiles/          built-in agent profiles (YAML)
knowledge/         playbook library (Markdown)
```
