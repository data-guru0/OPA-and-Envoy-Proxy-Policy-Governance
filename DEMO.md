# DEMO: Building AI Policy Governance with OPA + Envoy, step by step
---

The UI has five tabs. Students use all of them during the class:

| Tab | What it's for |
|---|---|
| 🤖 **Assistant** | Send requests (or click scenarios) and watch the request path light up through Envoy and OPA |
| 📜 **Policies** | See each policy file `LOADED` or `REJECTED` (with OPA's error) |
| ⚖️ **Ask OPA** | Query any policy rule directly with your own `input` JSON |
| 🧾 **Audit** | OPA's decision log (every decision with its full input/result) and Envoy's enforcement counters |
| 🛠️ **Operations** | Flip Envoy to fail-open, simulate an OPA outage, try to bypass Envoy |

> Every time you **save** a `.rego` file in `policies/`, the control plane pushes it into OPA within ~2 s. No restarts.
> If OPA rejects it (a typo), a **red banner** appears at the top of every tab with OPA's `row:col` error, and
> OPA keeps the last good version.

---


##  Tour of the system and first run

### The containers

The status pills at the top of the UI show the live state: Envoy up, OPA up, Envoy mode `fail-closed`, which
policies are loaded (none yet), and the model.

| Container | What it is |
|---|---|
| `assistant` | Your AI application: the web UI plus the Groq LLM agent. It proposes tool calls. |
| `bank-api` | The bank's backend that actually does lookups, refunds and transfers. |
| `envoy` | A proxy in front of the bank API. Every tool call passes through it. |
| `opa` | The policy engine. It holds your Rego rules and answers "allow or deny?". |
| `control` | Helper for the class: loads your `.rego` files into OPA, manages Envoy's config (fail-open switch) and collects OPA's decision log for the Audit tab. |


### First run with zero policy

Click **⭐ ₹75,000 transfer, no approval**.

**Do trial with the failue_mode=true and faliure_mode=false**

---

## From Envoy to the AI-agent tool policy

We'll create two policy files:

| File | Package | Role |
|---|---|---|
| `policies/envoy_authz.rego` | `envoy.authz` | **Entrypoint / adapter**: understands Envoy's HTTP input and answers Envoy |
| `policies/tools.rego` | `nexabank.tools` | **Business policy**: the bank's rules for AI tool calls, with no HTTP knowledge |

After every save, glance at the top of the UI: the **Policies** pill lists the file, or a red banner shows OPA's
error. Then click the scenarios named in each step on the **Assistant** tab.

### Step 1: Hello Envoy (simple allow / deny)

**Concept:** `package`, `default`, a rule, Envoy's input, the plugin's `result` contract.

Create `policies/envoy_authz.rego`:

```rego
package envoy.authz

default allow := false

allow if input.attributes.request.http.path == "/tools/lookup_customer"

result := {"allowed": allow}
```

Save. Within ~2 s the **Policies** pill shows `envoy_authz.rego`, and in the **Policies** tab it's `LOADED`.

Make a typo on purpose: add a line `allow if {` at the end of the file and save. The red banner shows OPA's
`rego_parse_error` with the line number. Delete the line and save again: the banner disappears.

**Click**
- **Customer lookup (support)** → Envoy **ALLOW**, bank API **executed**, and the profile comes back.
- **⭐ ₹75,000 transfer, no approval** → Envoy **DENY**: *Envoy returned 403 without reasons*. Expand *HTTP
  request the assistant sent through Envoy*: that's the tool call on the wire.


### Step 2: The adapter, from Envoy's HTTP view to a business request

Replace `policies/envoy_authz.rego` with:

```rego
package envoy.authz

import data.nexabank

http := input.attributes.request.http

# adapter: Envoy's HTTP request -> NexaBank business request
tool_request := object.union(json.unmarshal(http.headers["x-nexabank-context"]), {
	"tool": input.parsed_path[1],
	"args": input.parsed_body,
})

default allow := false

allow if nexabank.tools.allow with input as tool_request

result := {"allowed": allow}
```

Then create the **business policy** `policies/tools.rego`:

```rego
package nexabank.tools

default allow := false

allow if input.tool == "lookup_customer"
```

**Click:** **Customer lookup (support)** → ALLOW. **Small transfer ₹5,000** → DENY. Same behaviour as Step 1, but
the rule now lives in the business policy.

**Step 1: the rule is inside envoy_authz.rego**


allow if input.attributes.request.http.path == "/tools/lookup_customer"
The Envoy file decides by itself, by looking at the raw HTTP path.

**Step 2: the rule moves to tools.rego**

envoy_authz.rego no longer decides. It only translates the request and asks:

---

### Step 3: Department / role policy


**3a. Hard-coded first.** Replace `policies/tools.rego` with:

```rego
package nexabank.tools

default allow := false

allow if input.tool == "lookup_customer"

allow if {
	input.tool == "transfer_money"
	input.user.role == "payments_officer"
	input.user.department == "payments"
}
```

**Click:** **Intern asks for a transfer** → DENY. **Small transfer ₹5,000** (Anita) → ALLOW, **executed**.

Two `allow` rules = **OR**. Three lines in one body = **AND**. But the role matrix is hard-coded, and it belongs to
the compliance team, not to developers…

**3b. Data-driven.** Show `data.org.tool_permissions` in `policies/org/data.json`:

```json
"tool_permissions": {
  "lookup_customer": { "roles": ["payments_officer", "support_executive", "intern"], "departments": ["payments", "customer_support"] },
  "issue_refund":    { "roles": ["support_executive", "payments_officer"],           "departments": ["payments", "customer_support"] },
  "transfer_money":  { "roles": ["payments_officer"],                                "departments": ["payments"] }
}
```

Replace `policies/tools.rego` with one generic rule:

```rego
package nexabank.tools

default allow := false

allow if {
	perm := data.org.tool_permissions[input.tool]
	input.user.role in perm.roles
	input.user.department in perm.departments
}
```

**Click:** **Refund ₹3,000 (support)** → ALLOW. **Intern asks for a transfer** → DENY.

### 💥 Now the problem statement

Click **⭐ ₹75,000 transfer, no approval**.

**→ ALLOW. Envoy forwards it, the bank API executes it. ₹75,000 left Priya's account for a never-seen-before beneficiary.**

Pause here. This is the moment of the lesson:

> Anita *is* a payments officer. The Payment Agent *is* authorized to use `transfer_money`. Every identity and
> permission check passed. And still, this should never have happened without a manager's sign-off.
> **Authorization ≠ policy compliance.** Let's write the organization's actual rules.

---

### Step 4: Structured decisions with denial reasons

A bare 403 tells the user, the auditor and the LLM nothing. The idiomatic Rego pattern is to collect every violated
rule into a **set of reasons** and allow only when that set is empty.

Replace `policies/tools.rego` with:

```rego
package nexabank.tools

default allow := false

allow if {
	role_permitted
	count(deny) == 0
}

reasons := deny

# helper rule: a named, reusable condition
role_permitted if {
	perm := data.org.tool_permissions[input.tool]
	input.user.role in perm.roles
	input.user.department in perm.departments
}

deny contains msg if {
	not role_permitted
	msg := sprintf("role %q in department %q may not use %s", [input.user.role, input.user.department, input.tool])
}
```

Now teach the entrypoint to **send the reasons back through Envoy**. Replace `policies/envoy_authz.rego` with
its final version:

```rego
package envoy.authz

import data.nexabank

http := input.attributes.request.http

# adapter: Envoy's HTTP request -> NexaBank business request
tool_request := object.union(json.unmarshal(http.headers["x-nexabank-context"]), {
	"tool": input.parsed_path[1],
	"args": input.parsed_body,
})

default allow := false

allow if nexabank.tools.allow with input as tool_request

default reasons := []

reasons := r if {
	r := nexabank.tools.reasons with input as tool_request
}

# the answer Envoy enforces; when denied, Envoy returns http_status, headers and body to the caller
result := {
	"allowed": allow,
	"http_status": 403,
	"headers": {"content-type": "application/json"},
	"body": json.marshal({"allowed": allow, "reasons": reasons}),
}
```


**Click:** **Intern asks for a transfer** → DENY with `role "intern" in department "payments" may not use transfer_money`.
Expand *Envoy's 403 response (body written by OPA)*: that JSON came from your `json.marshal`.

---

### Step 5: Transaction amount threshold

**Concept:** comparisons on input, thresholds from `data`, a package-level constant.

Add to the bottom of `tools.rego`:

```rego
threshold := data.org.thresholds.approval_required_above

deny contains msg if {
	input.tool == "transfer_money"
	input.args.amount > threshold
	msg := sprintf("transfers above INR %d are not allowed", [threshold])
}
```

**Click:** **⭐ ₹75,000 transfer** → **DENY** `transfers above INR 50000 are not allowed`. **Small transfer** → ALLOW.
**₹75,000 manager approved** → DENY. Too strict! The business does allow large transfers, *with approval*.


---

### Step 6: Manager approval (and the most important Rego trap)

Change the rule. Many people write it this way first, so **type it like this on purpose**:

```rego
deny contains msg if {
	input.tool == "transfer_money"
	input.args.amount > threshold
	input.manager_approved == false
	msg := sprintf("transfers above INR %d require manager approval", [threshold])
}
```

**Click:** **⭐ ₹75,000** → DENY ✅. **₹75,000 manager approved** → ALLOW ✅. Looks perfect…

Now send a request in which the approval flag is **missing** (a buggy client, an older API version, an attacker who
strips the field). The Assistant tab always sends the flag, so ask OPA directly. Any service can query the
business policy, not only Envoy.

Open the **Ask OPA** tab:
1. Rule: `data.nexabank.tools`
2. Click the preset **Step 6 · ₹75,000 without manager_approved**. Look at the JSON: there is no
   `"manager_approved"` key at all.
3. Click **⚖️ Ask OPA**.

**Result: ✓ allow = true** 😱 ₹75,000 with NO approval information is ALLOWED.

(Students can also click **↺ Last tool call from the Assistant tab** to load the real request from the ⭐ scenario,
delete the `"manager_approved": false,` line by hand, and ask again. Same result.)

**Why?** `input.manager_approved` doesn't exist → `input.manager_approved == false` is **undefined** (not false) →
the deny body fails → no reason → `allow` is true. **A missing field became a silent allow.**

Fix it with `not`:

```rego
deny contains msg if {
	input.tool == "transfer_money"
	input.args.amount > threshold
	not input.manager_approved
	msg := sprintf("transfers above INR %d require manager approval", [threshold])
}
```

`not input.manager_approved` is true when the value is `false`, missing, or `null`. Only an explicit `true` passes.
Save, wait 2 s, and click **⚖️ Ask OPA** again with the same input: **✕ allow = false** ✅, with the reason
`transfers above INR 50000 require manager approval`.

**Talking point (write it on the board):**
> In Rego, **absence is not falsehood**. Write deny conditions so that *missing data leads to deny*.

---

### Step 7: Environment restrictions

**Concept:** context beyond the request itself; helper rules that read like business language.

Business rule: *the approval requirement applies to real money (production). In dev/staging, test transfers don't
need a manager.*

Add the helper and update the rule (and its message):

```rego
is_production if input.environment == "production"

deny contains msg if {
	input.tool == "transfer_money"
	is_production
	input.args.amount > threshold
	not input.manager_approved
	msg := sprintf("transfers above INR %d in production require manager approval", [threshold])
}
```

**Click:** **₹75,000 transfer in staging** → ALLOW. **⭐ ₹75,000** (production) → DENY.

**Talking point:** the *same request* gets different decisions depending on **where** it runs. The environment is
trusted context from deployment config, never from the user or the LLM.

---

### Step 8: Risk-based policy

**Concept:** consuming signals from other systems; several reasons accumulating in one set.

The assistant's risk engine (`assess_risk()`) scores each call: `+40` for an amount ≥ 50,000, `+30` for ≥ 2,00,000,
and `+30` for a new beneficiary. A score ≥ 70 is `high`. OPA doesn't compute risk. It **decides on** risk.

Add:

```rego
deny contains "high-risk transaction requires manager approval" if {
	input.risk.level == "high"
	is_production
	not input.manager_approved
}

deny contains msg if {
	input.risk.score >= 90
	msg := sprintf("risk score %d is too high for automated execution: route to the fraud team", [input.risk.score])
}
```
- Demo

```
Employee: Anita Rao
AI agent: payment-agent
Environment: production
Manager approval: ON (important)

Message:
Transfer ₹3,00,000 from customer C-1001 to account NXB-00099999 for equipment purchase.

Click Send.
```
---

### Step 9: AI-agent tool execution policy

**Concept:** governing the **agent** as a first-class subject: inventory, tool allowlist, approved environments,
per-agent limits.

Show `data.org.agents`: owner, tools, environments and limits per agent. This is the bank's **AI agent registry**,
the policy-side view of the SPIFFE IDs from the SPIRE session.

Right now, click **Support Agent tries a transfer** → **ALLOW**! Anita may move money, so the *support* chatbot
(built for refunds) just moved money on her behalf. **Beta agent in production** → ALLOW too.

Add the helpers:

```rego
agent := data.org.agents[input.agent.id]

agent_permitted if input.tool in agent.tools
```

Make the agent check a **positive requirement** in `allow`:

```rego
allow if {
	role_permitted
	agent_permitted
	count(deny) == 0
}
```

And add the agent rules:

```rego
deny contains msg if {
	not agent
	msg := sprintf("agent %q is not registered in the AI agent inventory", [input.agent.id])
}

deny contains msg if {
	agent
	not agent_permitted
	msg := sprintf("agent %q is not allowed to use tool %s", [input.agent.id, input.tool])
}

deny contains msg if {
	agent
	not input.environment in agent.environments
	msg := sprintf("agent %q is not approved for the %s environment", [input.agent.id, input.environment])
}

deny contains msg if {
	limit := agent.limits[input.tool]
	input.args.amount > limit
	msg := sprintf("agent %q may not %s more than INR %d", [input.agent.id, input.tool, limit])
}
```

**Click**

| Scenario | Result |
|---|---|
| Support Agent tries a transfer | DENY `agent "support-agent" is not allowed to use tool transfer_money` |
| Beta agent in production | DENY `agent "beta-agent" is not approved for the production environment` |
| Refund ₹20,000 (support) | DENY `agent "support-agent" may not issue_refund more than INR 10000` |
| ₹6,00,000 transfer, approved | DENY `agent "payment-agent" may not transfer_money more than INR 500000` (even a manager can't override the agent's hard limit) |

**Talking point:** this is **least privilege for AI agents**. The employee must be allowed *and* the agent must be
allowed *and* no organizational rule may object.

---

### Step 10: Input validation and a decision object


What if the LLM sends `"amount": -5000` for a refund (a debit disguised as a refund), or forgets `amount`?
`input.args.amount > threshold` would be undefined → no deny → allowed.

Add:

```rego
money_tools := {"transfer_money", "issue_refund"}

is_money_tool if input.tool in money_tools

deny contains "amount must be a positive number" if {
	is_money_tool
	not valid_amount
}

valid_amount if {
	is_number(input.args.amount)
	input.args.amount > 0
}
```

(`money_tools` is a **set** literal; `is_number` is one of OPA's ~200 built-in functions.)

Add a **decision object**: one versioned document that says what was decided, why, and under which policy version:

```rego
decision := {
	"allow": allow,
	"reasons": reasons,
	"policy_version": "nexabank-tools-1.0",
}
```

Query it in the **Ask OPA** tab: rule `data.nexabank.tools.decision`, preset **Step 6**, **⚖️ Ask OPA**. You get
`allow`, both `reasons` and the `policy_version`. Compare with rule `data.nexabank.tools`: that returns the
**whole package**, every rule as one JSON document (`agent`, `threshold`, `role_permitted`, …).

#### The complete `policies/tools.rego`

Read it top to bottom with the class. It should read like the bank's policy handbook:

```rego
# NexaBank: AI-agent tool execution policy
# Question answered: may THIS tool call, with THESE arguments, in THIS context, execute?
package nexabank.tools

# ---------------------------------------------------------------- the decision
default allow := false

allow if {
	role_permitted # someone explicitly granted this (positive check)
	agent_permitted
	count(deny) == 0 # and no organizational rule objects
}

reasons := deny

decision := {
	"allow": allow,
	"reasons": reasons,
	"policy_version": "nexabank-tools-1.0",
}

# ---------------------------------------------------------------- helper rules
threshold := data.org.thresholds.approval_required_above

agent := data.org.agents[input.agent.id]

money_tools := {"transfer_money", "issue_refund"}

is_money_tool if input.tool in money_tools

is_production if input.environment == "production"

role_permitted if {
	perm := data.org.tool_permissions[input.tool]
	input.user.role in perm.roles
	input.user.department in perm.departments
}

agent_permitted if input.tool in agent.tools

# ---------------------------------------------------------------- deny rules (each adds a reason)

# Department / role
deny contains msg if {
	not role_permitted
	msg := sprintf("role %q in department %q may not use %s", [input.user.role, input.user.department, input.tool])
}

# Input validation: money tools need a positive numeric amount
deny contains "amount must be a positive number" if {
	is_money_tool
	not valid_amount
}

valid_amount if {
	is_number(input.args.amount)
	input.args.amount > 0
}

# Amount threshold + manager approval + environment
deny contains msg if {
	input.tool == "transfer_money"
	is_production
	input.args.amount > threshold
	not input.manager_approved
	msg := sprintf("transfers above INR %d in production require manager approval", [threshold])
}

# Risk
deny contains "high-risk transaction requires manager approval" if {
	input.risk.level == "high"
	is_production
	not input.manager_approved
}

deny contains msg if {
	input.risk.score >= 90
	msg := sprintf("risk score %d is too high for automated execution: route to the fraud team", [input.risk.score])
}

# AI agent governance
deny contains msg if {
	not agent
	msg := sprintf("agent %q is not registered in the AI agent inventory", [input.agent.id])
}

deny contains msg if {
	agent
	not agent_permitted
	msg := sprintf("agent %q is not allowed to use tool %s", [input.agent.id, input.tool])
}

deny contains msg if {
	agent
	not input.environment in agent.environments
	msg := sprintf("agent %q is not approved for the %s environment", [input.agent.id, input.environment])
}

deny contains msg if {
	limit := agent.limits[input.tool]
	input.args.amount > limit
	msg := sprintf("agent %q may not %s more than INR %d", [input.agent.id, input.tool, limit])
}
```

**Rule order doesn't matter** in Rego. It's a set of logical statements, not a script.

---

## Step 11: Fail-open vs fail-closed, live

**Concept:** what the PEP does when the PDP can't answer is itself a **policy decision**, and often the most
important one. In Envoy it's one line: `failure_mode_allow`.

| `failure_mode_allow` | When OPA is down / errors / has no decision | Risk |
|---|---|---|
| `false` (**fail-closed**) | Envoy returns 403 | availability: the assistant can't act |
| `true` (**fail-open**) | Envoy forwards the request as if allowed | **security: no governance at all, silently** |

**Demo, all in the Operations tab.** Each switch is applied to Envoy live within ~2 s (no restart). The header pills
turn red to show the current state.

1. **Simulate an OPA outage** → on. The pill says *OPA unreachable (simulated)*. Envoy's ext_authz now points at an
   address where nothing listens: an OPA crash or a network partition, as Envoy sees it.
   Assistant tab → **⭐ ₹75,000** → Envoy **DENY** with no reasons. Envoy is still **fail-closed**.
2. **Fail-open** → on. The pill says *Envoy mode fail-open*.
   Assistant tab → **⭐ ₹75,000** → everything green: **Bank API executed**. ₹75,000 moved because the policy engine
   was unreachable. Let that sink in.
3. Open the **Audit** tab: the counter *OPA unreachable* went up, and **no new decision appears in OPA's decision
   log**. Nobody decided. The transfer left no policy record at all.
4. Back in **Operations**: switch **Fail-open** off and **Simulate an OPA outage** off. Click ⭐ again → denied with
   reasons, as before.

Expand *Show the ext_authz lines Envoy is running now* while the switches are on: students see
`failure_mode_allow: true` and `cluster_name: opa_unreachable` in the live config.

**Talking points**
- Money movement and AI tool execution → **fail-closed**, always.
- Protect availability with architecture, not by failing open: run OPA as a **sidecar** next to each Envoy, health-check
  it, run replicas.
- Fail-open with *no policy loaded* behaves the same way: an empty or broken policy deployment = no governance.

---