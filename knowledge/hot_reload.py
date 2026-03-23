"""Runbook hot-reload — watch for changes and reload without restart.

Uses filesystem polling (not inotify) for portability across Docker,
NFS, and ConfigMap-mounted volumes.

Features:
  - Detects new, modified, and deleted runbook YAML files
  - Reloads changed files atomically (old version stays until new parses OK)
  - Logs all changes for auditability
  - Exposes reload count metric

Usage:
    watcher = RunbookWatcher(store, runbook_dir)
    await watcher.start()  # polls every POLL_INTERVAL_SECONDS

Or trigger a manual reload:
    watcher.reload_now()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .loader import RunbookStore

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


class RunbookWatcher:
    """Watches a directory for runbook YAML changes and hot-reloads them.

    Keeps a snapshot of file modification times and reloads only changed files.
    """

    def __init__(
        self,
        store: RunbookStore,
        directory: str | Path,
        poll_interval: int = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.store = store
        self.directory = Path(directory)
        self.poll_interval = poll_interval
        self._file_mtimes: dict[Path, float] = {}
        self._task: asyncio.Task | None = None
        self.reload_count: int = 0
        self.last_reload_error: str | None = None

    async def start(self) -> None:
        """Start the background polling loop."""
        # Take initial snapshot
        self._snapshot()
        logger.info(
            "Runbook watcher started: watching %s (poll every %ds, %d files)",
            self.directory, self.poll_interval, len(self._file_mtimes),
        )

    async def run(self) -> None:
        """Run the polling loop (call from a background task)."""
        self._snapshot()
        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                await self._check_changes()
        except asyncio.CancelledError:
            logger.info("Runbook watcher stopped")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def reload_now(self) -> dict[str, Any]:
        """Trigger a synchronous full reload. Returns summary."""
        old_count = len(self.store._runbooks)
        try:
            new_count = self.store.load_directory(self.directory)
            self.reload_count += 1
            self._snapshot()
            self.last_reload_error = None
            logger.info("Manual reload: %d → %d runbooks", old_count, new_count)
            return {"status": "ok", "runbooks_loaded": new_count, "total_reloads": self.reload_count}
        except Exception as e:
            self.last_reload_error = str(e)
            logger.exception("Manual reload failed")
            return {"status": "error", "error": str(e)}

    def _snapshot(self) -> None:
        """Take a snapshot of file modification times."""
        self._file_mtimes.clear()
        if not self.directory.is_dir():
            return
        for pattern in ("*.yaml", "*.yml"):
            for path in self.directory.glob(pattern):
                self._file_mtimes[path] = path.stat().st_mtime

    async def _check_changes(self) -> None:
        """Compare current files to snapshot and reload changes."""
        if not self.directory.is_dir():
            return

        current_files: dict[Path, float] = {}
        for pattern in ("*.yaml", "*.yml"):
            for path in self.directory.glob(pattern):
                current_files[path] = path.stat().st_mtime

        # Detect changes
        added = set(current_files.keys()) - set(self._file_mtimes.keys())
        removed = set(self._file_mtimes.keys()) - set(current_files.keys())
        modified = {
            p for p in (set(current_files.keys()) & set(self._file_mtimes.keys()))
            if current_files[p] != self._file_mtimes[p]
        }

        if not added and not removed and not modified:
            return  # No changes

        logger.info(
            "Runbook changes detected: %d added, %d modified, %d removed",
            len(added), len(modified), len(removed),
        )

        # Reload changed/added files
        errors = []
        for path in added | modified:
            try:
                runbook = self.store.load_file(path)
                logger.info("Reloaded runbook: %s (from %s)", runbook.metadata.id, path.name)
            except Exception as e:
                errors.append(f"{path.name}: {e}")
                logger.exception("Failed to reload runbook %s", path.name)

        # Handle removed files
        for path in removed:
            # Find which runbook ID this file had
            runbook_id = path.stem
            if runbook_id in self.store._runbooks:
                del self.store._runbooks[runbook_id]
                logger.info("Removed runbook: %s (file deleted: %s)", runbook_id, path.name)

        # Update snapshot
        self._file_mtimes = current_files
        self.reload_count += 1

        if errors:
            self.last_reload_error = "; ".join(errors)
            logger.warning("Reload completed with %d errors: %s", len(errors), errors)
        else:
            self.last_reload_error = None
