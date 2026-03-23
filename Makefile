.PHONY: install dev run test test-integration lint docker-build docker-run clean health alert

# --- Setup ---
install:
	pip install -r requirements.txt

dev: install
	pip install pytest pytest-asyncio httpx ruff

# --- Run ---
# Must run from the PARENT directory so Python can find the k8s_runbook_agent package
run:
	cd .. && uvicorn k8s_runbook_agent.server:app --host 0.0.0.0 --port 8090 --reload

run-prod:
	cd .. && uvicorn k8s_runbook_agent.server:app --host 0.0.0.0 --port 8090 --workers 1 --no-access-log

# --- Test ---
test:
	python3 -m pytest tests/ -v --tb=short

test-quick:
	python3 -m pytest tests/ -q --tb=line

test-integration:
	python3 -m pytest tests/test_integration.py -v --tb=short

test-cov:
	pip install pytest-cov
	python3 -m pytest tests/ --cov=k8s_runbook_agent --cov-report=term-missing --cov-report=html

# --- Lint ---
lint:
	ruff check . --select E,F,W --ignore E501
	ruff format --check .

format:
	ruff format .

# --- Docker ---
docker-build:
	docker build -t k8s-runbook-agent:local .

docker-run: docker-build
	docker run -d --name runbook-agent -p 8090:8090 \
		--env-file .env \
		-v $(HOME)/.kube/config:/home/agent/.kube/config:ro \
		k8s-runbook-agent:local

docker-stop:
	docker stop runbook-agent && docker rm runbook-agent

# --- Helm ---
helm-lint:
	helm lint helm/k8s-runbook-agent/

helm-template:
	helm template runbook-agent helm/k8s-runbook-agent/ -f helm/k8s-runbook-agent/values-staging.yaml

helm-install-staging:
	helm install runbook-agent helm/k8s-runbook-agent/ \
		-n k8s-runbook-agent --create-namespace \
		-f helm/k8s-runbook-agent/values-staging.yaml

helm-install-prod:
	helm install runbook-agent helm/k8s-runbook-agent/ \
		-n k8s-runbook-agent --create-namespace \
		-f helm/k8s-runbook-agent/values-production.yaml

# --- Convenience ---
health:
	@curl -s http://localhost:8090/health | python3 -m json.tool

ready:
	@curl -s http://localhost:8090/ready | python3 -m json.tool

ready-agents:
	@curl -s http://localhost:8090/ready/agents | python3 -m json.tool

ready-agents-force:
	@curl -s "http://localhost:8090/ready/agents?force=true" | python3 -m json.tool

metrics:
	@curl -s http://localhost:8090/metrics

sessions:
	@curl -s http://localhost:8090/sessions | python3 -m json.tool

alert:
	@curl -s -X POST http://localhost:8090/webhooks/grafana \
		-H "Content-Type: application/json" \
		-d '{"alerts":[{"status":"firing","labels":{"alertname":"KubePodCrashLooping","namespace":"default","pod":"test-pod-abc","severity":"critical"},"annotations":{"summary":"Test pod crash looping"},"fingerprint":"test-'$$$$'"}]}' \
		| python3 -m json.tool

reload-runbooks:
	@curl -s -X POST http://localhost:8090/admin/runbooks/reload | python3 -m json.tool

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage
