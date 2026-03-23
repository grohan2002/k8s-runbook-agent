"""Tests for the knowledge base (runbook loading and search)."""

import pytest

from k8s_runbook_agent.knowledge.loader import RunbookStore
from k8s_runbook_agent.config import settings


class TestRunbookStore:
    @pytest.fixture
    def store(self):
        s = RunbookStore()
        s.load_directory(settings.runbook_dir)
        return s

    def test_loads_runbooks(self, store):
        assert len(store._runbooks) >= 3  # crashloop, imagepull, oomkilled

    def test_get_by_id(self, store):
        rb = store.get("pod-crashloopbackoff")
        assert rb is not None
        assert rb.metadata.id == "pod-crashloopbackoff"
        assert len(rb.initial_inspection) > 0
        assert len(rb.diagnosis_tree) > 0

    def test_get_nonexistent(self, store):
        assert store.get("nonexistent") is None

    def test_search_by_alert_name(self, store):
        matches = store.search(
            query="KubePodCrashLooping",
            alert_name="KubePodCrashLooping",
            labels={"namespace": "prod"},
        )
        assert len(matches) > 0
        assert matches[0].runbook_id == "pod-crashloopbackoff"

    def test_search_oom(self, store):
        matches = store.search(
            query="OOMKilled",
            alert_name="KubePodOOMKilled",
            labels={},
        )
        assert len(matches) > 0
        assert any("oom" in m.runbook_id for m in matches)

    def test_search_no_results(self, store):
        matches = store.search(
            query="completely_unrelated_gibberish",
            alert_name="FakeAlert",
            labels={},
        )
        # May return 0 or low-score results
        for m in matches:
            assert m.score < 5.0  # Low relevance


class TestRunbookStructure:
    @pytest.fixture
    def store(self):
        s = RunbookStore()
        s.load_directory(settings.runbook_dir)
        return s

    def test_crashloop_runbook_has_branches(self, store):
        rb = store.get("pod-crashloopbackoff")
        assert rb is not None
        # Should have multiple diagnosis branches
        assert len(rb.diagnosis_tree) >= 2
        # Each branch should have root causes
        for branch in rb.diagnosis_tree:
            assert branch.symptom
            assert len(branch.root_causes) > 0

    def test_runbook_has_fallback(self, store):
        rb = store.get("pod-crashloopbackoff")
        assert rb is not None
        assert rb.fallback.message
        assert rb.fallback.action

    def test_inspection_steps_have_tools(self, store):
        rb = store.get("pod-crashloopbackoff")
        assert rb is not None
        for step in rb.initial_inspection:
            assert step.tool  # Must reference a tool name
            assert step.why   # Must explain why
