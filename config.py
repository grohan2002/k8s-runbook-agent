"""Configuration loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All settings are read from environment variables (or a .env file)."""

    model_config = {
        "env_file": [
            ".env",                                              # CWD (when running from parent dir)
            str(Path(__file__).parent / ".env"),                  # Inside k8s_runbook_agent/
        ],
        "env_file_encoding": "utf-8",
    }

    # Anthropic
    anthropic_api_key: str = ""

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""

    # Grafana webhook
    grafana_webhook_secret: str = ""

    # Kubernetes — blank means "use in-cluster config"
    kubeconfig: str = ""

    # PostgreSQL — session persistence (empty = in-memory only)
    database_url: str = ""

    # Agent behavior
    dry_run_default: bool = True
    runbook_dir: Path = Path(__file__).parent / "knowledge" / "runbooks"
    log_level: str = "INFO"
    max_tokens_per_session: int = 0  # 0 = unlimited

    # RBAC — comma-separated Slack user IDs
    approval_allowed_users: str = ""
    approval_allowed_groups: str = ""
    approval_senior_users: str = ""
    approval_min_risk_for_senior: str = "high"

    # Escalation SLA (seconds)
    escalation_sla_critical: int = 300
    escalation_sla_warning: int = 900
    escalation_sla_info: int = 3600
    escalation_group: str = ""  # Slack user group to tag

    # Multi-cluster — JSON array of cluster configs
    cluster_configs: str = ""

    # Runbook hot-reload
    runbook_poll_interval: int = 30

    # Incident memory (pgvector RAG)
    incident_memory_enabled: bool = True
    incident_memory_recall_limit: int = 5
    incident_memory_recurring_threshold: int = 3
    incident_memory_recurring_window_days: int = 7
    voyage_model: str = "voyage-3"
    voyage_embedding_dims: int = 1024

    # Multi-agent system
    multi_agent_enabled: bool = False
    triage_model: str = "claude-3-5-haiku-20241022"
    specialist_model: str = "claude-sonnet-4-20250514"
    coordinator_model: str = "claude-opus-4-20250514"
    coordinator_token_budget: int = 8000

    # Fix verification (secondary reviewer)
    fix_verification_enabled: bool = True
    fix_verification_model: str = "claude-3-5-haiku-20241022"
    fix_verification_max_tokens: int = 1024

    # Toil detection (SRE feature)
    toil_detection_enabled: bool = True
    toil_detection_interval_hours: int = 168  # weekly
    toil_detection_window_days: int = 7
    toil_detection_threshold: int = 5

    # PagerDuty (Events API v2)
    pagerduty_routing_key: str = ""   # Integration key from PD service
    pagerduty_api_key: str = ""       # Full API key (optional — for notes)

    # OpsGenie
    opsgenie_api_key: str = ""
    opsgenie_team: str = ""           # Team to route alerts to
    opsgenie_region: str = "us"       # "us" or "eu"

    # Security hardening (production)
    production_mode: bool = False
    admin_api_key: str = ""
    max_payload_bytes: int = 1048576  # 1 MB
    max_concurrent_sessions: int = 50

    # Data retention (days, 0 = never delete)
    session_retention_days: int = 30
    audit_retention_days: int = 90
    memory_retention_days: int = 365
    in_memory_eviction_hours: int = 1


# Singleton — import this wherever you need settings
settings = Settings()
