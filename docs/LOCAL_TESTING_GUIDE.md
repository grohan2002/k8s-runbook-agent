# Local Testing Guide — K8s Runbook Agent with kind + Grafana + Slack

This guide walks you through running the K8s Runbook Agent on your Mac against the local kind cluster from the **Local Kubernetes Lab** guide (kind + Prometheus + Grafana).

## Prerequisites

You should already have from the lab guide:

| Component | Where | How to Verify |
|-----------|-------|--------------|
| kind cluster (`local-lab`) | Docker Desktop | `kind get clusters` → `local-lab` |
| 3 nodes (1 CP + 2 workers) | kind | `kubectl get nodes` → 3 Ready |
| Prometheus | `monitoring` namespace | `kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus` |
| Grafana | `monitoring` namespace, port 3000 | `open http://localhost:3000` (admin / local-lab-admin) |
| Alertmanager | `monitoring` namespace, port 9093 | `open http://localhost:9093` |
| metrics-server | `kube-system` namespace | `kubectl top nodes` shows CPU/memory |
| Slack workspace | Your workspace | Incoming Webhook URL |

If any of the above are missing, run through the lab guide first.

---

## Step 1: Start Port-Forwards (if not already running)

```bash
# Kill any stale forwards
kill $(lsof -ti:3000,9090,9093) 2>/dev/null || true

# Open fresh forwards
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana      3000:80   &
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus   9090:9090 &
kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093 &
```

Verify:
- Grafana: http://localhost:3000 (admin / local-lab-admin)
- Prometheus: http://localhost:9090
- Alertmanager: http://localhost:9093

---

## Step 2: Set Up PostgreSQL with pgvector

The agent needs PostgreSQL for session persistence and incident memory.

```bash
# Start pgvector (skip if already running)
docker run -d --name pg-runbook \
  -e POSTGRES_USER=agent \
  -e POSTGRES_PASSWORD=agent \
  -e POSTGRES_DB=k8s_agent \
  -p 5432:5432 \
  pgvector/pgvector:pg16

# Verify
docker ps | grep pg-runbook
```

---

## Step 3: Configure the Agent

```bash
cd k8s_runbook_agent
cp .env.example .env
```

Edit `.env` with your actual values:

```bash
# ===== REQUIRED =====
ANTHROPIC_API_KEY=sk-ant-your-real-key-here

# ===== POSTGRESQL =====
DATABASE_URL=postgresql://agent:agent@localhost:5432/k8s_agent

# ===== KUBERNETES =====
# kind sets this automatically — verify with: kubectl config current-context
# Should show: kind-local-lab
KUBECONFIG=

# ===== SLACK (from your workspace) =====
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_CHANNEL_ID=C0123456789

# ===== GRAFANA WEBHOOK =====
# Choose any secret — you'll set the same value in Grafana
GRAFANA_WEBHOOK_SECRET=my-local-lab-secret

# ===== AGENT BEHAVIOR (local dev) =====
DRY_RUN_DEFAULT=true
LOG_LEVEL=DEBUG
MULTI_AGENT_ENABLED=false
```

---

## Step 4: Set Up Python virtualenv, Install, and Start

> **Important:** The `voyageai` package requires Python <=3.13. If you have Python 3.14, use pyenv to install 3.12 first.

```bash
# Install Python 3.12 (if not already available)
pyenv install 3.12.8

# Create virtualenv with Python 3.12
cd k8s_runbook_agent
pyenv local 3.12.8
python3 -m venv venv
source venv/bin/activate

# Verify
python3 --version   # Should show 3.12.x

# Install dependencies
pip install -r requirements.txt

# Start (runs on port 8080)
make run
# or: uvicorn k8s_runbook_agent.server:app --host 0.0.0.0 --port 8080 --reload
```

> **Every new terminal:** run `source venv/bin/activate` before starting the agent. The venv doesn't affect behavior — just isolates dependencies.

You should see startup logs:

```
K8s Runbook Agent starting...
  Dry-run default: True
  Loaded 16 diagnostic runbooks
  PostgreSQL session store initialized
  Embeddings: Voyage AI initialized (model=voyage-3, dims=1024)
  Escalation timer: started
  Runbook hot-reload: watching knowledge/runbooks (every 30s)
```

Verify:

```bash
# Health check
curl http://localhost:8080/health
# {"status": "ok"}

# Deep readiness — K8s, PG, embeddings all OK
curl http://localhost:8080/ready | python3 -m json.tool
# Should show: kubernetes=ok, postgresql=ok
```

---

## Step 5: Point Grafana Alerts at the Agent

The agent runs on your Mac at `localhost:8080`. Grafana runs inside kind. To make Grafana reach the agent, you need the host's IP as seen from inside Docker.

### 5a: Find your host IP from inside kind

```bash
# On macOS with Docker Desktop, this always works:
HOST_IP="host.docker.internal"

# Verify kind can reach your agent:
docker exec local-lab-control-plane curl -s http://host.docker.internal:8080/health
# Should return: {"status":"ok"}
```

### 5b: Add the Agent as a Grafana Contact Point (UI)

1. Open http://localhost:3000 → sign in (admin / local-lab-admin)
2. Left sidebar → **Alerting** (bell icon) → **Contact points**
3. Click **+ Add contact point**

| Field | Value |
|-------|-------|
| Name | `runbook-agent` |
| Integration | **Webhook** |
| URL | `http://host.docker.internal:8080/webhooks/grafana` |
| HTTP Method | `POST` |
| Authorization Header | `Bearer my-local-lab-secret` |

4. Click **Test** → your agent terminal should log the incoming payload
5. Click **Save contact point**

### 5c: Add Slack as a Contact Point (UI)

1. Still in **Contact points**, click **+ Add contact point**

| Field | Value |
|-------|-------|
| Name | `k8s-slack` |
| Integration | **Slack** |
| Webhook URL | Your Slack Incoming Webhook URL |
| Channel | `#k8s-alerts` |
| Username | `Grafana Alerts` |

2. Click **Test** → check your Slack channel
3. Click **Save contact point**

### 5d: Configure Notification Policy

1. Go to **Alerting** → **Notification policies**
2. Edit the **Default policy** → set contact point to `k8s-slack`
3. Click **+ Add nested policy**:
   - Matching label: `severity = critical`
   - Contact point: `runbook-agent`
   - Check **Continue matching subsequent sibling nodes** (so Slack also fires)
4. **Save**

### 5e: Alternative — Alertmanager Webhook (for Prometheus alerts)

If you want alerts to route through Alertmanager (the lab guide's default path), update `prom-values.yaml`:

```yaml
# Add to receivers in alertmanager.config:
- name: slack-and-agent
  slack_configs:
    - channel: '#k8s-critical'
      send_resolved: true
  webhook_configs:
    - url: 'http://host.docker.internal:8080/webhooks/grafana'
      send_resolved: false
      http_config:
        bearer_token: 'my-local-lab-secret'
```

Then upgrade Helm:

```bash
helm upgrade kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values prom-values.yaml --wait
```

---

## Step 6: Configure Slack for Agent Responses

The agent needs to POST back to Slack with diagnoses and approval buttons. This uses the **Slack Bot Token** (different from Incoming Webhooks).

### If you haven't created a Slack App yet:

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name: `K8s Runbook Agent`, select your workspace
3. **OAuth & Permissions** → add scopes: `chat:write`, `commands`
4. **Install to Workspace** → copy **Bot User OAuth Token** (`xoxb-...`)
5. Copy **Signing Secret** from **Basic Information** page

### Configure Interactivity (for Approve/Reject buttons):

For local testing, you need a public URL. Use **ngrok**:

```bash
# Install ngrok
brew install ngrok

# Tunnel to your local agent
ngrok http 8080
# Note the https URL: https://abc123.ngrok-free.app
```

Then in Slack App settings:
1. **Interactivity & Shortcuts** → Enable → Request URL:
   ```
   https://abc123.ngrok-free.app/slack/interactions
   ```
2. **Slash Commands** → Create `/k8s-diag`:
   ```
   https://abc123.ngrok-free.app/slack/commands
   ```

Update your `.env`:

```bash
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-secret
SLACK_CHANNEL_ID=C0123456789   # Right-click channel → View channel details → copy ID
```

Restart the agent (`make run`).

---

## Step 7: Trigger a Real Alert — End-to-End Test

### 7a: Create a crashing pod (CrashLoopBackOff)

```bash
kubectl -n apps run crash-test \
  --image=busybox \
  --restart=Always \
  -- sh -c 'echo Crashing; exit 1'

# Watch it crash
kubectl -n apps get pod crash-test -w
```

### 7b: Speed up the alert (optional)

The default KubePodCrashLooping rule waits 15 minutes. Speed it up:

```bash
kubectl -n monitoring edit prometheusrule kube-prometheus-stack-kubernetes-apps
# Find KubePodCrashLooping rule, change: for: 15m → for: 1m
```

### 7c: Watch the pipeline fire

```
Prometheus detects crash loop restarts
    ↓ (1-5 minutes)
Alertmanager groups the alert
    ↓
Grafana/Alertmanager fires webhook to agent
    ↓
Agent receives alert at /webhooks/grafana
    ↓
Agent matches runbook: pod-crashloopbackoff
    ↓
Agent calls Claude → investigates with K8s tools:
    get_pod_status → get_pod_logs → get_events → ...
    ↓
Agent posts diagnosis to Slack:
    "Root cause: Container exits with code 1 on every start"
    [Approve Fix] [Reject] [View Details]
```

### 7d: Monitor agent logs

```bash
# In another terminal — watch the agent's investigation
# The DEBUG log level shows every tool call
tail -f the uvicorn output
```

### 7e: Check everything worked

```bash
# Agent sessions
curl http://localhost:8080/sessions | python3 -m json.tool

# Prometheus alerts
open http://localhost:9090/alerts

# Alertmanager
open http://localhost:9093/#/alerts

# Slack — check your channel for the diagnosis message
```

### 7f: Clean up

```bash
kubectl -n apps delete pod crash-test
# Alertmanager sends RESOLVED to Slack automatically
```

---

## Step 8: Test Without Waiting for Prometheus (Direct Webhook)

Skip the 5-minute Prometheus alert cycle by sending a webhook directly:

```bash
# Simulate a KubePodCrashLooping alert
curl -X POST http://localhost:8080/webhooks/grafana \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-local-lab-secret" \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "KubePodCrashLooping",
        "namespace": "apps",
        "pod": "crash-test",
        "severity": "critical",
        "container": "crash-test"
      },
      "annotations": {
        "summary": "Pod apps/crash-test is crash looping",
        "description": "Pod has restarted 5 times in the last 10 minutes"
      },
      "fingerprint": "local-test-001"
    }]
  }'
```

The agent will immediately start investigating the actual pod in your kind cluster.

### More test scenarios:

```bash
# Simulate OOMKilled (deploy the memhog pod from the lab guide first)
curl -X POST http://localhost:8080/webhooks/grafana \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-local-lab-secret" \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "KubePodOOMKilled",
        "namespace": "apps",
        "pod": "memhog",
        "severity": "critical"
      },
      "annotations": {
        "summary": "Pod apps/memhog was OOMKilled"
      },
      "fingerprint": "local-test-002"
    }]
  }'

# Simulate Node NotReady
curl -X POST http://localhost:8080/webhooks/grafana \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-local-lab-secret" \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "KubeNodeNotReady",
        "node": "local-lab-worker",
        "severity": "critical"
      },
      "annotations": {
        "summary": "Node local-lab-worker is NotReady"
      },
      "fingerprint": "local-test-003"
    }]
  }'

# Simulate DNS failure
curl -X POST http://localhost:8080/webhooks/grafana \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-local-lab-secret" \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "CoreDNSDown",
        "namespace": "kube-system",
        "severity": "critical"
      },
      "annotations": {
        "summary": "CoreDNS has no ready endpoints"
      },
      "fingerprint": "local-test-004"
    }]
  }'
```

---

## Step 9: Test Multi-Agent Mode (Optional)

Switch to multi-agent to see triage → specialist routing:

```bash
# Update .env
MULTI_AGENT_ENABLED=true

# Restart agent
make run
```

Send the same test alerts — watch the logs for:

```
Triage: KubePodCrashLooping → pod (confidence=high, source=triage)
Session diag-xxx [pod]: round 1/25 (1 messages)
Session diag-xxx [pod]: tool get_pod_status
...
```

Check per-agent health:

```bash
curl http://localhost:8080/ready/agents | python3 -m json.tool
```

---

## Step 10: Import the Agent's Grafana Dashboard

```bash
# The agent ships a 14-panel dashboard
curl -X POST http://localhost:3000/api/dashboards/db \
  -H "Content-Type: application/json" \
  -u admin:local-lab-admin \
  -d "{\"dashboard\": $(cat deploy/grafana/dashboard.json), \"overwrite\": true}"
```

Open http://localhost:3000 → Dashboards → **K8s Runbook Agent** to see alerts received, diagnoses, tool calls, and token usage.

---

## Quick Reference

| What | Command |
|------|---------|
| Start agent | `make run` |
| Agent health | `curl http://localhost:8080/health` |
| Agent readiness | `curl http://localhost:8080/ready` |
| Agent sessions | `curl http://localhost:8080/sessions \| python3 -m json.tool` |
| Send test alert | `make alert` |
| Grafana | http://localhost:3000 (admin / local-lab-admin) |
| Prometheus | http://localhost:9090 |
| Alertmanager | http://localhost:9093 |
| Agent logs | watch the uvicorn terminal |
| Slack commands | `/k8s-diag status` in Slack |
| Kill port-forwards | `kill $(lsof -ti:3000,8080,9090,9093)` |
| Deploy crash pod | `kubectl -n apps run crash-test --image=busybox --restart=Always -- sh -c 'exit 1'` |
| Clean up crash pod | `kubectl -n apps delete pod crash-test` |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Agent can't reach K8s API | Verify `kubectl config current-context` is `kind-local-lab` |
| Grafana webhook returns 401 | Check `Authorization: Bearer` matches `GRAFANA_WEBHOOK_SECRET` in `.env` |
| Slack buttons don't work | Ensure ngrok is running and Interactivity URL is set in Slack App |
| Agent shows `embeddings: error` | Normal if no Voyage API — falls back to tsvector search |
| `postgresql: not_configured` | Start the pgvector container: `docker start pg-runbook` |
| Prometheus alert doesn't fire | Speed up: edit the rule's `for:` duration to `1m` |
| Agent creates session but no diagnosis | Check `ANTHROPIC_API_KEY` is valid in `.env` |
| `host.docker.internal` not resolving | macOS Docker Desktop specific — ensure Docker Desktop is running |
