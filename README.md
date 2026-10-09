# NexaBank AI Assistant: OPA + Envoy Policy Governance demo

> **The LLM proposes the action. OPA decides whether policy permits it. Envoy enforces OPA's decision.**

```text
browser ─► Envoy :8000 ─► assistant ─► Groq LLM proposes a tool call
                          assistant ─► Envoy :9000 ─(ext_authz → OPA decides)─► bank-api executes it
```

The Rego policies are intentionally not included. You write them in class with [DEMO.md](DEMO.md). Until then,
Envoy denies every tool call (fail-closed).

---

## Setup

Prerequisites: **Docker Desktop** (running) and a **Groq API key** (<https://console.groq.com/keys>). No local Python needed.

1. Create `.env` in the project folder (skip this if it already exists):

   ```ini
   GROQ_API_KEY=gsk_your_key_here
   GROQ_MODEL=openai/gpt-oss-120b
   ```

   | Variable       | Meaning |
   |----------------|---------|
   | `GROQ_API_KEY` | Your Groq key (required) |
   | `GROQ_MODEL`   | Any Groq model with tool calling, e.g. `openai/gpt-oss-120b` or `llama-3.3-70b-versatile` |

2. Start everything:

   ```powershell
   cd "D:\AI Governance Code\04_OPA"
   docker compose up -d --build
   ```

3. Open <http://localhost:8000> (served through Envoy).

After editing `.env`, run `docker compose up -d`. Everything else happens in the browser: `.rego` files saved in
`policies/` go live within ~2 s, and so do edits to `envoy/listeners.yaml`.

---

## Using the app

| Tab | What you do there |
|---|---|
| 🤖 **Assistant** | Click a **scenario** (or type a request) and watch the **request path**: each hop (assistant → Envoy → bank API) turns green (allowed), red (denied by OPA) or grey (not reached). The **governance trace** shows the LLM's proposed tool call, the HTTP request sent through Envoy, OPA's decision and reasons, and the tool result. |
| 📜 **Policies** | Each file in `policies/` with `LOADED` or `REJECTED` (plus OPA's error). A rejected file also shows as a red banner on every tab. |
| ⚖️ **Ask OPA** | Query a rule (`data.nexabank.tools`, `…decision`, `…allow`, `…reasons`) with your own `input` JSON. Presets include the last tool call from the Assistant tab. |
| 🧾 **Audit** | OPA's decision log (click a row for the full input and result) and Envoy's ext_authz counters: allowed / denied / OPA unreachable. |
| 🛠️ **Operations** | Switch Envoy to **fail-open**, **simulate an OPA outage**, view the live ext_authz config, and **try to bypass Envoy**. All applied live. |

---

## Project layout

```text
04_OPA/
├── .env                      your settings (see Setup)
├── docker-compose.yml        envoy + opa + control + assistant + bank-api (bank-api on a private network)
├── envoy/
│   ├── envoy.yaml            Envoy bootstrap: clusters, admin, and where to load listeners from
│   └── listeners.yaml        listeners :8000 (UI) and :9000 (the PEP: ext_authz filter → OPA, failure_mode_allow)
├── app/                      one Python image (standard library only), no policy code
│   ├── assistant.py          UI/API + Groq agent; sends tool calls through Envoy with a trusted context header
│   ├── bank_api.py           the tools (lookup, refund, transfer)
│   ├── control.py            control plane: pushes policies into OPA, renders Envoy's listeners, collects OPA's
│   │                         decision logs
│   ├── static/index.html     the UI
│   └── Dockerfile
└── policies/
    └── org/data.json         organizational reference data → data.org   (you add the .rego files here)
```
