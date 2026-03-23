# Troubleshooting Guide — K8s Runbook Agent

All errors encountered during setup, configuration, and operation — with proven fixes.

---

## Table of Contents

1. [Python & Dependencies](#1-python--dependencies)
2. [PostgreSQL](#2-postgresql)
3. [Server Startup](#3-server-startup)
4. [Slack Integration](#4-slack-integration)
5. [Grafana & Alertmanager Pipeline](#5-grafana--alertmanager-pipeline)
6. [Agent Behavior](#6-agent-behavior)
7. [Kubernetes Connectivity](#7-kubernetes-connectivity)

---

## 1. Python & Dependencies

### `pip: command not found` (pyenv)

```
pyenv: pip: command not found
The `pip' command exists in these Python versions: 3.11.14
```

**Cause:** pyenv manages Python versions and `pip` isn't linked.

**Fix:** Use `pip3` or the module form:
```bash
pip3 install -r requirements.txt
# or
python3 -m pip install -r requirements.txt
```

---

### `voyageai` package won't install (Python 3.14)

```
ERROR: Could not find a version that satisfies the requirement voyageai<1.0,>=0.3.0
Requires-Python >=3.9,<3.14
```

**Cause:** The `voyageai` package only supports Python up to 3.13. Your system has 3.14.

**Fix:** Use Python 3.12 in a virtualenv:
```bash
pyenv install 3.12.8
cd k8s_runbook_agent
pyenv local 3.12.8
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Alternative (skip Voyage):** The agent falls back to PostgreSQL full-text search (tsvector) when Voyage isn't available. Remove `voyageai` from `requirements.txt` and install the rest:
```bash
grep -v voyageai requirements.txt | pip install -r /dev/stdin
```

The agent will log `embeddings: not available — using tsvector fallback` at startup.

---

### `ModuleNotFoundError: No module named 'k8s_runbook_agent'`

```
ModuleNotFoundError: No module named 'k8s_runbook_agent'
```

**Cause:** Running uvicorn from inside the `k8s_runbook_agent/` directory instead of its parent.

**Fix:** Run from the parent directory:
```bash
cd /path/to/claude-agent-building    # NOT k8s_runbook_agent/
uvicorn k8s_runbook_agent.server:app --host 0.0.0.0 --port 8080 --reload
```

**Why:** Python needs to see `k8s_runbook_agent` as a package. When you're inside it, Python can't find the parent package.

---

### Virtualenv: "Do I need to activate it every time?"

**Yes.** Every new terminal window needs:
```bash
cd k8s_runbook_agent
source venv/bin/activate
```

The venv doesn't affect agent behavior — it just isolates Python dependencies from your system. If you forget to activate, you'll get `ModuleNotFoundError` for packages like `fastapi`, `anthropic`, etc.

---

## 2. PostgreSQL

### `role "agent" does not exist`

```
asyncpg.exceptions.InvalidAuthorizationSpecificationError: role "agent" does not exist
```

**Cause:** You have a **local PostgreSQL** running on port 5432 that intercepts the connection before Docker's pgvector container.

**Diagnosis:**
```bash
lsof -i :5432 -P -n
# If you see BOTH postgres and com.docker, that's the conflict
```

**Fix:** Run pgvector on a different port:
```bash
docker stop pg-runbook && docker rm pg-runbook
docker run -d --name pg-runbook \
  -e POSTGRES_USER=agent \
  -e POSTGRES_PASSWORD=agent \
  -e POSTGRES_DB=k8s_agent \
  -p 5433:5432 \
  pgvector/pgvector:pg16
```

Update `.env`:
```bash
DATABASE_URL=postgresql://agent:agent@localhost:5433/k8s_agent
```

---

### `postgresql: not_configured` in /ready

**Cause:** `DATABASE_URL` is empty or not set in `.env`.

**Fix:**
```bash
# If using Docker pgvector on port 5433:
DATABASE_URL=postgresql://agent:agent@localhost:5433/k8s_agent

# If using Docker pgvector on port 5432 (no local PG conflict):
DATABASE_URL=postgresql://agent:agent@localhost:5432/k8s_agent
```

Verify the container is running:
```bash
docker ps | grep pg-runbook
# If stopped:
docker start pg-runbook
```

---

## 3. Server Startup

### `Address already in use` (port 8080 or 8090)

```
ERROR: [Errno 48] Address already in use
```

**Cause:** Another process is using the port.

**Fix:**
```bash
# Kill whatever is on the port
lsof -ti:8080 | xargs kill -9 2>/dev/null

# Or use a different port
uvicorn k8s_runbook_agent.server:app --host 0.0.0.0 --port 8090 --reload
```

---

### `NameError: name '_NoOpTracer' is not defined`

```
File "observability/tracing.py", line 80, in <module>
    tracer = get_tracer()
NameError: name '_NoOpTracer' is not defined
```

**Cause:** The `tracer = get_tracer()` line was placed before the `_NoOpTracer` class definition.

**Fix:** Ensure this line in `observability/tracing.py` appears AFTER the `_NoOpTracer` class:
```python
class _NoOpTracer:
    """Dummy tracer that returns no-op spans."""
    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()
    def start_span(self, name, **kwargs):
        return _NoOpSpan()

# Module-level tracer instance — must be after _NoOpTracer definition
tracer = get_tracer()
```

---

### `Extra inputs are not permitted` (pydantic validation)

```
pydantic_core._pydantic_core.ValidationError: 7 validation errors for Settings
production_mode
  Extra inputs are not permitted [type=extra_forbidden]
max_payload_bytes
  Extra inputs are not permitted [type=extra_forbidden]
```

**Cause:** Your `.env` file has variables that aren't defined in `config.py`'s `Settings` class.

**Fix:** Add the missing fields to `config.py`:
```python
class Settings(BaseSettings):
    # ... existing fields ...

    # Security hardening (production)
    production_mode: bool = False
    admin_api_key: str = ""
    max_payload_bytes: int = 1048576
    max_concurrent_sessions: int = 50

    # Data retention
    session_retention_days: int = 30
    audit_retention_days: int = 90
    memory_retention_days: int = 365
    in_memory_eviction_hours: int = 1
```

Or remove the offending lines from `.env` if you don't need them for local dev.

---

## 4. Slack Integration

### Bot can post but messages don't appear in channel

```
slack_sdk.errors.SlackApiError: not_in_channel
```

**Cause:** The Slack bot hasn't been invited to the target channel.

**Fix:** In the Slack channel, type:
```
/invite @k8s_runbook_agent
```

Then test with a fresh alert.

---

### "User @rohan.gupta is not authorized to approve fixes"

**Cause:** The `APPROVAL_ALLOWED_USERS` in `.env` contains placeholder IDs (`U123,U456`) that don't match your real Slack user ID.

**Fix — Option A (open mode for local dev):** Clear the allowlists:
```bash
APPROVAL_ALLOWED_USERS=
APPROVAL_ALLOWED_GROUPS=
APPROVAL_SENIOR_USERS=
```

**Fix — Option B (add your user ID):** Find your Slack user ID:
1. Click your profile picture in Slack
2. Click **Profile**
3. Click **⋮** (more) → **Copy member ID**
4. Set it in `.env`:
```bash
APPROVAL_ALLOWED_USERS=U05XXXXXXXX
```

Restart the agent after changing `.env`.

---

### Where to find Slack credentials

| Variable | Where to find it |
|----------|-----------------|
| `SLACK_BOT_TOKEN` | Slack App → **OAuth & Permissions** → Bot User OAuth Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Slack App → **Basic Information** → App Credentials → Signing Secret |
| `SLACK_CHANNEL_ID` | In Slack: right-click channel → **View channel details** → scroll to bottom → Channel ID (starts with `C`) |

All three are required. The bot token sends messages, the signing secret verifies incoming button clicks, and the channel ID tells the agent where to post.

---

### Approve/Reject buttons don't respond (local dev)

**Cause:** Slack sends button clicks to the Interactivity URL, which must be a public HTTPS URL. `localhost` doesn't work.

**Fix:** Use ngrok:
```bash
brew install ngrok
ngrok http 8080   # or 8090 if using that port
# Note the https URL: https://abc123.ngrok-free.app
```

In Slack App settings:
1. **Interactivity & Shortcuts** → Request URL: `https://abc123.ngrok-free.app/slack/interactions`
2. **Slash Commands** → `/k8s-diag`: `https://abc123.ngrok-free.app/slack/commands`

---

## 5. Grafana & Alertmanager Pipeline

### Alert fires via curl but not from Prometheus

**Symptom:** `curl -X POST .../webhooks/grafana` triggers the agent, but deploying a crashing pod doesn't.

**Cause:** The alert pipeline has multiple stages, and any stage can be the bottleneck:

```
Pod crashes → Prometheus scrapes (30s) → Alert rule evaluates → for: duration (15m default!)
→ Alertmanager groups (30s) → Webhook fires to agent
```

**Debug step by step:**

```bash
# 1. Is the pod crashing?
kubectl -n apps get pods

# 2. Does Prometheus see the metric?
curl -s 'http://localhost:9090/api/v1/query?query=kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}'

# 3. Is the Prometheus alert rule firing or pending?
curl -s 'http://localhost:9090/api/v1/alerts' | python3 -c "
import json, sys
for a in json.load(sys.stdin)['data']['alerts']:
    if 'CrashLoop' in a['labels'].get('alertname',''):
        print(f'{a[\"labels\"][\"alertname\"]} state={a[\"state\"]} severity={a[\"labels\"].get(\"severity\",\"?\")}')
"

# 4. Has Alertmanager received it?
curl -s 'http://localhost:9093/api/v2/alerts' | python3 -c "
import json, sys
for a in json.load(sys.stdin):
    if 'CrashLoop' in a.get('labels',{}).get('alertname',''):
        print(f'{a[\"labels\"][\"alertname\"]} receivers={[r[\"name\"] for r in a.get(\"receivers\",[])]]}')
"
```

---

### Alert stuck in "pending" for 15 minutes

**Cause:** The default `KubePodCrashLooping` rule has `for: 15m`.

**Fix:** Speed it up for testing:
```bash
kubectl -n monitoring get prometheusrule kube-prometheus-stack-kubernetes-apps -o json > /tmp/rule.json

python3 -c "
import json
with open('/tmp/rule.json') as f:
    rule = json.load(f)
for group in rule['spec']['groups']:
    for r in group['rules']:
        if r.get('alert') == 'KubePodCrashLooping':
            r['for'] = '1m'
            print('Changed to 1m')
with open('/tmp/rule.json', 'w') as f:
    json.dump(rule, f)
"

kubectl apply -f /tmp/rule.json
```

Delete and recreate the crash pod to reset the pending timer:
```bash
kubectl -n apps delete pod crash-test --force
kubectl -n apps run crash-test --image=busybox --restart=Always -- sh -c 'exit 1'
```

---

### Alert goes to `slack-default` instead of `slack-and-agent`

**Cause:** The Alertmanager routing rule only sends `severity=critical` to the agent webhook. Most built-in K8s alert rules use `severity=warning`.

**Fix:** Update `prom-values.yaml` to route warnings to the agent too:
```yaml
routes:
  - matchers:
      - severity = critical
    receiver: slack-and-agent
    group_wait: 10s
  - matchers:
      - severity = warning
    receiver: slack-and-agent    # Changed from slack-default
    group_wait: 10s
```

Apply and restart:
```bash
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --values prom-values.yaml --wait

kubectl -n monitoring rollout restart statefulset alertmanager-kube-prometheus-stack-alertmanager
```

Re-establish port-forward after restart:
```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093 &
```

---

### Webhook URL wrong in Alertmanager config

**Symptom:** Alertmanager shows `url: <secret>` for the webhook but the agent never receives alerts.

**Check the actual URL:**
```bash
kubectl -n monitoring get secret alertmanager-kube-prometheus-stack-alertmanager \
  -o jsonpath='{.data.alertmanager\.yaml}' | base64 -d | grep -A 3 "webhook_configs"
```

**Fix:** The URL must be `http://host.docker.internal:PORT/webhooks/grafana` (where PORT is your agent's port). Update in `prom-values.yaml`:
```yaml
webhook_configs:
  - url: 'http://host.docker.internal:8090/webhooks/grafana'
    send_resolved: false
```

---

### "Authorization Header" not visible in Grafana Contact Point

**Where it is:** When creating/editing a webhook contact point:
1. Click **"Optional Webhook settings"** (expandable section)
2. Inside: set **Authorization Header Type** = `Bearer` and **Credentials** = your secret

**Alternative:** Use **Add custom header** instead:
- Header name: `Authorization`
- Header value: `Bearer your-secret-here`

**Simplest for local dev:** Leave `GRAFANA_WEBHOOK_SECRET` empty in `.env`. The agent accepts all webhooks without auth in dev mode.

---

### `GRAFANA_WEBHOOK_SECRET` — where does this value come from?

**You create it yourself.** It's any random string shared between Grafana and the agent:
```bash
openssl rand -hex 20
# Example: a3f8b2c1d4e5f6789012345678abcdef01234567
```

Set the **same value** in:
1. `.env`: `GRAFANA_WEBHOOK_SECRET=a3f8b2c1d4e5f6789012345678abcdef01234567`
2. Grafana Contact Point → Authorization Header: `Bearer a3f8b2c1d4e5f6789012345678abcdef01234567`

---

### `host.docker.internal` not resolving

**Applies to:** macOS with Docker Desktop only.

**Fix:** Ensure Docker Desktop is running. `host.docker.internal` is a Docker Desktop feature that resolves to the host machine's IP from inside containers.

**Verify:**
```bash
docker exec local-lab-control-plane curl -s http://host.docker.internal:8090/health
# Should return: {"status":"ok"}
```

---

## 6. Agent Behavior

### "Execution blocked by guardrails: Fix requires human-provided values"

```
BLOCKED — fix cannot be applied:
• Fix requires human-provided values: Confirmation of intended behavior...
```

**This is correct behavior.** The agent detected the fix needs a human decision (e.g., what command to use, what value to set) and refused to auto-execute.

**What to do:**
- For test crash pods: this is expected — the agent correctly identified it's a test pod
- For real issues: the Slack message will list what values are needed. Provide them and re-approve
- To test the full approve → execute flow, use scenarios with deterministic fixes (e.g., OOMKilled where the fix is "increase memory limit")

---

### Agent correctly escalates instead of fixing

**Examples of correct escalation:**
- `NodeClockNotSynchronising` → "host-level NTP issue, requires OS access"
- `AlertmanagerFailedToSendAlerts` → "missing service dependency requires human decision"

These are **not bugs** — the agent correctly identified issues it can't fix via the K8s API and escalated them for human handling.

---

### Agent creates session but no diagnosis

**Cause:** `ANTHROPIC_API_KEY` is invalid or has insufficient credits.

**Check:**
```bash
curl -s http://localhost:8090/ready | python3 -c "
import json, sys
data = json.load(sys.stdin)
print('Anthropic:', data['checks'].get('anthropic_api_key', 'missing'))
"
```

---

### `embeddings: error` in /ready

**Cause:** Voyage AI package not installed or API key issue.

**This is non-critical.** The agent falls back to PostgreSQL tsvector full-text search for incident memory. Semantic search (Voyage) is better but keyword search works fine.

---

### Too many sessions from Alertmanager flood

**Symptom:** Agent creates 10+ sessions simultaneously when Alertmanager sends a batch of alerts.

**Cause:** When Alertmanager config changes, it re-sends all currently firing alerts.

**Mitigation:** The agent deduplicates by fingerprint — the same alert won't create duplicate sessions. Different alerts (NodeClock, AlertmanagerFailed, etc.) each get their own session.

**Cost concern:** Each session uses ~20K-100K tokens. To limit: set `MAX_TOKENS_PER_SESSION=50000` in `.env`.

---

## 7. Kubernetes Connectivity

### Agent can't reach K8s API

**Check:**
```bash
kubectl config current-context
# Should show: kind-local-lab
```

If using kind, the kubeconfig is auto-configured. If it shows a different context:
```bash
kubectl config use-context kind-local-lab
```

---

### `metrics not available` from tools

**Cause:** metrics-server not installed in the cluster.

**Check:**
```bash
kubectl -n kube-system get deployment metrics-server
kubectl top nodes
```

If missing, the lab guide includes installation steps. The agent still works without metrics — it just can't check CPU/memory usage.

---

## Quick Diagnostic Commands

```bash
# Full system check
curl -s http://localhost:8090/ready | python3 -m json.tool

# Is the agent processing?
curl -s http://localhost:8090/sessions | python3 -m json.tool

# Prometheus alert state
curl -s 'http://localhost:9090/api/v1/alerts' | python3 -c "
import json,sys
for a in json.load(sys.stdin)['data']['alerts']:
    print(f\"{a['labels'].get('alertname','?'):40} state={a['state']:8} sev={a['labels'].get('severity','?')}\")"

# Alertmanager routing
curl -s 'http://localhost:9093/api/v2/alerts' | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    print(f\"{a['labels'].get('alertname','?'):40} → {[r['name'] for r in a.get('receivers',[])]}\") "

# Slack connectivity
curl -s http://localhost:8090/ready | python3 -c "
import json,sys; print('Slack:', json.load(sys.stdin)['checks'].get('slack_token','missing'))"

# Kill all port-forwards
kill $(lsof -ti:3000,8080,8090,9090,9093) 2>/dev/null
```
