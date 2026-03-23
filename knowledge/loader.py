"""Load diagnostic runbook YAMLs and provide keyword-based search.

Version tracking: each loaded runbook is stamped with:
  - file_hash: SHA-256 of the YAML file content
  - git_sha: HEAD commit SHA of the runbook directory (if in a git repo)
  - loaded_at: UTC timestamp when loaded
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..models import (
    DiagnosticRunbook,
    DiagnosisBranch,
    Fallback,
    InspectionStep,
    RootCause,
    RunbookMatch,
    RunbookMetadata,
)


def _get_git_sha(directory: Path) -> str:
    """Get the HEAD commit SHA for a directory, or empty string if not in git."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _file_hash(filepath: Path) -> str:
    """SHA-256 hash of a file's contents."""
    return hashlib.sha256(filepath.read_bytes()).hexdigest()[:16]


class RunbookVersion:
    """Tracks the version of a loaded runbook for auditability."""

    def __init__(self, runbook_id: str, file_path: str, file_hash: str, git_sha: str) -> None:
        self.runbook_id = runbook_id
        self.file_path = file_path
        self.file_hash = file_hash
        self.git_sha = git_sha
        self.loaded_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "runbook_id": self.runbook_id,
            "file_path": self.file_path,
            "file_hash": self.file_hash,
            "git_sha": self.git_sha,
            "loaded_at": self.loaded_at.isoformat(),
        }


class RunbookStore:
    """In-memory store of diagnostic runbooks loaded from YAML files."""

    def __init__(self) -> None:
        self._runbooks: dict[str, DiagnosticRunbook] = {}
        self._versions: dict[str, RunbookVersion] = {}
        self._git_sha: str = ""

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load_directory(self, directory: str | Path) -> int:
        """Load all .yaml/.yml files from *directory*. Returns count loaded."""
        path = Path(directory)
        if not path.is_dir():
            raise FileNotFoundError(f"Runbook directory not found: {path}")

        self._git_sha = _get_git_sha(path)

        count = 0
        for yaml_file in sorted(path.glob("*.yaml")):
            self.load_file(yaml_file)
            count += 1
        for yaml_file in sorted(path.glob("*.yml")):
            self.load_file(yaml_file)
            count += 1
        return count

    def load_file(self, filepath: str | Path) -> DiagnosticRunbook:
        """Parse a single YAML runbook file into a DiagnosticRunbook."""
        filepath = Path(filepath)
        with open(filepath) as f:
            raw = yaml.safe_load(f)

        runbook = _parse_runbook(raw)
        self._runbooks[runbook.metadata.id] = runbook

        # Track version
        self._versions[runbook.metadata.id] = RunbookVersion(
            runbook_id=runbook.metadata.id,
            file_path=str(filepath),
            file_hash=_file_hash(filepath),
            git_sha=self._git_sha,
        )

        return runbook

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def get(self, runbook_id: str) -> DiagnosticRunbook | None:
        return self._runbooks.get(runbook_id)

    @property
    def all_runbooks(self) -> list[DiagnosticRunbook]:
        return list(self._runbooks.values())

    @property
    def git_sha(self) -> str:
        """Git commit SHA at the time runbooks were loaded."""
        return self._git_sha

    def get_version(self, runbook_id: str) -> RunbookVersion | None:
        return self._versions.get(runbook_id)

    def all_versions(self) -> list[dict]:
        """Return version info for all loaded runbooks."""
        return [v.to_dict() for v in self._versions.values()]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(
        self,
        *,
        query: str = "",
        alert_name: str = "",
        labels: dict[str, str] | None = None,
    ) -> list[RunbookMatch]:
        """Search runbooks by alert name, labels, and free-text query.

        Scoring strategy:
          - Exact alert_name match on severity_signals  → +10
          - Label value found in severity_signals       → +5
          - Tag overlap with query words                → +2 per tag
          - Title word overlap with query words         → +1 per word
        """
        labels = labels or {}
        query_words = set(query.lower().split()) if query else set()
        alert_lower = alert_name.lower()

        results: list[RunbookMatch] = []

        for rb in self._runbooks.values():
            score = 0.0
            reasons: list[str] = []

            # 1. Severity signal matching (alert_name, reason, etc.)
            for signal in rb.metadata.severity_signals:
                for key, val in signal.items():
                    val_lower = val.lower()
                    if key == "alertname" and alert_lower and val_lower == alert_lower:
                        score += 10
                        reasons.append(f"alertname exact match: {val}")
                    elif key == "alertname" and alert_lower and val_lower in alert_lower:
                        score += 5
                        reasons.append(f"alertname partial match: {val}")
                    elif key in labels and labels[key].lower() == val_lower:
                        score += 5
                        reasons.append(f"label match: {key}={val}")
                    elif alert_lower and val_lower in alert_lower:
                        score += 3
                        reasons.append(f"signal match: {key}={val}")

            # 2. Tag overlap with query
            if query_words:
                tag_set = set(t.lower() for t in rb.metadata.tags)
                tag_hits = query_words & tag_set
                if tag_hits:
                    score += len(tag_hits) * 2
                    reasons.append(f"tag matches: {', '.join(tag_hits)}")

            # 3. Title word overlap with query
            if query_words:
                title_words = set(rb.metadata.title.lower().split())
                title_hits = query_words & title_words
                if title_hits:
                    score += len(title_hits)
                    reasons.append(f"title matches: {', '.join(title_hits)}")

            # 4. Query words in description
            if query_words and rb.metadata.description:
                desc_words = set(rb.metadata.description.lower().split())
                desc_hits = query_words & desc_words
                if desc_hits:
                    score += len(desc_hits) * 0.5
                    reasons.append(f"description matches: {', '.join(desc_hits)}")

            if score > 0:
                results.append(
                    RunbookMatch(
                        runbook_id=rb.metadata.id,
                        title=rb.metadata.title,
                        score=score,
                        match_reasons=reasons,
                    )
                )

        results.sort(key=lambda m: m.score, reverse=True)
        return results


# ------------------------------------------------------------------
# YAML → Pydantic parsing
# ------------------------------------------------------------------
def _parse_runbook(raw: dict) -> DiagnosticRunbook:
    """Convert raw YAML dict into a DiagnosticRunbook model."""
    meta_raw = raw.get("metadata", {})
    metadata = RunbookMetadata(
        id=meta_raw["id"],
        title=meta_raw["title"],
        description=meta_raw.get("description", ""),
        severity_signals=meta_raw.get("severity_signals", []),
        tags=meta_raw.get("tags", []),
    )

    inspection = [
        InspectionStep(
            tool=step["tool"],
            why=step.get("why", ""),
            args=step.get("args", {}),
        )
        for step in raw.get("initial_inspection", [])
    ]

    tree = []
    for branch in raw.get("diagnosis_tree", []):
        root_causes = [
            RootCause(
                cause=rc["cause"],
                confidence_signals=rc.get("confidence_signals", []),
                resolution_strategy=rc.get("resolution_strategy", ""),
            )
            for rc in branch.get("root_causes", [])
        ]
        tree.append(
            DiagnosisBranch(
                symptom=branch["symptom"],
                investigation=branch.get("investigation", []),
                root_causes=root_causes,
            )
        )

    fallback_raw = raw.get("fallback", {})
    fallback = Fallback(
        message=fallback_raw.get("message", Fallback().message),
        action=fallback_raw.get("action", Fallback().action),
        collect=fallback_raw.get("collect", []),
    )

    return DiagnosticRunbook(
        metadata=metadata,
        initial_inspection=inspection,
        diagnosis_tree=tree,
        fallback=fallback,
    )
