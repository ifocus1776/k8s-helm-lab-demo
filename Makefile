# Kubernetes + Helm desktop lab. Run `make help` for targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV        ?= .venv
CHART       ?= charts/hello-lab
RELEASE     ?= hello-lab
NS          ?= demo
ENV         ?=              # dev | prod  -> adds -f $(CHART)/values-$(ENV).yaml
REPLICAS    ?=              # e.g. make upgrade REPLICAS=3
MESSAGE     ?=              # e.g. make upgrade MESSAGE="Hello v2"
REV         ?=              # e.g. make rollback REV=1 (default: previous revision)
DEPLOY      ?= false        # make lab-up DEPLOY=true also deploys the chart via Ansible
BECOME      ?= -K           # set BECOME= if you have passwordless sudo

ANSIBLE_PLAYBOOK := $(if $(wildcard $(VENV)/bin/ansible-playbook),$(abspath $(VENV))/bin/ansible-playbook,ansible-playbook)
ANSIBLE_GALAXY   := $(if $(wildcard $(VENV)/bin/ansible-galaxy),$(abspath $(VENV))/bin/ansible-galaxy,ansible-galaxy)
LINT_BIN         := $(if $(wildcard $(VENV)/bin/yamllint),$(abspath $(VENV))/bin/,)

VALUES_ARGS := $(if $(strip $(ENV)),-f $(CHART)/values-$(strip $(ENV)).yaml,)
SET_ARGS    := $(if $(strip $(REPLICAS)),--set replicaCount=$(strip $(REPLICAS)),) \
               $(if $(strip $(MESSAGE)),--set-string app.message="$(strip $(MESSAGE))",)

.PHONY: help bootstrap collections lab-up lab-check deploy upgrade rollback history status test \
        dev prod lab-down lint template qa clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

bootstrap: ## Create .venv with pinned ansible-core + lint tools, install collections
	sudo apt-get update && sudo apt-get install -y python3-venv python3-apt make
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt -r requirements-dev.txt
	$(MAKE) collections

collections: ## Install pinned Ansible collections into ansible/collections
	cd ansible && $(ANSIBLE_GALAXY) collection install -r requirements.yml -p ./collections

lab-up: ## Install k3s + kubectl + helm (and optionally the chart: DEPLOY=true)
	cd ansible && $(ANSIBLE_PLAYBOOK) site.yml $(BECOME) -e deploy_sample_app=$(strip $(DEPLOY))

lab-check: ## Dry run of the playbook (--check --diff)
	cd ansible && $(ANSIBLE_PLAYBOOK) site.yml $(BECOME) --check --diff -e deploy_sample_app=$(strip $(DEPLOY))

deploy: ## helm upgrade --install the chart (ENV=dev|prod optional)
	helm upgrade --install $(RELEASE) $(CHART) -n $(NS) --create-namespace $(VALUES_ARGS) $(SET_ARGS) --wait --timeout 3m

upgrade: ## helm upgrade with overrides, e.g. make upgrade REPLICAS=3 MESSAGE="v2"
	helm upgrade $(RELEASE) $(CHART) -n $(NS) $(VALUES_ARGS) $(SET_ARGS) --wait --timeout 3m

rollback: ## helm rollback to REV (default: previous revision)
	helm rollback $(RELEASE) $(REV) -n $(NS) --wait --timeout 3m

history: ## helm history of the release
	helm history $(RELEASE) -n $(NS)

status: ## Show release, pods, service, ingress
	helm status $(RELEASE) -n $(NS) | head -n 8
	kubectl -n $(NS) get deploy,pods,svc,ingress,hpa,pdb -o wide

test: ## Run the chart's helm test (smoke test)
	helm test $(RELEASE) -n $(NS)

dev: ## Deploy the dev environment (namespace dev, values-dev.yaml)
	$(MAKE) deploy RELEASE=hello-lab NS=dev ENV=dev

prod: ## Deploy the prod environment (namespace prod, values-prod.yaml)
	$(MAKE) deploy RELEASE=hello-lab NS=prod ENV=prod

qa: ## Run the QA console at http://127.0.0.1:8787
	python3 qa-lab/server.py

lab-down: ## Remove k3s completely (k3s-uninstall.sh via Ansible)
	cd ansible && $(ANSIBLE_PLAYBOOK) teardown.yml $(BECOME)

template: ## Render the chart for every environment to stdout
	@for f in "" values-dev.yaml values-prod.yaml; do \
	  echo "# ===== $${f:-values.yaml} ====="; \
	  helm template $(RELEASE) $(CHART) $${f:+-f $(CHART)/$$f}; done

lint: ## helm lint/template (+kubeconform), yamllint, ansible syntax-check + ansible-lint
	helm lint --strict $(CHART)
	helm lint --strict $(CHART) -f $(CHART)/values-dev.yaml
	helm lint --strict $(CHART) -f $(CHART)/values-prod.yaml
	@if command -v kubeconform >/dev/null; then \
	  for f in "" values-dev.yaml values-prod.yaml; do \
	    helm template $(RELEASE) $(CHART) $${f:+-f $(CHART)/$$f} | kubeconform -strict -summary -ignore-missing-schemas; done; \
	else echo "kubeconform not installed; skipping schema validation"; fi
	$(LINT_BIN)yamllint -s .
	cd ansible && $(ANSIBLE_PLAYBOOK) site.yml --syntax-check && $(ANSIBLE_PLAYBOOK) teardown.yml --syntax-check
	@if command -v $(LINT_BIN)ansible-lint >/dev/null; then cd ansible && $(LINT_BIN)ansible-lint; \
	else echo "ansible-lint not installed; skipping"; fi
	@if [ -d advanced-kubeadm/collections ]; then cd advanced-kubeadm && $(ANSIBLE_PLAYBOOK) site.yml --syntax-check \
	  && $(ANSIBLE_PLAYBOOK) reset.yml --syntax-check && { ! command -v $(LINT_BIN)ansible-lint >/dev/null || $(LINT_BIN)ansible-lint; }; \
	else echo "advanced-kubeadm: run 'ansible-galaxy collection install -r requirements.yml -p ./collections' there to lint it"; fi

clean: ## Remove local venv and downloaded collections
	rm -rf $(VENV) ansible/collections
