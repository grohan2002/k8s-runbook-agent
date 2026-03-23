# K8s Runbook Agent

AI-powered Kubernetes troubleshooting agent that receives Grafana alerts, diagnoses issues using Claude, proposes fixes for human approval via Slack, and executes approved remediations. Learns from past incidents via pgvector RAG. Supports single-agent and multi-agent modes.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Single-Agent vs Multi-Agent](#single-agent-vs-multi-agent)
- [Quick Start (Local)](#quick-start-local-development)
- [Local Testing with kind + Grafana + Slack](docs/LOCAL_TESTING_GUIDE.md) ← **start here if you have the kind lab running**
- [Connecting Grafana (Alert Source)](#connecting-grafana-alert-source)
- [Connecting Slack (Human-in-the-Loop)](#connecting-slack-human-in-the-loop)
- [Connecting PagerDuty / OpsGenie](#connecting-pagerduty--opsgenie)
- [Production Deployment](#production-deployment)
- [Agent Health Checks](#agent-health-checks)
- [Incident Memory](#incident-memory-pgvector-rag)
- [Custom Runbooks](#custom-runbooks)
- [Safety Guardrails](#safety-guardrails)
- [API Reference](#api-endpoints)
- [Environment Variables](#environment-variables)

---

## How It Works

The agent sits idle until a Grafana alert fires, then runs a fully autonomous diagnostic pipeline:

```
 Grafana alert fires
        │
        ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │  1. RECEIVE        Webhook parses the alert, deduplicates       │
 │                    by fingerprint, correlates with active        │
 │                    incidents                                    │
 │                                                                 │
 │  2. TRIAGE         (Multi-agent only) Haiku classifies the      │
 │                    alert into a specialist domain in <2s         │
 │                                                                 │
 │  3. INVESTIGATE    Claude calls K8s inspection tools:            │
 │                    pod status, logs, events, metrics,            │
 │                    node conditions, endpoints, etc.              │
 │                                                                 │
 │  4. RECALL         Agent searches incident memory for           │
 │                    similar past incidents (pgvector RAG)         │
 │                                                                 │
 │  5. DIAGNOSE       Claude produces a structured root cause      │
 │                    with evidence and confidence level            │
 │                                                                 │
 │  6. PROPOSE FIX    Claude proposes a specific remediation       │
 │                    with dry-run preview and rollback plan        │
 │                                                                 │
 │  7. APPROVE        Slack message with Approve / Reject buttons  │
 │                    + PagerDuty/OpsGenie page if SLA breached     │
 │                                                                 │
 │  8. EXECUTE        Dry-run first, then live mutation,           │
 │                    then verify the fix worked                    │
 │                                                                 │
 │  9. LEARN          Record incident + outcome to memory          │
 │                    for future investigations                     │
 └──────────────────────────────────────────────────────────────────┘
```

The agent never executes a fix without human approval. Every mutation tool supports dry-run mode, and 8 safety guardrails validate every fix before execution.

### Does This Need an Agent Platform?

**No.** This is a self-contained FastAPI application. It does not require Vertex Agent Engine, Bedrock Agents, LangChain, or any orchestration platform. It runs as a single Docker container with:

- **PostgreSQL 14+** with pgvector (session store + incident memory)
- **Anthropic API** (Claude for diagnosis + Voyage for embeddings)
- **Slack App** (human-in-the-loop approval)
- **Grafana** (alert source)

---

## Single-Agent vs Multi-Agent

The agent supports two operating modes, controlled by a single env var:

```bash
MULTI_AGENT_ENABLED=false   # Single-agent mode (default)
MULTI_AGENT_ENABLED=true    # Multi-agent mode
```

### Single-Agent Mode (Default)

One Claude Sonnet instance handles everything with all 22 tools:

```
Alert → Sonnet (22 tools, single prompt) → Diagnosis + Fix → Slack → Execute
```

**Best for:** Getting started, low alert volume, simple environments.

### Multi-Agent Mode

A pipeline of specialized agents, each with a focused tool subset:

```
Alert → Triage (Haiku)     Fast classification, <2s, ~$0.0002
            │
            ├─ Pod Specialist (Sonnet, 11 tools)
            ├─ Network Specialist (Sonnet, 11 tools)
            ├─ Infra Specialist (Sonnet, 11 tools)
            └─ App Specialist (Sonnet, 13 tools)
                    │
                    ▼ (only for correlated multi-alert incidents)
            Coordinator (Opus)    Synthesizes across specialists
                    │
                    ▼
            Executor (Sonnet, 6 mutation tools)
```

### Why Multi-Agent Is More Accurate and Cheaper

| Metric | Single-Agent | Multi-Agent | Why |
|--------|-------------|-------------|-----|
| **Tools per call** | 22 | 11-13 | Fewer irrelevant tools = less confusion, faster convergence |
| **Tokens per alert** | ~20,000 | ~15,500 | Focused prompt + fewer tools = shorter context |
| **Avg rounds to diagnose** | 8-12 | 5-8 | Domain-specific prompt guides investigation directly |
| **Triage cost** | $0 | $0.0002 | Haiku is nearly free |
| **Diagnosis accuracy** | Good | Better | Specialist knows exactly what to look for per domain |
| **Cascading failures** | Cannot diagnose | Coordinator synthesizes | Multiple specialists + Opus synthesis |
| **Coordinator cost** | N/A | $0.20 (rare) | Only fires for ~5% of incidents |

**Cost comparison per alert:**

```
Single-agent:  ~$0.09 (Sonnet × 22 tools × 8-12 rounds)
Multi-agent:   ~$0.07 (Haiku triage + Sonnet × 11 tools × 5-8 rounds)
                       + $0.20 for coordinator (only 5% of incidents)
```

### The 4 Specialist Domains

| Domain | Handles | Tools | Runbooks |
|--------|---------|-------|----------|
| **Pod** | CrashLoop, OOM, ImagePull, Eviction, Scheduling, Jobs | get_pod_status, get_pod_logs, get_events, get_resource_usage, get_node_conditions + 6 common | 5 runbooks |
| **Network** | DNS, Ingress, Service endpoints, TLS, NetworkPolicy | get_endpoint_status, get_ingress_status, get_network_policy + 8 common | 4 runbooks |
| **Infrastructure** | Node NotReady, HPA, PVC, CPU throttling, disk/memory pressure | get_node_conditions, get_hpa_status, get_pvc_status, get_resource_usage + 7 common | 4 runbooks |
| **Application** | Error rates, deployment rollouts, latency, replica mismatch | get_pod_status, get_pod_logs, get_endpoint_status, get_hpa_status + 9 common | 3 runbooks |

### When the Coordinator Activates

The Coordinator (Opus) only runs when 2+ alerts fire for the same workload within 2 minutes. It reads all specialist diagnoses and identifies cascading failure patterns:

```
Node DiskPressure → Pod evictions → Service endpoint loss → 5xx errors
                    ^^^^^^^^^^^^    ^^^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^
                    Infra specialist  Network specialist    App specialist
                                         │
                                         ▼
                    Coordinator: "Root cause is node disk pressure.
                     The evictions and 5xx are downstream effects."
```

---

## Quick Start (Local Development)

### Prerequisites

| Dependency | Required? | Notes |
|-----------|-----------|-------|
| Python 3.12 | Yes | **Must be 3.12 — not 3.14** (voyageai doesn't support 3.14 yet) |
| Anthropic API key | Yes | [Get one here](https://console.anthropic.com/) |
| Kubernetes cluster | Yes | minikube, kind, Docker Desktop, or remote |
| PostgreSQL 14+ | Recommended | With pgvector. In-memory mode works but no persistence or incident memory |
| Slack app | Recommended | Needed for approval flow |

### Step 1: PostgreSQL with pgvector

```bash
# Docker (quickest)
docker run -d --name pg-runbook \
  -e POSTGRES_USER=agent \
  -e POSTGRES_PASSWORD=agent \
  -e POSTGRES_DB=k8s_agent \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

The agent auto-creates all tables (`sessions`, `audit_log`, `incident_memory`) on startup.

### Step 2: Set Up Python 3.12 virtualenv

> **Important:** The `voyageai` package (used for embeddings) requires Python <=3.13. If you have Python 3.14 via pyenv or system Python, you must use 3.12.

```bash
# Install Python 3.12 via pyenv (if not already installed)
pyenv install 3.12.8

# Create and activate virtualenv
cd k8s_runbook_agent
pyenv local 3.12.8
python3 -m venv venv
source venv/bin/activate

# Verify
python3 --version   # Should show 3.12.x
```

> **Does the virtualenv affect the agent run?** No — once activated, `make run` and `uvicorn` use the venv's Python automatically. Just make sure you activate it (`source venv/bin/activate`) before starting the agent in every new terminal.

Install dependencies inside the venv:

```bash
pip install -r requirements.txt

# For tests
pip install pytest pytest-asyncio httpx
```

### Step 3: Configure the .env file

```bash
cp .env.example .env
```

Now edit `.env`. Here's how to get each value:

#### Required: Anthropic API Key

```bash
# Get from https://console.anthropic.com/ → API Keys → Create Key
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

This single key powers both Claude (diagnosis) and Voyage (embeddings).

#### Required: PostgreSQL URL

If you started the pgvector container from Step 1:

```bash
DATABASE_URL=postgresql://agent:agent@localhost:5432/k8s_agent
```

#### Slack Configuration (3 values needed)

All three serve different purposes — you need all of them for the approval flow:

| Variable | What it does | Where to find it |
|----------|-------------|-----------------|
| `SLACK_BOT_TOKEN` | Sends messages (diagnoses, buttons) | Slack App → **OAuth & Permissions** → Bot User OAuth Token |
| `SLACK_SIGNING_SECRET` | Verifies incoming requests are from Slack (HMAC) | Slack App → **Basic Information** → App Credentials → Signing Secret |
| `SLACK_CHANNEL_ID` | Which channel to post alerts to | In Slack: right-click channel → **View channel details** → scroll to bottom → copy Channel ID (starts with `C`) |

**How to create the Slack App (if you haven't yet):**

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name: `K8s Runbook Agent`, select your workspace
3. **OAuth & Permissions** → add Bot Token Scopes: `chat:write`, `commands`
4. Click **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`)
5. Go back to **Basic Information** → under App Credentials → copy **Signing Secret**
6. In Slack, right-click your alerts channel → **View channel details** → copy the **Channel ID** at the bottom

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
SLACK_SIGNING_SECRET=your-signing-secret-here
SLACK_CHANNEL_ID=C0XXXXXXXXX
```

#### Grafana Webhook Secret

This is a shared secret you create yourself — any random string. Set the **same value** in both the agent `.env` and the Grafana contact point.

```bash
# Generate a random secret:
#   openssl rand -hex 20
# Or just use a simple string for local testing:
GRAFANA_WEBHOOK_SECRET=my-local-lab-secret
```

Then in Grafana → Alerting → Contact points → your webhook:
- Click **Optional Webhook settings** (expandable section)
- Set **Authorization Header Type**: `Bearer`
- Set **Authorization Header Credentials**: `my-local-lab-secret`

> **Tip:** Leave `GRAFANA_WEBHOOK_SECRET` empty during initial setup. The agent accepts all webhooks without auth when empty (dev mode). Add the secret once you've confirmed the pipeline works.

#### Local Dev Defaults

```bash
DRY_RUN_DEFAULT=true       # Safe mode — never applies live mutations
LOG_LEVEL=DEBUG            # See every tool call in logs
MULTI_AGENT_ENABLED=false  # Start with single-agent
```

#### Complete minimum `.env` for local dev:

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-your-key-here
DATABASE_URL=postgresql://agent:agent@localhost:5432/k8s_agent

# Slack (all three needed for approval flow)
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-secret
SLACK_CHANNEL_ID=C0XXXXXXXXX

# Grafana (empty = dev mode, no auth)
GRAFANA_WEBHOOK_SECRET=

# Local dev
DRY_RUN_DEFAULT=true
LOG_LEVEL=DEBUG
```

See [Environment Variables](#environment-variables) for the full list of 30+ configuration options.

### Step 4: Start

```bash
# Make sure your venv is activated first
source venv/bin/activate

make run    # Development with hot-reload
# or
uvicorn k8s_runbook_agent.server:app --host 0.0.0.0 --port 8080 --reload
```

> **Every new terminal:** run `source venv/bin/activate` before starting the agent. The venv does not affect the agent's behavior — it just isolates Python dependencies from your system Python.

### Step 5: Verify

```bash
make health              # {"status":"ok"}
make ready               # All dependency checks
make ready-agents        # Per-agent health (multi-agent mode)
make ready-agents-force  # Bypass 60s cache, re-probe all agents
```

### Step 6: Send a Test Alert

```bash
make alert
# or manually:
curl -X POST http://localhost:8080/webhooks/grafana \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "KubePodCrashLooping",
        "namespace": "default",
        "pod": "my-app-abc123",
        "severity": "critical"
      },
      "annotations": {
        "summary": "Pod default/my-app-abc123 is crash looping"
      },
      "fingerprint": "test-001"
    }]
  }'
```

### Step 7: Check Results

```bash
make sessions   # List all sessions
curl http://localhost:8080/sessions/diag-XXXX | python3 -m json.tool
curl http://localhost:8080/sessions/diag-XXXX/audit | python3 -m json.tool
```

---

## Connecting Grafana (Alert Source)

Grafana sends alerts to the agent via a webhook contact point.

### Setup

1. In Grafana: **Alerting > Contact points > New contact point**
2. Type: **Webhook**
3. URL:
   - Local: `http://localhost:8080/webhooks/grafana`
   - In-cluster: `http://k8s-runbook-agent.k8s-runbook-agent.svc/webhooks/grafana`
   - External: `https://your-domain.com/webhooks/grafana`
4. **Optional security**: Add HTTP header `Authorization: Bearer <your-secret>` and set `GRAFANA_WEBHOOK_SECRET` in the agent

### How Alerts Are Processed

```
Grafana fires alert
    │
    ▼
POST /webhooks/grafana
    │
    ├─ Parse Grafana unified alerting JSON payload
    ├─ Extract: alert_name, labels, annotations, severity, fingerprint
    ├─ Deduplicate by fingerprint (skip if already investigating)
    ├─ Correlate with active sessions (same workload = group together)
    ├─ Return 200 Accepted immediately
    │
    └─ Background task: orchestrator.investigate(alert)
```

### Supported Alert Labels

The agent uses these labels for routing and context:

| Label | Used For |
|-------|---------|
| `alertname` | Runbook matching + triage routing |
| `namespace` | K8s API scope |
| `pod` / `pod_name` | Direct pod inspection |
| `severity` | SLA escalation timing (critical=5m, warning=15m, info=60m) |
| `node` | Node-level investigation |
| `deployment` / `app` | Alert correlation + incident memory |
| `service` | Network specialist routing |
| `ingress` | Network specialist routing |
| `persistentvolumeclaim` | Infrastructure specialist routing |

### Testing Without Grafana

Use `make alert` or the curl command above to send simulated alerts directly.

---

## Connecting Slack (Human-in-the-Loop)

Slack is where the agent communicates with your team: posting diagnoses, proposing fixes, and waiting for approval.

### Step 1: Create a Slack App

1. Go to https://api.slack.com/apps and click **Create New App**
2. Choose **From scratch**, name it (e.g. "K8s Runbook Agent")
3. Select your workspace

### Step 2: Configure Bot Token Scopes

Go to **OAuth & Permissions** and add these **Bot Token Scopes**:

| Scope | Why |
|-------|-----|
| `chat:write` | Post diagnosis messages and fix proposals |
| `commands` | Handle `/k8s-diag` slash command |

### Step 3: Install to Workspace

Click **Install to Workspace** and copy:
- **Bot User OAuth Token** (`xoxb-...`) → set as `SLACK_BOT_TOKEN`
- **Signing Secret** (from Basic Information page) → set as `SLACK_SIGNING_SECRET`

### Step 4: Configure Interactive Components

Go to **Interactivity & Shortcuts** → Enable → Set Request URL:

```
https://<your-host>/slack/interactions
```

This is where Slack sends button click events (Approve / Reject / View Details / Rollback).

### Step 5: Create Slash Command

Go to **Slash Commands** → Create:

| Field | Value |
|-------|-------|
| Command | `/k8s-diag` |
| Request URL | `https://<your-host>/slack/commands` |
| Description | Check K8s runbook agent status |

### Step 6: Set Environment Variables

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_CHANNEL_ID=C0123456789    # Channel where alerts are posted
```

### What Users See in Slack

**When an alert fires**, the agent posts a threaded investigation:

```
🔔 Alert: KubePodCrashLooping
   Namespace: production
   Pod: api-server-abc123
   Severity: critical

🔍 Investigating... (matched runbook: pod-crashloopbackoff)
```

**After diagnosis**, the agent posts the proposed fix:

```
🩺 Diagnosis
   Root Cause: OOMKilled — memory limit 256Mi too low for workload
   Confidence: HIGH
   Evidence:
     • Last termination reason: OOMKilled (exit code 137)
     • Memory usage: 254Mi / 256Mi limit

🔧 Proposed Fix
   Increase memory limit from 256Mi to 512Mi
   Risk: LOW
   Rollback: kubectl rollout undo deployment/api-server

   [✅ Approve Fix]  [❌ Reject]  [📋 View Details]
```

**After approval**, the agent executes and reports:

```
✅ Execution Result: Increased memory limit to 512Mi
   Details: Patched deployment/api-server
   Verification: Pod running, 0 restarts in last 5 minutes
```

### Slash Commands

| Command | What It Does |
|---------|-------------|
| `/k8s-diag status` | Show all active investigation sessions |
| `/k8s-diag details diag-abc123` | Show full details for a session |

### RBAC (Who Can Approve Fixes)

```bash
# Only these Slack users can approve fixes
APPROVAL_ALLOWED_USERS=U123,U456,U789

# Slack user groups that can approve
APPROVAL_ALLOWED_GROUPS=S001

# Senior users required for HIGH+ risk fixes
APPROVAL_SENIOR_USERS=U001,U002
APPROVAL_MIN_RISK_FOR_SENIOR=high
```

### Escalation SLA

If no one responds within the SLA window, the agent:
1. Posts an urgent reminder tagging the on-call group
2. Creates a PagerDuty/OpsGenie incident (if configured)
3. Auto-rejects the fix at 2x the SLA and marks for manual investigation

```bash
ESCALATION_SLA_CRITICAL=300   # 5 minutes
ESCALATION_SLA_WARNING=900    # 15 minutes
ESCALATION_SLA_INFO=3600      # 1 hour
ESCALATION_GROUP=S123456      # Slack user group to tag
```

---

## Connecting PagerDuty / OpsGenie

When the SLA is breached and no one responds in Slack, the agent creates an incident on PagerDuty or OpsGenie (or both) to page on-call.

### PagerDuty Setup

1. In PagerDuty: go to your **Service** → **Integrations** → **Add Integration**
2. Select **Events API v2**
3. Copy the **Integration Key** (routing key)
4. Set in agent:

```bash
PAGERDUTY_ROUTING_KEY=R0xxxxxxxxxxxxxxxxxxxxxxxxxxxx
# Optional: full API key for adding notes to incidents
PAGERDUTY_API_KEY=u+xxxxxxxxxxxxxxxx
```

### OpsGenie Setup

1. In OpsGenie: **Settings** → **Integrations** → **Add Integration** → **API**
2. Copy the **API Key**
3. Set in agent:

```bash
OPSGENIE_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
OPSGENIE_TEAM=platform-on-call
OPSGENIE_REGION=us   # or "eu"
```

### Incident Lifecycle

| Event | Slack | PagerDuty | OpsGenie |
|-------|-------|-----------|----------|
| SLA breach | Urgent reminder | Trigger incident | Create alert (P1/P2/P3) |
| Human approves | Confirmation | Acknowledge | Acknowledge |
| Fix succeeds | Result posted | Resolve | Close + note |
| Fix fails | Error details | Add note | Add note |
| Human rejects | Rejection posted | Resolve + note | Close + note |

Both providers are optional. If neither is configured, escalation stays Slack-only.

---

## Agent Health Checks

### Basic Health (Liveness Probe)

```bash
curl http://localhost:8080/health
# {"status": "ok"}
```

### Deep Readiness (All Dependencies)

```bash
curl http://localhost:8080/ready
```

```json
{
  "status": "ready",
  "checks": {
    "anthropic_api_key": "ok",
    "kubernetes": "ok",
    "postgresql": "ok",
    "slack_token": "ok",
    "embeddings": "ok",
    "multi_agent": "all_healthy",
    "multi_agent_healthy": 8,
    "active_sessions": 2
  }
}
```

### Per-Agent Health (Multi-Agent Mode)

```bash
curl http://localhost:8080/ready/agents
# Force refresh (bypass 60s cache):
curl "http://localhost:8080/ready/agents?force=true"
```

```json
{
  "status": "all_healthy",
  "multi_agent_enabled": true,
  "agents": {
    "triage": {
      "status": "ok",
      "model": "claude-3-5-haiku-20241022",
      "latency_ms": 245.3,
      "details": {"fallback": "deterministic routing (always available)"}
    },
    "specialist_pod": {
      "status": "ok",
      "model": "claude-sonnet-4-20250514",
      "tool_count": 11,
      "latency_ms": 312.1,
      "details": {"domain": "pod", "expected_tools": 11}
    },
    "specialist_network": { "status": "ok", "tool_count": 11 },
    "specialist_infrastructure": { "status": "ok", "tool_count": 11 },
    "specialist_application": { "status": "ok", "tool_count": 13 },
    "coordinator": {
      "status": "ok",
      "model": "claude-opus-4-20250514",
      "details": {"token_budget": 8000, "activation": "correlated alerts only"}
    },
    "executor": {
      "status": "ok",
      "tool_count": 6,
      "details": {"mutation_tools": ["patch_resource", "scale_deployment", "..."], "dry_run_default": true}
    },
    "embeddings": {
      "status": "ok",
      "model": "voyage-3",
      "latency_ms": 180.5,
      "details": {"dimensions": 1024}
    }
  },
  "healthy_count": 8,
  "degraded_count": 0,
  "error_count": 0
}
```

Each agent is pinged independently. Health states: `ok`, `degraded` (rate-limited but reachable), `error` (cannot function), `not_configured`.

---

## Production Deployment

### Infrastructure Requirements

| Component | Minimum | Recommended | Notes |
|-----------|---------|-------------|-------|
| **PostgreSQL** | 14+ with pgvector | 16+ with pgvector, 2 vCPU, 4GB RAM | RDS/CloudSQL/AlloyDB with `pgvector` extension enabled |
| **Agent pod** | 100m CPU, 256Mi RAM | 500m CPU, 512Mi RAM | Single replica (leader election handles HA) |
| **Network** | Egress to Anthropic API, Slack API | + PagerDuty/OpsGenie APIs | See NetworkPolicy in Helm chart |
| **TLS termination** | Required | Ingress controller or cloud LB | Agent speaks plain HTTP internally; TLS at the edge |
| **Kubernetes RBAC** | ClusterRole for read access | + write access for mutations | Helm chart creates both ClusterRoles |

### Step 1: Provision PostgreSQL with pgvector

```bash
# AWS RDS
aws rds create-db-instance \
  --db-instance-identifier k8s-runbook-agent \
  --engine postgres --engine-version 16.4 \
  --db-instance-class db.t3.medium \
  --allocated-storage 20 \
  --master-username agent \
  --master-user-password <generate-strong-password>

# Then enable pgvector:
psql -h <rds-endpoint> -U agent -d k8s_agent -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Google CloudSQL
gcloud sql instances create k8s-runbook-agent \
  --database-version=POSTGRES_16 \
  --tier=db-custom-2-4096 \
  --region=us-central1
# pgvector is pre-installed on CloudSQL for PostgreSQL 14+

# Or use the pgvector Docker image for testing:
docker run -d --name pg-runbook \
  -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=<password> -e POSTGRES_DB=k8s_agent \
  -p 5432:5432 pgvector/pgvector:pg16
```

The agent auto-creates all tables (`sessions`, `audit_log`, `incident_memory`) on first startup.

### Step 2: Set Up Secrets

**Option A: Kubernetes Secrets (simple)**

```bash
kubectl create namespace k8s-runbook-agent

kubectl create secret generic k8s-runbook-agent-secrets \
  -n k8s-runbook-agent \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=SLACK_BOT_TOKEN=xoxb-... \
  --from-literal=SLACK_SIGNING_SECRET=... \
  --from-literal=GRAFANA_WEBHOOK_SECRET=<random-string> \
  --from-literal=DATABASE_URL=postgresql://agent:<password>@<pg-host>:5432/k8s_agent \
  --from-literal=ADMIN_API_KEY=<random-string>
```

**Option B: ExternalSecrets operator (recommended for production)**

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: k8s-runbook-agent-secrets
  namespace: k8s-runbook-agent
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager  # or vault, gcp-secretmanager
    kind: ClusterSecretStore
  target:
    name: k8s-runbook-agent-secrets
  data:
    - secretKey: ANTHROPIC_API_KEY
      remoteRef:
        key: k8s-runbook-agent/anthropic-api-key
    - secretKey: SLACK_BOT_TOKEN
      remoteRef:
        key: k8s-runbook-agent/slack-bot-token
    # ... etc
```

**Option C: HashiCorp Vault with CSI driver**

```yaml
# Mount secrets as files at /secrets/ — the agent's SecretReloader watches this path
volumes:
  - name: vault-secrets
    csi:
      driver: secrets-store.csi.k8s.io
      readOnly: true
      volumeAttributes:
        secretProviderClass: k8s-runbook-agent-vault
```

### Step 3: Build and Push the Container Image

```bash
cd k8s_runbook_agent

# Build
docker build -t <your-registry>/k8s-runbook-agent:v1.0.0 .

# Push
docker push <your-registry>/k8s-runbook-agent:v1.0.0
```

The Dockerfile is multi-stage (builder + runtime), runs as non-root user `agent` (UID 1000), and supports `readOnlyRootFilesystem`.

### Step 4: Deploy with Helm

```bash
# Production deployment
helm install runbook-agent helm/k8s-runbook-agent/ \
  -n k8s-runbook-agent --create-namespace \
  -f helm/k8s-runbook-agent/values-production.yaml \
  --set image.repository=<your-registry>/k8s-runbook-agent \
  --set image.tag=v1.0.0 \
  --set secrets.existingSecret=k8s-runbook-agent-secrets \
  --set slack.channelId=C0123456789

# Staging deployment (dry-run mode, relaxed SLAs)
helm install runbook-agent helm/k8s-runbook-agent/ \
  -n k8s-runbook-agent-staging --create-namespace \
  -f helm/k8s-runbook-agent/values-staging.yaml \
  --set image.repository=<your-registry>/k8s-runbook-agent \
  --set image.tag=v1.0.0 \
  --set secrets.anthropicApiKey=sk-ant-... \
  --set secrets.slackBotToken=xoxb-... \
  --set secrets.databaseUrl=postgresql://...
```

### Step 5: Configure Ingress (TLS Termination)

The agent needs to be reachable from Grafana (webhook) and Slack (interactive callbacks). Both require HTTPS.

**nginx-ingress example:**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: k8s-runbook-agent
  namespace: k8s-runbook-agent
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts: [runbook-agent.your-domain.com]
      secretName: runbook-agent-tls
  rules:
    - host: runbook-agent.your-domain.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: k8s-runbook-agent
                port:
                  number: 80
```

**AWS ALB example:**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: k8s-runbook-agent
  namespace: k8s-runbook-agent
  annotations:
    kubernetes.io/ingress.class: alb
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:...
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
    alb.ingress.kubernetes.io/ssl-redirect: "443"
    alb.ingress.kubernetes.io/target-type: ip
spec:
  rules:
    - host: runbook-agent.your-domain.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: k8s-runbook-agent
                port:
                  number: 80
```

### Step 6: Connect Grafana and Slack

See [Connecting Grafana](#connecting-grafana-alert-source) and [Connecting Slack](#connecting-slack-human-in-the-loop) sections above. Use the Ingress hostname for all URLs:

- Grafana webhook: `https://runbook-agent.your-domain.com/webhooks/grafana`
- Slack interactivity: `https://runbook-agent.your-domain.com/slack/interactions`
- Slack slash command: `https://runbook-agent.your-domain.com/slack/commands`

### Step 7: Import Grafana Dashboard

```bash
# Via Grafana API
curl -X POST https://grafana.your-domain.com/api/dashboards/db \
  -H "Authorization: Bearer <grafana-api-key>" \
  -H "Content-Type: application/json" \
  -d @deploy/grafana/dashboard.json

# Or via Grafana UI: Dashboards → Import → Upload JSON
```

### Step 8: Verify Production Deployment

```bash
# Basic health
curl https://runbook-agent.your-domain.com/health
# {"status": "ok"}

# Deep readiness (all dependencies)
curl https://runbook-agent.your-domain.com/ready
# Should show: anthropic_api_key=ok, kubernetes=ok, postgresql=ok, slack_token=ok

# Per-agent health (if multi-agent enabled)
curl https://runbook-agent.your-domain.com/ready/agents

# Send a test alert
curl -X POST https://runbook-agent.your-domain.com/webhooks/grafana \
  -H "Authorization: Bearer <your-webhook-secret>" \
  -H "Content-Type: application/json" \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"TestAlert","namespace":"default","severity":"info"},"fingerprint":"test-prod-001"}]}'

# Check it created a session
curl https://runbook-agent.your-domain.com/sessions

# Admin endpoints (require ADMIN_API_KEY)
curl https://runbook-agent.your-domain.com/admin/clusters \
  -H "Authorization: Bearer <your-admin-api-key>"
```

---

### Production Hardening Checklist

| Category | Setting | Value | Why |
|----------|---------|-------|-----|
| **Security mode** | `PRODUCTION_MODE` | `true` | Validates all secrets at startup, sanitizes error responses, adds HSTS |
| **Admin auth** | `ADMIN_API_KEY` | `<random-64-char>` | Protects `/admin/*` endpoints |
| **Webhook auth** | `GRAFANA_WEBHOOK_SECRET` | `<random-string>` | Prevents unauthorized alert injection |
| **Slack auth** | `SLACK_SIGNING_SECRET` | from Slack app | HMAC verification of all Slack requests |
| **Payload limit** | `MAX_PAYLOAD_BYTES` | `1048576` (1MB) | Prevents memory exhaustion from oversized requests |
| **Session limit** | `MAX_CONCURRENT_SESSIONS` | `50` | Prevents alert storms from overwhelming the agent |
| **Token budget** | `MAX_TOKENS_PER_SESSION` | `50000` | Caps API cost per investigation |
| **Dry-run** | `DRY_RUN_DEFAULT` | `false` | Enable live mutations (set `true` for staging) |
| **RBAC** | `APPROVAL_ALLOWED_USERS` | `U123,U456` | Only listed Slack users can approve fixes |
| **Senior approval** | `APPROVAL_SENIOR_USERS` | `U001,U002` | Required for HIGH+ risk fixes |
| **Escalation SLA** | `ESCALATION_SLA_CRITICAL` | `300` | Page on-call after 5 minutes with no response |
| **Data retention** | `SESSION_RETENTION_DAYS` | `30` | Auto-prune resolved sessions from PostgreSQL |
| **Audit retention** | `AUDIT_RETENTION_DAYS` | `90` | Auto-prune old audit log entries |
| **Memory retention** | `MEMORY_RETENTION_DAYS` | `365` | Keep incident memory for 1 year |
| **Memory eviction** | `IN_MEMORY_EVICTION_HOURS` | `1` | Free RAM from resolved sessions after 1 hour |

### Container Security (Helm Chart Defaults)

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  seccompProfile:
    type: RuntimeDefault
  capabilities:
    drop: [ALL]
```

### NetworkPolicy (Helm Chart Default)

The Helm chart creates a NetworkPolicy that restricts traffic to:

| Direction | Target | Port | Why |
|-----------|--------|------|-----|
| **Ingress** | Any → Agent | 8080 | Grafana webhooks, Slack callbacks, health checks |
| **Egress** | Agent → DNS | 53 | Kubernetes service discovery |
| **Egress** | Agent → K8s API | 443, 6443 | Cluster inspection and mutations |
| **Egress** | Agent → PostgreSQL | 5432 | Session persistence, incident memory |
| **Egress** | Agent → Anthropic/Slack/PD/OG | 443 | External API calls |

### Security Headers (Automatic)

Every response includes:

```
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
Content-Security-Policy: default-src 'none'
Cache-Control: no-store
Strict-Transport-Security: max-age=31536000; includeSubDomains  (production mode)
```

### Monitoring in Production

1. **Prometheus**: scrape `/metrics` (annotations are set in the Helm chart)
2. **Grafana dashboard**: import `deploy/grafana/dashboard.json` — 14 panels covering alerts, diagnoses, tool calls, latency, errors, escalations, multi-agent routing
3. **Alerting on the agent itself**: set up Grafana alerts for:
   - `runbook_escalations_total` rate > 0 (agent can't handle alerts)
   - `runbook_anthropic_calls_total{status="error"}` rate > 0 (API issues)
   - `runbook_active_sessions` > 10 (investigation backlog)
4. **Log aggregation**: structured JSON logs with correlation IDs (`session_id`, `alert_name`, `namespace`) — pipe to Loki, Elasticsearch, or CloudWatch

### Upgrading

```bash
# Build new image
docker build -t <registry>/k8s-runbook-agent:v1.1.0 .
docker push <registry>/k8s-runbook-agent:v1.1.0

# Rolling upgrade (zero downtime)
helm upgrade runbook-agent helm/k8s-runbook-agent/ \
  -n k8s-runbook-agent \
  -f helm/k8s-runbook-agent/values-production.yaml \
  --set image.tag=v1.1.0

# Rollback if needed
helm rollback runbook-agent -n k8s-runbook-agent
```

The Deployment uses `maxUnavailable: 0, maxSurge: 1` for zero-downtime rolling updates. Active sessions survive upgrades because they're persisted to PostgreSQL.

---

## Incident Memory (pgvector RAG)

The agent learns from past incidents. After each resolved session, it embeds and stores the incident. Before each new investigation, it retrieves similar past incidents and injects them into Claude's context.

### What Claude Sees

```
## Incident Memory — Similar Past Incidents

### Past Incidents (3 matches, cosine similarity > 0.7)
1. [2025-03-18] OOMKilled in production/api-server
   Root cause: Memory limit 256Mi too low for Java heap
   Fix: Increase to 512Mi → SUCCESS (similarity: 0.94)

### Fix Success Rates for "KubePodCrashLooping"
- "Increase memory limit": 5/5 (100%)
- "Restart pod": 2/3 (67%)

### Recurring Pattern Warning
This workload has had 3 OOMKilled incidents in 7 days.
Consider a permanent capacity review rather than the same remediation.
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `INCIDENT_MEMORY_ENABLED` | `true` | Enable/disable |
| `INCIDENT_MEMORY_RECALL_LIMIT` | `5` | Max past incidents per query |
| `INCIDENT_MEMORY_RECURRING_THRESHOLD` | `3` | Incidents in window to flag |
| `INCIDENT_MEMORY_RECURRING_WINDOW_DAYS` | `7` | Time window for detection |
| `VOYAGE_MODEL` | `voyage-3` | Embedding model |

Uses Voyage AI for embeddings (via your Anthropic API key). Falls back to PostgreSQL tsvector full-text search if embeddings are unavailable.

---

## Custom Runbooks

Place YAML files in `knowledge/runbooks/`. They are hot-reloaded every 30 seconds.

```yaml
apiVersion: runbook/v1
kind: DiagnosticRunbook

metadata:
  id: my-custom-runbook
  title: "My Custom Alert Diagnosis"
  severity_signals:
    - alertname: MyCustomAlert
  tags: [custom, myapp]

initial_inspection:
  - tool: get_pod_status
    why: "Check pod health"
  - tool: get_pod_logs
    why: "Read application logs"

diagnosis_tree:
  - symptom: "Error X in logs"
    root_causes:
      - cause: "Config value Y is wrong"
        confidence_signals:
          - "Logs show 'invalid config Y'"
        resolution_strategy: |
          Patch the ConfigMap to fix value Y.

fallback:
  message: "Cannot determine root cause."
  action: "Escalate to team."
```

16 runbooks are included covering: CrashLoop, OOM, ImagePull, Eviction, Scheduling, Node NotReady, CPU Throttling, HPA, PVC, DNS, Ingress, Service Endpoints, Certificates, Jobs, Error Rates, Deployment Rollouts.

---

## Safety Guardrails

Every fix must pass all 8 guardrails before execution:

| # | Guardrail | Action |
|---|-----------|--------|
| 1 | Namespace blocklist | BLOCK kube-system, monitoring, cert-manager, etc. |
| 2 | Risk ceiling | BLOCK CRITICAL risk |
| 3 | Human values needed | BLOCK until human provides values |
| 4 | Replica bounds | BLOCK >50, WARN on 0 |
| 5 | Image safety | BLOCK `:latest` tags |
| 6 | Dry-run required | WARN if no dry-run performed |
| 7 | Rollback plan | WARN if missing |
| 8 | Confidence check | BLOCK LOW, WARN MEDIUM |

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | Liveness probe |
| GET | `/ready` | None | Deep readiness with dependency checks |
| GET | `/ready/agents` | None | Per-agent health (multi-agent) |
| GET | `/metrics` | None | Prometheus metrics |
| POST | `/webhooks/grafana` | `GRAFANA_WEBHOOK_SECRET` | Alert ingestion |
| POST | `/slack/interactions` | Slack HMAC | Button callbacks (approve/reject) |
| POST | `/slack/commands` | Slack HMAC | `/k8s-diag` slash command |
| GET | `/sessions` | Rate-limited | List all sessions |
| GET | `/sessions/{id}` | Rate-limited | Session details |
| GET | `/sessions/{id}/audit` | Rate-limited | Audit trail |
| POST | `/admin/runbooks/reload` | `ADMIN_API_KEY` | Hot-reload runbooks |
| POST | `/admin/secrets/reload` | `ADMIN_API_KEY` | Reload secrets without restart |
| GET | `/admin/clusters` | `ADMIN_API_KEY` | Multi-cluster info |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| **Core** | | | |
| `ANTHROPIC_API_KEY` | **Yes** | — | Claude API + Voyage embeddings |
| `DATABASE_URL` | Recommended | — | PostgreSQL with pgvector |
| `KUBECONFIG` | No | in-cluster | Path to kubeconfig |
| `DRY_RUN_DEFAULT` | No | `true` | Dry-run mode for mutations |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `MAX_TOKENS_PER_SESSION` | No | `0` | Token budget per session (0=unlimited) |
| **Security** | | | |
| `PRODUCTION_MODE` | No | `false` | Enforce secrets, sanitize errors, add HSTS |
| `ADMIN_API_KEY` | Prod: **Yes** | — | Bearer token for `/admin/*` endpoints |
| `MAX_PAYLOAD_BYTES` | No | `1048576` | Max request body size (1MB) |
| `MAX_CONCURRENT_SESSIONS` | No | `50` | Cap on simultaneous investigations |
| **Slack** | | | |
| `SLACK_BOT_TOKEN` | Recommended | — | Bot token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Prod: **Yes** | — | HMAC signature verification |
| `SLACK_CHANNEL_ID` | Recommended | — | Default channel for alerts |
| **Grafana** | | | |
| `GRAFANA_WEBHOOK_SECRET` | Prod: **Yes** | — | Webhook Bearer token auth |
| **Multi-Agent** | | | |
| `MULTI_AGENT_ENABLED` | No | `false` | Enable multi-agent pipeline |
| `TRIAGE_MODEL` | No | `claude-3-5-haiku-20241022` | Triage classifier model |
| `SPECIALIST_MODEL` | No | `claude-sonnet-4-20250514` | Specialist diagnosis model |
| `COORDINATOR_MODEL` | No | `claude-opus-4-20250514` | Coordinator synthesis model |
| `COORDINATOR_TOKEN_BUDGET` | No | `8000` | Max tokens for coordinator |
| **Escalation** | | | |
| `ESCALATION_SLA_CRITICAL` | No | `300` | SLA for critical alerts (seconds) |
| `ESCALATION_SLA_WARNING` | No | `900` | SLA for warning alerts |
| `ESCALATION_SLA_INFO` | No | `3600` | SLA for info alerts |
| `ESCALATION_GROUP` | No | — | Slack user group to tag |
| **Incident Memory** | | | |
| `INCIDENT_MEMORY_ENABLED` | No | `true` | Enable pgvector RAG |
| `INCIDENT_MEMORY_RECALL_LIMIT` | No | `5` | Past incidents to retrieve |
| `VOYAGE_MODEL` | No | `voyage-3` | Embedding model |
| **Incident Management** | | | |
| `PAGERDUTY_ROUTING_KEY` | No | — | PagerDuty Events API v2 key |
| `OPSGENIE_API_KEY` | No | — | OpsGenie API key |
| `OPSGENIE_TEAM` | No | — | OpsGenie team routing |
| `OPSGENIE_REGION` | No | `us` | OpsGenie region (us/eu) |
| **RBAC** | | | |
| `APPROVAL_ALLOWED_USERS` | No | — | Comma-separated Slack user IDs |
| `APPROVAL_SENIOR_USERS` | No | — | Senior users for HIGH+ risk |
| `APPROVAL_MIN_RISK_FOR_SENIOR` | No | `high` | Risk threshold for senior approval |
| **Data Retention** | | | |
| `SESSION_RETENTION_DAYS` | No | `30` | Auto-prune resolved sessions from PG |
| `AUDIT_RETENTION_DAYS` | No | `90` | Auto-prune audit log entries |
| `MEMORY_RETENTION_DAYS` | No | `365` | Auto-prune incident memory records |
| `IN_MEMORY_EVICTION_HOURS` | No | `1` | Evict resolved sessions from RAM |

See `.env.example` for the complete list.

---

## Running Tests

```bash
make test              # 314 unit tests
make test-integration  # +26 integration tests (needs API key / cluster)
make test-cov          # With coverage report
make lint              # Ruff check + format
```

---

## Makefile Targets

```bash
make install            # Install dependencies
make run                # Start with hot-reload (dev)
make run-prod           # Start production mode
make test               # Run all unit tests
make health             # curl /health
make ready              # curl /ready (all dependencies)
make ready-agents       # curl /ready/agents (per-agent health)
make ready-agents-force # Per-agent health (bypass cache)
make alert              # Send test alert
make sessions           # List sessions
make reload-runbooks    # Hot-reload runbooks
make docker-build       # Build container image
make docker-run         # Run container
```

---

## Architecture Overview

```
Grafana Alert ──webhook──▶ FastAPI Server (port 8080)
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              Alert Parser          Session Store
              + Dedup               (in-mem + PG)
              + Correlation              │
                    │                    │
                    ▼                    │
         ┌── Orchestrator ◀─────────────┘
         │   ┌─────────────────────────────────────────┐
         │   │ Single-Agent         OR   Multi-Agent   │
         │   │ (Sonnet, 22 tools)       ┌─────────┐    │
         │   │                          │ Triage  │    │
         │   │                          │ (Haiku) │    │
         │   │                          └────┬────┘    │
         │   │                    ┌──────────┼─────┐   │
         │   │                    ▼     ▼    ▼     ▼   │
         │   │                  Pod  Net  Infra  App   │
         │   │                 (Sonnet, filtered tools) │
         │   │                          │              │
         │   │                    Coordinator (Opus)   │
         │   └─────────────────────────────────────────┘
         │         │
         │         ├──▶ Incident Memory (pgvector recall + record)
         │         ├──▶ Knowledge Base (16 runbooks, hot-reloaded)
         │         ├──▶ K8s Tools (14 read-only + 6 mutation)
         │         └──▶ Guardrails (8 safety checks)
         │
         ▼
   Slack Bot ──buttons──▶ Human Approval
         │                     │
         │         ┌───────────┘
         │         ▼
         │    Fix Executor (Sonnet)
         │    (dry-run → live → verify)
         │         │
         │         ├──▶ Incident Memory (record outcome)
         │         └──▶ PagerDuty / OpsGenie (resolve incident)
         │
         ▼
   Results posted to Slack thread
```

---

## Project Stats

```
77 Python modules
16 diagnostic runbooks (75 root causes)
22 Kubernetes tools (14 read-only + 6 mutation)
 8 agent types (Triage, 4 Specialists, Coordinator, Executor, Single)
 8 safety guardrails + production hardening layer
314 unit tests + 26 integration tests
10 Helm chart templates
14-panel Grafana dashboard
Incident memory with pgvector RAG
Per-agent health checks with 60s-cached model pings
PagerDuty + OpsGenie incident management
Data retention manager (session/audit/memory TTL)
Security middleware (payload limits, headers, admin auth, error sanitization)
```
