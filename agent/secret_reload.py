"""Secret rotation support — reload credentials without restarting.

When running in Kubernetes, secrets mounted as environment variables don't
update on rotation.  This module provides two approaches:

1. **Admin endpoint** (`POST /admin/secrets/reload`) — triggers manual reload
2. **File-watch** — if secrets are mounted as files (via CSI driver or
   projected volumes), watches for changes and reloads automatically.

Reloaded secrets take effect on the next API call — existing in-flight
requests use the old values.

Usage in server.py:
    from .agent.secret_reload import SecretReloader, reload_secrets_from_env
    reloader = SecretReloader()
    await reloader.start()    # watch /secrets/ mount path
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# Fields in Settings that are secrets and can be hot-reloaded
RELOADABLE_SECRETS = [
    "anthropic_api_key",
    "slack_bot_token",
    "slack_signing_secret",
    "grafana_webhook_secret",
    "database_url",
    "pagerduty_routing_key",
    "pagerduty_api_key",
    "opsgenie_api_key",
]

# Mapping of env var names to settings fields
ENV_TO_FIELD = {
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "SLACK_BOT_TOKEN": "slack_bot_token",
    "SLACK_SIGNING_SECRET": "slack_signing_secret",
    "GRAFANA_WEBHOOK_SECRET": "grafana_webhook_secret",
    "DATABASE_URL": "database_url",
    "PAGERDUTY_ROUTING_KEY": "pagerduty_routing_key",
    "PAGERDUTY_API_KEY": "pagerduty_api_key",
    "OPSGENIE_API_KEY": "opsgenie_api_key",
}


def reload_secrets_from_env() -> dict[str, str]:
    """Re-read secret env vars and update the settings singleton.

    Returns a dict of field names that were changed (old value masked).
    """
    changed: dict[str, str] = {}

    for env_var, field_name in ENV_TO_FIELD.items():
        new_value = os.environ.get(env_var, "")
        current_value = getattr(settings, field_name, "")

        if new_value and new_value != current_value:
            object.__setattr__(settings, field_name, new_value)
            # Mask for logging — show first 4 and last 4 chars
            masked = _mask_value(new_value)
            changed[field_name] = masked
            logger.info("Secret rotated: %s → %s", field_name, masked)

    return changed


def reload_secrets_from_files(secret_dir: str | Path) -> dict[str, str]:
    """Read secrets from a directory of files (K8s secret volume mount).

    Each file in the directory is named after the env var (e.g. ANTHROPIC_API_KEY)
    and contains the secret value.
    """
    secret_path = Path(secret_dir)
    if not secret_path.is_dir():
        return {}

    changed: dict[str, str] = {}

    for env_var, field_name in ENV_TO_FIELD.items():
        file_path = secret_path / env_var
        if file_path.is_file():
            new_value = file_path.read_text().strip()
            current_value = getattr(settings, field_name, "")

            if new_value and new_value != current_value:
                object.__setattr__(settings, field_name, new_value)
                # Also update os.environ so child processes pick it up
                os.environ[env_var] = new_value
                masked = _mask_value(new_value)
                changed[field_name] = masked
                logger.info("Secret rotated (file): %s → %s", field_name, masked)

    return changed


class SecretReloader:
    """Watches a secret directory for changes and reloads settings.

    Designed for Kubernetes secret volume mounts where the kubelet
    atomically updates the symlink when the Secret is rotated.
    """

    def __init__(
        self,
        secret_dir: str | Path = "/secrets",
        poll_interval: int = 60,
    ) -> None:
        self.secret_dir = Path(secret_dir)
        self.poll_interval = poll_interval
        self._last_mtime: float = 0.0
        self._task: asyncio.Task | None = None
        self.last_reload: datetime | None = None
        self.reload_count: int = 0

    @property
    def enabled(self) -> bool:
        return self.secret_dir.is_dir()

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Secret reloader: disabled (%s does not exist)", self.secret_dir)
            return
        logger.info("Secret reloader: watching %s (every %ds)", self.secret_dir, self.poll_interval)

    async def run(self) -> None:
        """Main polling loop — run as a background task."""
        if not self.enabled:
            return

        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                self._check_and_reload()
        except asyncio.CancelledError:
            logger.info("Secret reloader stopped")

    def _check_and_reload(self) -> None:
        """Check if the secret directory has been updated."""
        try:
            # K8s updates secrets via atomic symlink swap
            # Check the ..data symlink's mtime
            data_link = self.secret_dir / "..data"
            if data_link.exists():
                current_mtime = data_link.stat().st_mtime
            else:
                # Fall back to directory mtime
                current_mtime = self.secret_dir.stat().st_mtime

            if current_mtime > self._last_mtime:
                if self._last_mtime > 0:  # Skip first check (startup)
                    logger.info("Secret directory changed, reloading...")
                    changed = reload_secrets_from_files(self.secret_dir)
                    if changed:
                        self.reload_count += 1
                        self.last_reload = datetime.now(timezone.utc)
                        logger.info(
                            "Secrets reloaded (%d fields updated): %s",
                            len(changed), list(changed.keys()),
                        )
                self._last_mtime = current_mtime

        except Exception:
            logger.exception("Secret reload check failed")

    def force_reload(self) -> dict[str, str]:
        """Force an immediate reload. Returns changed fields."""
        if self.enabled:
            changed = reload_secrets_from_files(self.secret_dir)
        else:
            changed = reload_secrets_from_env()

        if changed:
            self.reload_count += 1
            self.last_reload = datetime.now(timezone.utc)

        return changed

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "secret_dir": str(self.secret_dir),
            "poll_interval": self.poll_interval,
            "reload_count": self.reload_count,
            "last_reload": self.last_reload.isoformat() if self.last_reload else None,
        }


def _mask_value(value: str) -> str:
    """Mask a secret value for safe logging."""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]
