# Overridable from the environment: `REGION=us-west-2 make plan`.
REGION  ?= us-east-1
CLUSTER ?= da51-lab

# Two tiers with separate state. core holds the VPC, buckets, registry and budget alarms
# and is applied once in week 1; cluster holds everything that bills by the hour and is
# created and destroyed every working session. Keeping them apart is what makes ~163
# cluster-hours cost ~16 USD instead of ~101.
CORE_DIR    ?= terraform/core
CLUSTER_DIR ?= terraform/cluster

TFC := terraform -chdir=$(CORE_DIR)
TF  := terraform -chdir=$(CLUSTER_DIR)

.DEFAULT_GOAL := help
.PHONY: help init fmt validate lint plan kubeconfig hooks \
	core-plan core-up core-down lab-up lab-down gpu gpu-l40s cost fix-cidr \
	vllm-up vllm-down smoke \
	monitoring-secret monitoring-up monitoring-down audit-metrics pf dashboards \
	cleanup-volumes orphans datasets datasets-check runner-image model-fetch

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

init: ## terraform init on both tiers
	$(TFC) init
	$(TF) init

core-plan: ## Show pending changes to the long-lived tier
	$(TFC) plan

core-up: ## Create the long-lived tier: VPC, buckets, ECR, budget alarms (once, week 1)
	$(TFC) apply

core-down: ## Destroy the long-lived tier. Week 6 only -- this deletes the artifacts bucket.
	@printf 'This destroys the VPC, the ECR repo and the ARTIFACTS BUCKET (weights, datasets,\n'
	@printf 'every benchmark run). Type DESTROY-CORE to continue: '; \
		read -r ans; [ "$$ans" = "DESTROY-CORE" ] || { echo "aborted"; exit 1; }
	$(TFC) destroy

lab-up: ## Start a working session: create the cluster tier, GPU still at 0
	$(TF) apply -var cluster_name=$(CLUSTER)
	@echo
	@echo "Cluster is up with both GPU node groups at 0."
	@echo "  make kubeconfig && make monitoring-up"
	@echo "  make gpu n=1     when you are ready to measure"

lab-down: ## End a session: snapshot metrics, release volumes, destroy the cluster tier
	@echo "Before destroying, confirm each of these:"
	@echo "  1. Prometheus time series for every run_id are exported to S3."
	@echo "  2. Benchmark results and raw run output are synced to S3."
	@echo "  3. Grafana dashboard changes worth keeping are committed to observability/."
	@echo
	@echo "Destroying loses all in-cluster state. Anything not on S3 is gone for good."
	@printf 'Type yes to destroy the cluster tier: '; \
		read -r ans; [ "$$ans" = "yes" ] || { echo "aborted"; exit 1; }
	@echo "releasing EBS volumes while the CSI driver still exists..."
	-@$(MAKE) --no-print-directory cleanup-volumes
	$(TF) destroy -var cluster_name=$(CLUSTER)
	@echo; echo "checking for volumes that outlived the cluster:"
	-@$(MAKE) --no-print-directory orphans

gpu: ## Scale the L4 node group: make gpu n=0|1|3
	@test -n "$(n)" || { echo "usage: make gpu n=0|1|3"; exit 1; }
	$(TF) apply -var cluster_name=$(CLUSTER) -var gpu_desired=$(n)

gpu-l40s: ## Scale the L40S comparison node group: make gpu-l40s n=0|1
	@test -n "$(n)" || { echo "usage: make gpu-l40s n=0|1"; exit 1; }
	$(TF) apply -var cluster_name=$(CLUSTER) -var gpu_l40s_desired=$(n)

fmt: ## Rewrite Terraform files to canonical format
	terraform fmt -recursive terraform/

validate: ## Validate both tiers without touching the backend
	$(TFC) init -backend=false -upgrade=false >/dev/null && $(TFC) validate
	$(TF)  init -backend=false -upgrade=false >/dev/null && $(TF)  validate

lint: ## tflint over both tiers
	tflint --init --config=$(CURDIR)/.tflint.hcl
	tflint --chdir=$(CORE_DIR) --config=$(CURDIR)/.tflint.hcl
	tflint --chdir=$(CLUSTER_DIR) --config=$(CURDIR)/.tflint.hcl

plan: ## Show the pending change set for the cluster tier
	$(TF) plan -var cluster_name=$(CLUSTER)

# No -auto-approve anywhere. Every target that mutates AWS stops on Terraform's own
# yes/no prompt, because the GPU node group is the single largest cost in this project.

# Destroying the cluster destroys Prometheus's storage with it. The checklist is
# not decoration -- the per-run time series are what explain a bottleneck, and
# they cannot be reconstructed without spending the GPU hours again.
# Cost Explorer bills ~$0.01 per request, so this is a command you run, not something
# you put on a loop. Zero-cost services are filtered out to keep the end-of-session glance
# short; a day with no spend at all prints nothing for that day.
cost: ## Show the last 7 days of spend, grouped by service
	@aws ce get-cost-and-usage \
		--time-period Start=$$(date -u -v-7d +%Y-%m-%d 2>/dev/null || date -u -d '7 days ago' +%Y-%m-%d),End=$$(date -u +%Y-%m-%d) \
		--granularity DAILY --metrics UnblendedCost \
		--group-by Type=DIMENSION,Key=SERVICE --output json \
	| jq -r '["DATE","SERVICE","USD"], (.ResultsByTime[] as $$d | $$d.Groups[] \
		| select((.Metrics.UnblendedCost.Amount|tonumber) > 0.005) \
		| [$$d.TimePeriod.Start, .Keys[0], (.Metrics.UnblendedCost.Amount|tonumber*100|round/100|tostring)]) \
		| @tsv' \
	| column -t -s "$$(printf '\t')"
	@printf 'total 7d: '
	@aws ce get-cost-and-usage \
		--time-period Start=$$(date -u -v-7d +%Y-%m-%d 2>/dev/null || date -u -d '7 days ago' +%Y-%m-%d),End=$$(date -u +%Y-%m-%d) \
		--granularity MONTHLY --metrics UnblendedCost --output json \
	| jq -r '[.ResultsByTime[].Total.UnblendedCost.Amount|tonumber]|add|.*100|round/100|"$$\(.)"'

# A residential connection hands out a new address whenever it reconnects, and the
# symptom is a kubectl that hangs and times out rather than a permission error -- it
# looks like a broken cluster, not a firewall. This makes the fix one command.
fix-cidr: ## Repoint allowed_cidrs at your current public IP, then apply
	@ip=$$(curl -s --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]'); \
		[ -n "$$ip" ] || { echo "could not determine public IP"; exit 1; }; \
		echo "current public IP: $$ip"; \
		sed -i.bak "s|^allowed_cidrs.*|allowed_cidrs = [\"$$ip/32\"]|" $(CLUSTER_DIR)/terraform.tfvars; \
		rm -f $(CLUSTER_DIR)/terraform.tfvars.bak; \
		grep allowed_cidrs $(CLUSTER_DIR)/terraform.tfvars
	$(TF) apply -var cluster_name=$(CLUSTER)


# --- vLLM workload ----------------------------------------------------------
# The manifests carry __PLACEHOLDERS__ instead of the IRSA role ARN and bucket name.
# Both are outputs of a cluster that gets rebuilt every session, so committing them
# would bake in a stale value and leak the account number. They are substituted here.
RENDER_DIR := .rendered/vllm

vllm-up: ## Render and apply the vLLM manifests (cluster must be up)
	@arn=$$($(TF) output -raw vllm_role_arn 2>/dev/null); \
	bkt=$$($(TF) output -raw artifacts_bucket_name 2>/dev/null); \
	if [ -z "$$arn" ] || [ -z "$$bkt" ]; then \
		echo "terraform outputs are empty -- is the cluster up? try: make lab-up"; \
		exit 1; \
	fi; \
	rm -rf $(RENDER_DIR); mkdir -p $(RENDER_DIR); \
	for f in k8s/vllm/*.yaml; do \
		sed -e "s|__VLLM_ROLE_ARN__|$$arn|g" -e "s|__ARTIFACTS_BUCKET__|$$bkt|g" \
			"$$f" > "$(RENDER_DIR)/$$(basename $$f)"; \
	done; \
	if grep -l '__[A-Z_]*__' $(RENDER_DIR)/*.yaml; then \
		echo "a placeholder was left unsubstituted in the files above"; exit 1; \
	fi; \
	kubectl apply -f $(RENDER_DIR)/
	@echo
	@echo "Applied. First start syncs 14.2 GiB from S3 and loads it onto the GPU;"
	@echo "allow up to 20 minutes. Watch with:"
	@echo "  kubectl -n inference get pod -w"
	@echo "  kubectl -n inference logs -f deploy/vllm-server -c fetch-weights"

vllm-down: ## Remove the vLLM workload but keep the cluster
	kubectl delete -f k8s/vllm/00-namespace.yaml --ignore-not-found

smoke: ## Prove the deployment answers correctly before measuring anything
	./bench/scripts/smoke.sh


# --- Monitoring stack -------------------------------------------------------
CHART_GPU_OPERATOR ?= v26.7.0
CHART_KPS          ?= 91.4.1

monitoring-secret: ## Generate the Grafana admin password into a Secret (printed once)
	@pw=$$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24); \
	kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	kubectl -n monitoring create secret generic grafana-admin \
		--from-literal=admin-user=admin \
		--from-literal=admin-password="$$pw" \
		--dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	echo "grafana admin / $$pw"; \
	echo "(shown once and never written to the repo -- store it in your password manager)"

monitoring-up: ## Install GPU Operator (DCGM only) + kube-prometheus-stack + rules
	helm repo add nvidia https://helm.ngc.nvidia.com/nvidia >/dev/null 2>&1 || true
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
	helm repo update >/dev/null
	@kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1 \
		|| $(MAKE) --no-print-directory monitoring-secret
	helm upgrade --install gpu-operator nvidia/gpu-operator \
		--version $(CHART_GPU_OPERATOR) -n gpu-operator --create-namespace \
		-f k8s/monitoring/gpu-operator-values.yaml --wait --timeout 15m
	helm upgrade --install kps prometheus-community/kube-prometheus-stack \
		--version $(CHART_KPS) -n monitoring --create-namespace \
		-f k8s/monitoring/kps-values.yaml --wait --timeout 15m
	kubectl apply -f k8s/monitoring/servicemonitor-vllm.yaml
	kubectl apply -f observability/rules/
	@$(MAKE) --no-print-directory dashboards
	@echo
	@echo "Next: 'make pf', then 'make audit-metrics' before measuring anything."

monitoring-down: ## Remove the monitoring stack, including its PVCs
	-helm uninstall kps -n monitoring
	-helm uninstall gpu-operator -n gpu-operator
	-kubectl -n monitoring delete pvc --all

dashboards: ## Load observability/dashboards/*.json into Grafana via ConfigMap
	@for f in observability/dashboards/*.json; do \
		name="vllm-dash-$$(basename $$f .json)"; \
		kubectl -n monitoring create configmap "$$name" --from-file="$$f" \
			--dry-run=client -o yaml \
		| kubectl label -f - --local -o yaml --dry-run=client grafana_dashboard=1 \
		| kubectl apply -f - ; \
	done
	@echo "Grafana's sidecar picks these up within ~60s. Dashboards are code here:"
	@echo "edit in the UI to explore, but export back to observability/dashboards/"
	@echo "or the change dies with the cluster."

audit-metrics: ## Confirm every metric the rules depend on exists by name
	./bench/scripts/audit_metrics.sh

pf: ## Port-forward Prometheus 9090, Grafana 3000, vLLM 8000
	@kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/dev/null 2>&1 &
	@kubectl -n monitoring port-forward svc/kps-grafana 3000:80 >/dev/null 2>&1 &
	@kubectl -n inference port-forward svc/vllm-svc 8000:8000 >/dev/null 2>&1 &
	@sleep 3; echo "prometheus :9090   grafana :3000   vllm :8000"
	@echo "stop with: pkill -f 'kubectl.*port-forward'"

# --- EBS hygiene ------------------------------------------------------------
# Dynamically provisioned volumes are deleted by the EBS CSI controller when their PVC
# goes away. `terraform destroy` removes the cluster -- and the controller -- without
# ever deleting the PVCs, so the volumes survive as detached, billable orphans. At two
# volumes a session and three sessions a week that is real money for nothing.
cleanup-volumes: ## Delete workload PVCs so the CSI driver releases their EBS volumes
	-kubectl delete namespace inference --ignore-not-found --timeout=5m
	-helm uninstall kps -n monitoring 2>/dev/null
	-kubectl -n monitoring delete pvc --all --timeout=5m

orphans: ## List detached EBS volumes that are still being billed
	@aws ec2 describe-volumes --region $(REGION) \
		--filters Name=status,Values=available \
		--query 'Volumes[].{id:VolumeId,GiB:Size,created:CreateTime,tags:Tags[?Key==`kubernetes.io/created-for/pvc/name`].Value|[0]}' \
		--output table
	@echo "Delete one with: aws ec2 delete-volume --volume-id vol-xxxx"


# --- Benchmark inputs and runner --------------------------------------------
DATASET_VERSION ?= v1
DATASET_N       ?= 2000
# `terraform output` prints its "No outputs found" warning to stdout as well as stderr,
# so 2>/dev/null does not make this empty when the cluster is down -- it makes it 540
# characters of warning text, backticks included. Anything downstream then believes it
# has a bucket name. Only a string shaped like a bucket name gets through.
# Read from CORE, not cluster: datasets and model weights are published while the
# cluster tier is down, and core is the tier that always exists.
ARTIFACTS       ?= $(shell $(TFC) output -raw artifacts_bucket_name 2>/dev/null \
                     | grep -Ex '[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]' || true)

model-fetch: ## Download model weights and publish them to S3 (resumable, retries)
	@test -n "$(ARTIFACTS)" || { echo "artifacts bucket unknown (is the cluster up?)"; exit 1; }
	@BUCKET="$(ARTIFACTS)" ./bench/scripts/fetch_model.sh

datasets: ## Generate the three reference datasets and publish them with checksums
	python3 bench/datasets/make_datasets.py --n $(DATASET_N) --version $(DATASET_VERSION)
	@test -n "$(ARTIFACTS)" || { echo "artifacts bucket unknown (is the cluster up?)"; exit 1; }
	aws s3 sync bench/datasets/ "s3://$(ARTIFACTS)/datasets/$(DATASET_VERSION)/" \
		--exclude "*" --include "*-$(DATASET_VERSION).jsonl" \
		--include "manifest-$(DATASET_VERSION).json"

datasets-check: ## Re-verify dataset checksums against the manifest
	python3 bench/datasets/make_datasets.py --check $(DATASET_VERSION)

runner-image: ## Build and push the bench runner to ECR
	@test -n "$(ARTIFACTS)" || { echo "cluster must be up to read account/region"; exit 1; }
	@acct=$$(aws sts get-caller-identity --query Account --output text); \
	reg=$$acct.dkr.ecr.$(REGION).amazonaws.com; \
	tag=$$(git rev-parse --short HEAD 2>/dev/null || echo dev); \
	aws ecr describe-repositories --repository-names vllm-bench/bench-runner --region $(REGION) >/dev/null 2>&1 \
		|| aws ecr create-repository --repository-name vllm-bench/bench-runner --region $(REGION) >/dev/null; \
	aws ecr get-login-password --region $(REGION) | docker login --username AWS --password-stdin $$reg; \
	docker build --platform linux/amd64 --build-arg GIT_COMMIT=$$tag \
		-t $$reg/vllm-bench/bench-runner:$$tag bench/runner; \
	docker push $$reg/vllm-bench/bench-runner:$$tag; \
	echo "RUNNER_IMAGE=$$reg/vllm-bench/bench-runner:$$tag"


kubeconfig: ## Point kubectl at the cluster
	aws eks update-kubeconfig --region $(REGION) --name $(CLUSTER)

hooks: ## Install the git pre-commit hooks
	pre-commit install --install-hooks
	pre-commit run --all-files || true
