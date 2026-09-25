# Overridable from the environment:
# `AWS_PROFILE=vinai REGION=us-west-2 make plan`.
AWS_PROFILE ?= default
REGION      ?= us-east-1
CLUSTER     ?= da51-lab

# Both the AWS CLI and Terraform AWS provider inherit this profile in every recipe.
export AWS_PROFILE

# Three tiers with separate state. core owns the VPC/artifacts, data owns persistent
# Aurora, and cluster owns disposable EKS compute. `lab-down` can therefore remove the
# expensive serving cluster without deleting LiteLLM identity and usage state.
CORE_DIR    ?= terraform/core
DATA_DIR    ?= terraform/data
CLUSTER_DIR ?= terraform/cluster

TFC := terraform -chdir=$(CORE_DIR)
TFD := terraform -chdir=$(DATA_DIR)
TF  := terraform -chdir=$(CLUSTER_DIR)

.DEFAULT_GOAL := help
.PHONY: help init fmt validate lint plan kubeconfig hooks \
	core-plan core-up core-down data-plan data-up data-down agent-keys lab-up lab-down gpu gpu-l40s cost fix-cidr \
	vllm-up vllm-diff vllm-down smoke \
	guardrail-image guardrail-up guardrail-diff guardrail-down \
	litellm-secret litellm-up litellm-diff litellm-down litellm-smoke \
	monitoring-secret monitoring-up monitoring-down audit-metrics pf dashboards \
	snapshot cleanup-volumes orphans nodes-zero teardown-check kill-nodes datasets datasets-check runner-image model-fetch models-awq \
	rag-data rag-eval rag-eval-nopolicy guardrails-test \
	attacks-build attacks-score defenses-dryrun defenses-eval spotlight-cost \
	secrets-scan \
	dense-env dense-build dense-eval dense-ablation diagrams \
	ingress-up ingress-down ingress-url creds \
	availability embeddings-up embeddings-down tracing-up tracing-down trace tracing-check \
	cache-invalidate cache-clear

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

init: ## terraform init on core, data and cluster tiers
	$(TFC) init
	$(TFD) init
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

data-plan: ## Show pending Aurora changes (stateful tier)
	$(TFD) plan

data-down: ## Destroy the Aurora tier. Runs inside lab-down; virtual keys go with it.
# docs/litellm-aurora-implementation.md says there is deliberately no data-down, because
# Aurora holds LiteLLM state and must be protected from an accidental delete. That was the
# right call for a database meant to outlive the cluster. It is the wrong call here, and
# the difference is arithmetic: Aurora bills by the hour with no scale-to-zero, so left
# running it costs ~37 USD over the three weeks left against 182 USD of remaining budget.
# Session-scoped it costs about 5.
#
# What makes that trade safe is bench/scripts/provision_keys.py: the only state worth
# keeping is seven virtual keys, and they are regenerated from bench/agents.json in one
# command. If keys ever need to be stable -- a real pilot with DA clients configured
# against them -- set deletion_protection back to true in terraform/data/terraform.tfvars,
# drop this from lab-down, and pay the 37 USD.
	$(TFD) destroy -var-file=terraform.tfvars

data-up: ## Create/update Aurora PostgreSQL (session-scoped; see terraform/data/terraform.tfvars)
	$(TFD) apply
	@echo
	@echo "Aurora is ready. Its password is managed by RDS in Secrets Manager."
	@echo "Next: make lab-up && make kubeconfig && make litellm-up"

lab-up: ## Start a working session: create the cluster tier, GPU still at 0
	$(TF) apply -var cluster_name=$(CLUSTER)
	@echo
	@echo "Cluster is up with both GPU node groups at 0."
	@echo "  make kubeconfig && make monitoring-up"
	@echo "  make gpu n=1     when you are ready to measure"

snapshot: ## Export this session's time series to s3://<artifacts>/runs/. HOURS=12 LABEL=
	@python3 bench/scripts/snapshot.py --hours "$(or $(HOURS),12)" --label "$(LABEL)"

lab-down: ## End a session: snapshot metrics, release volumes, destroy the cluster tier
# The snapshot runs here rather than sitting in a checklist. It used to be item 1 of three
# lines of text followed by a yes/no prompt, with nothing in the repo that could perform
# it -- so the honest description of the old behaviour was "type yes to delete your
# measurements". Prometheus stores them in a PVC that cleanup-volumes deletes two steps
# below, and reproducing them costs GPU hours.
	@echo "exporting this session's time series before anything is deleted..."
	@$(MAKE) --no-print-directory snapshot; rc=$$?; \
		if [ $$rc -eq 2 ]; then \
			echo "  khong tim thay Prometheus -- khong co gi de xuat, di tiep"; \
		elif [ $$rc -ne 0 ]; then \
			echo; \
			echo "SNAPSHOT THAT BAI trong khi Prometheus van song."; \
			echo "Destroy bay gio la mat du lieu do duoc."; \
			echo "Chay 'make snapshot' de xem loi, hoac ep bo qua bang:"; \
			echo "  make lab-down SKIP_SNAPSHOT=1"; \
			test -n "$(SKIP_SNAPSHOT)"; \
		fi
	@echo
	@echo "Still worth confirming by hand:"
	@echo "  1. Benchmark results and raw run output are synced to S3."
	@echo "  2. Grafana dashboard changes worth keeping are committed to observability/."
	@echo
	@echo "Destroying loses all in-cluster state. Anything not on S3 is gone for good."
	@printf 'Type yes to destroy the cluster tier: '; \
		read -r ans; [ "$$ans" = "yes" ] || { echo "aborted"; exit 1; }
	@echo "deleting the load balancer before the cluster that owns it..."
	-@$(MAKE) --no-print-directory ingress-down
	@echo "releasing EBS volumes while the CSI driver still exists..."
	-@$(MAKE) --no-print-directory cleanup-volumes
	@echo "scaling every node group to zero BEFORE destroy..."
	-@$(MAKE) --no-print-directory nodes-zero
	$(TF) destroy -var cluster_name=$(CLUSTER)
# After the cluster, not before: LiteLLM holds connections to Aurora, and tearing the
# database out from under a running gateway produces a crash loop rather than a clean
# stop. Nothing reads the database once the cluster is gone.
	@echo; echo "destroying the session-scoped Aurora tier..."
	-@$(MAKE) --no-print-directory data-down
	@echo; echo "checking for volumes that outlived the cluster:"
	-@$(MAKE) --no-print-directory orphans
	-@$(MAKE) --no-print-directory teardown-check

# Scale to zero BEFORE destroying, not as part of it.
#
# A managed node group is an autoscaling group underneath, and a destroy that fails
# partway leaves that ASG behind still wanting its desired count. Terminating those
# instances by hand achieves nothing -- the ASG replaces them within a minute, which is
# what "EC2 keeps creating instances in a loop" actually is. Scaling to zero first means a
# failed destroy leaves nothing running and nothing being replaced: the billing stops even
# when the cleanup does not finish.
nodes-zero: ## Scale every node group to 0 and wait for the instances to go
	@ngs=$$(aws eks list-nodegroups --cluster-name $(CLUSTER) --region $(REGION) \
		--query 'nodegroups' --output text 2>/dev/null); \
	if [ -z "$$ngs" ]; then echo "  khong co node group nao (cum da xoa?)"; exit 0; fi; \
	for ng in $$ngs; do \
		echo "  ha $$ng ve 0 ..."; \
		aws eks update-nodegroup-config --cluster-name $(CLUSTER) --region $(REGION) \
			--nodegroup-name "$$ng" --scaling-config minSize=0,desiredSize=0 \
			--query 'update.status' --output text >/dev/null 2>&1 \
			|| echo "    (khong ha duoc -- co the dang co update khac chay)"; \
	done; \
	echo "  cho instance tat (toi da 5 phut)..."; \
	for i in $$(seq 1 30); do \
		n=$$(aws ec2 describe-instances --region $(REGION) \
			--filters "Name=tag:eks:cluster-name,Values=$(CLUSTER)" \
				"Name=instance-state-name,Values=running,pending" \
			--query 'length(Reservations[].Instances[])' --output text 2>/dev/null); \
		if [ "$$n" = "0" ]; then echo "  khong con instance nao"; break; fi; \
		printf '  con %s instance...\n' "$$n"; sleep 10; \
	done

# `orphans` only looks at EBS volumes. The expensive survivor is a node group whose ASG
# keeps relaunching instances, and nothing checked for that until it had happened.
teardown-check: ## Verify nothing survived: node group, autoscaling group, EC2
	@echo "kiem tra sau teardown:"
	@ng=$$(aws eks list-nodegroups --cluster-name $(CLUSTER) --region $(REGION) \
		--query 'nodegroups' --output text 2>/dev/null); \
		if [ -z "$$ng" ]; then echo "  node group      : khong con"; \
		else echo "  node group      : VAN CON -> $$ng"; fi
	@asg=$$(aws autoscaling describe-auto-scaling-groups --region $(REGION) \
		--query "AutoScalingGroups[?contains(AutoScalingGroupName,'$(CLUSTER)')].AutoScalingGroupName" \
		--output text 2>/dev/null); \
		if [ -z "$$asg" ]; then echo "  autoscaling grp : khong con"; \
		else echo "  autoscaling grp : VAN CON -> $$asg   <-- thu nay tao lai EC2"; fi
	@n=$$(aws ec2 describe-instances --region $(REGION) \
		--filters "Name=tag:eks:cluster-name,Values=$(CLUSTER)" \
			"Name=instance-state-name,Values=running,pending" \
		--query 'length(Reservations[].Instances[])' --output text 2>/dev/null); \
		if [ "$$n" = "0" ]; then echo "  EC2             : khong con"; \
		else echo "  EC2             : VAN CON $$n instance"; fi

# For when the loop is happening right now and the billing has to stop before anyone
# works out why. Scaling to zero is the fix; terminating the instances is not, because
# that is precisely the thing the ASG undoes.
kill-nodes: ## Phanh khan cap: ha moi node group ve 0 ngay, khong destroy
	@$(MAKE) --no-print-directory nodes-zero
	@$(MAKE) --no-print-directory teardown-check

# Scaling goes through the AWS API, not Terraform.
#
# terraform-aws-modules/eks sets `ignore_changes = [scaling_config[0].desired_size]` on
# every managed node group, so Terraform deliberately never touches desired_size after
# creation -- the field is left to a cluster autoscaler. `terraform apply -var
# gpu_desired=1` therefore reports "0 changed" and silently does nothing, which is the
# worst possible failure for the one command that controls the project's largest cost.
#
# Because Terraform ignores the field, changing it out of band creates no drift.
gpu: ## Scale the L4 node group: make gpu n=0|1|3
	@test -n "$(n)" || { echo "usage: make gpu n=0|1|3"; exit 1; }
	@ng=$$(aws eks list-nodegroups --cluster-name $(CLUSTER) --region $(REGION) \
		--query "nodegroups[?starts_with(@,'gpu-2')]|[0]" --output text); \
	echo "scaling $$ng to $(n) ..."; \
	aws eks update-nodegroup-config --cluster-name $(CLUSTER) --region $(REGION) \
		--nodegroup-name "$$ng" --scaling-config minSize=0,maxSize=3,desiredSize=$(n) \
		--query 'update.status' --output text
	@echo "a new node needs ~3-4 min to join, plus 1-3 min to sync weights from S3."
	@echo "watch: kubectl get nodes -l workload=inference -w"

gpu-l40s: ## Scale the L40S comparison node group: make gpu-l40s n=0|1
	@test -n "$(n)" || { echo "usage: make gpu-l40s n=0|1"; exit 1; }
	@ng=$$(aws eks list-nodegroups --cluster-name $(CLUSTER) --region $(REGION) \
		--query "nodegroups[?starts_with(@,'gpu-l40s')]|[0]" --output text); \
	echo "scaling $$ng to $(n) ..."; \
	aws eks update-nodegroup-config --cluster-name $(CLUSTER) --region $(REGION) \
		--nodegroup-name "$$ng" --scaling-config minSize=0,maxSize=1,desiredSize=$(n) \
		--query 'update.status' --output text

fmt: ## Rewrite Terraform files to canonical format
	terraform fmt -recursive terraform/

validate: ## Validate all three tiers without touching the backend
	$(TFC) init -backend=false -upgrade=false >/dev/null && $(TFC) validate
	$(TFD) init -backend=false -upgrade=false >/dev/null && $(TFD) validate
	$(TF)  init -backend=false -upgrade=false >/dev/null && $(TF)  validate

lint: ## tflint over all three tiers
	tflint --init --config=$(CURDIR)/.tflint.hcl
	tflint --chdir=$(CORE_DIR) --config=$(CURDIR)/.tflint.hcl
	tflint --chdir=$(DATA_DIR) --config=$(CURDIR)/.tflint.hcl
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
fix-cidr: ## Repoint the cluster at your current public IP after an ISP address change
# Goes through the EKS API on purpose, not `terraform apply`. The cluster state holds
# kubernetes_storage_class_v1, which Terraform must read through the Kubernetes API --
# and that API is exactly what a stale CIDR blocks. Applying to fix the lockout therefore
# deadlocks on the lockout. The EKS control-plane API stays reachable, so it can.
# The tfvars edit afterwards keeps Terraform's config in step with reality.
	@ip=$$(curl -s --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]'); \
		[ -n "$$ip" ] || { echo "could not determine public IP"; exit 1; }; \
		cur=$$(aws eks describe-cluster --name $(CLUSTER) \
			--query 'cluster.resourcesVpcConfig.publicAccessCidrs' --output text); \
		echo "public IP : $$ip"; echo "on cluster: $$cur"; \
		if [ "$$cur" = "$$ip/32" ]; then echo "already current -- nothing to do"; exit 0; fi; \
		q() { aws eks describe-cluster --name $(CLUSTER) \
			--query "cluster.resourcesVpcConfig.$$1" --output text; }; \
		pub=$$(q endpointPublicAccess); priv=$$(q endpointPrivateAccess); \
		id=$$(aws eks update-cluster-config --name $(CLUSTER) \
			--resources-vpc-config "endpointPublicAccess=$$pub,endpointPrivateAccess=$$priv,publicAccessCidrs=$$ip/32" \
			--query 'update.id' --output text); \
		echo "update $$id -- takes a few minutes"; \
		until [ "$$(aws eks describe-update --name $(CLUSTER) --update-id $$id \
			--query 'update.status' --output text)" != "InProgress" ]; do sleep 15; done; \
		st=$$(aws eks describe-update --name $(CLUSTER) --update-id $$id \
			--query 'update.status' --output text); \
		[ "$$st" = "Successful" ] || { echo "update ended $$st"; exit 1; }; \
		sed -i.bak "s|^allowed_cidrs.*|allowed_cidrs = [\"$$ip/32\"]|" $(CLUSTER_DIR)/terraform.tfvars; \
		rm -f $(CLUSTER_DIR)/terraform.tfvars.bak; \
		grep allowed_cidrs $(CLUSTER_DIR)/terraform.tfvars; \
		kubectl get --raw /healthz && echo " -- API reachable again"


# --- vLLM workload ----------------------------------------------------------
# MODE picks which models run and how the card is divided:
#   shared  both models on one GPU  -- the lab default and the pilot configuration
#   solo-a  class A alone, whole card -- produces X_A for production sizing
#   solo-b  class B alone, whole card -- produces X_B
#
# Production gives each model its own node group, so the numbers that go into the GPU
# count formula must come from the solo modes. Measuring only `shared` would size
# production for an architecture nobody intends to deploy.
MODE ?= shared

# Which weights to serve: awq (4-bit) or none (the original FP16 checkpoints).
#
# awq is the default because `shared` is the lab's normal mode and FP16 does not fit in
# it: model A alone needs 14.2 GiB against a 14.6 GiB share of the card. The chart now
# refuses that combination at render time with the arithmetic printed, instead of letting
# it fail as an allocation error partway into a measurement window.
#
# Use QUANT=none for the solo runs that produce the production sizing number -- a
# throughput figure from 4-bit weights is not comparable with one from FP16.
QUANT ?= awq

vllm-up: ## Install/upgrade vLLM. MODE=shared|solo-a|solo-b  QUANT=awq|none
# `terraform output -raw` on a destroyed tier exits 0 and prints a "No outputs found"
# warning, in colour, to stdout. So neither `|| echo PLACEHOLDER` nor a plain -z test
# fires: the variable ends up holding ANSI escape sequences, which reach the chart and
# come back as "YAML parse error: control characters are not allowed" pointing at a
# template that is fine. Match the shape of the value instead of trusting the exit code
# -- the same guard litellm-secret and ARTIFACTS already use.
	@arn=$$($(TF) output -raw vllm_role_arn 2>/dev/null \
		| grep -E '^arn:aws:iam::[0-9]{12}:role/' || true); \
	bkt=$$($(TFC) output -raw artifacts_bucket_name 2>/dev/null \
		| grep -Ex '[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]' || true); \
	if [ -z "$$arn" ] || [ -z "$$bkt" ]; then \
		echo "terraform outputs are empty -- is the cluster up? try: make lab-up"; \
		exit 1; \
	fi; \
	helm upgrade --install vllm charts/vllm \
		-n inference --create-namespace \
		--set mode=$(MODE) \
		--set quantization=$(QUANT) \
		--set artifactsBucket="$$bkt" \
		--set roleArn="$$arn" \
		--wait --timeout 25m
	@echo
	@echo "mode=$(MODE) quantization=$(QUANT). A cold node syncs weights from S3 first;"
	@echo "allow up to 20 minutes. Watch with:"
	@echo "  kubectl -n inference get pod -w"
	@echo "  kubectl -n inference logs -f deploy/vllm-a -c fetch-weights"

vllm-diff: ## Render the chart without applying it. MODE=shared|solo-a|solo-b QUANT=awq|none
	@arn=$$($(TF) output -raw vllm_role_arn 2>/dev/null \
		| grep -E '^arn:aws:iam::[0-9]{12}:role/' || echo PLACEHOLDER); \
	bkt=$$($(TFC) output -raw artifacts_bucket_name 2>/dev/null \
		| grep -Ex '[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]' || echo PLACEHOLDER); \
	helm template vllm charts/vllm -n inference \
		--set mode=$(MODE) --set quantization=$(QUANT) \
		--set artifactsBucket="$$bkt" --set roleArn="$$arn"

smoke: ## Seven checks that separate "cluster broken" from "measurement bad". CLASS=a|b
	@CLASS=$(or $(CLASS),a) ./bench/scripts/smoke.sh

vllm-down: ## Remove the vLLM release but keep the cluster
	-helm uninstall vllm -n inference
	-kubectl delete namespace inference --ignore-not-found

# --- Guardrail serving hop -------------------------------------------------
# The image contains the same pipeline exercised by `make guardrails-test`. LiteLLM
# talks to this OpenAI-compatible service, which prepares the RAG prompt, calls vLLM,
# and releases the answer only after the output checks pass.
# Hash the actual build inputs, including uncommitted files. Tagging only with HEAD would
# put changed source under an old immutable ECR tag and make the next push fail.
GUARDRAIL_TAG ?= src-$(shell find guardrails prompt rag services/llm_pipeline \
	data/xanhsm_retrieval_mock/corpus/retrieval_corpus.jsonl \
	-type f ! -path '*/__pycache__/*' ! -name '*.pyc' \
	| LC_ALL=C sort | xargs git hash-object | git hash-object --stdin | cut -c1-12)

guardrail-image: ## Build and push the guardrail service image to the core ECR repository
	@repo=$$($(TFC) output -raw ecr_guardrail_url 2>/dev/null \
		| grep -E '^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/' || true); \
	[ -n "$$repo" ] || { echo "guardrail ECR output is empty -- review/apply the core tier first"; exit 1; }; \
	reg=$${repo%%/*}; \
	aws ecr get-login-password --region $(REGION) \
		| docker login --username AWS --password-stdin "$$reg"; \
	docker build --platform linux/amd64 -f services/llm_pipeline/Dockerfile \
		-t "$$repo:$(GUARDRAIL_TAG)" .; \
	docker push "$$repo:$(GUARDRAIL_TAG)"; \
	echo "GUARDRAIL_IMAGE=$$repo:$(GUARDRAIL_TAG)"

guardrail-up: ## Install/upgrade the OpenAI-compatible guardrail service
	@repo=$$($(TFC) output -raw ecr_guardrail_url 2>/dev/null \
		| grep -E '^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/' || true); \
	[ -n "$$repo" ] || { echo "guardrail ECR output is empty -- review/apply the core tier first"; exit 1; }; \
	helm upgrade --install guardrail charts/guardrail \
		-n llm-serving --create-namespace \
		--set image.repository="$$repo" --set image.tag="$(GUARDRAIL_TAG)" \
		--wait --timeout 5m

guardrail-diff: ## Render guardrail manifests without applying them
	helm template guardrail charts/guardrail -n llm-serving \
		--set image.repository=PLACEHOLDER --set image.tag=$(GUARDRAIL_TAG)

guardrail-down: ## Remove the guardrail release but keep the namespace
	-helm uninstall guardrail -n llm-serving

# --- LiteLLM gateway --------------------------------------------------------
# Master/salt keys stay in a Kubernetes Secret and never pass through Helm values or
# git. DATABASE_URL can override the Aurora URL resolved from the data-tier outputs.
litellm-secret: ## Create/update LiteLLM secrets from Aurora (or DATABASE_URL override)
	@kubectl create namespace llm-serving --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@master=$$(kubectl -n llm-serving get secret litellm-secrets \
		-o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | base64 -d || true); \
	salt=$$(kubectl -n llm-serving get secret litellm-secrets \
		-o jsonpath='{.data.LITELLM_SALT_KEY}' 2>/dev/null | base64 -d || true); \
	[ -n "$$master" ] || master="sk-$$(openssl rand -hex 24)"; \
	[ -n "$$salt" ] || salt="sk-$$(openssl rand -hex 24)"; \
	db="$${DATABASE_URL:-}"; \
	if [ -z "$$db" ]; then \
		endpoint=$$($(TFD) output -raw aurora_writer_endpoint 2>/dev/null \
			| grep -E '^[A-Za-z0-9.-]+$$' || true); \
		port=$$($(TFD) output -raw aurora_port 2>/dev/null | grep -E '^[0-9]+$$' || true); \
		name=$$($(TFD) output -raw database_name 2>/dev/null \
			| grep -E '^[A-Za-z][A-Za-z0-9_]*$$' || true); \
		secret_arn=$$($(TFD) output -raw master_user_secret_arn 2>/dev/null \
			| grep '^arn:aws:secretsmanager:' || true); \
		if [ -n "$$endpoint" ] && [ -n "$$port" ] && [ -n "$$name" ] && [ -n "$$secret_arn" ]; then \
			payload=$$(aws secretsmanager get-secret-value --region $(REGION) \
				--secret-id "$$secret_arn" --query SecretString --output text); \
			user=$$(printf '%s' "$$payload" | jq -r '.username'); \
			pass=$$(printf '%s' "$$payload" | jq -r '.password'); \
			user_uri=$$(jq -rn --arg v "$$user" '$$v|@uri'); \
			pass_uri=$$(jq -rn --arg v "$$pass" '$$v|@uri'); \
			db="postgresql://$$user_uri:$$pass_uri@$$endpoint:$$port/$$name?sslmode=require"; \
		fi; \
	fi; \
	if [ -z "$$db" ] && [ -z "$(ALLOW_NO_DB)" ]; then \
		echo "DATABASE_URL unavailable -- run 'make data-up' or export DATABASE_URL,"; \
		echo "or run without one:  make litellm-up ALLOW_NO_DB=1"; \
		echo "Without a database LiteLLM still routes, authenticates with the master key"; \
		echo "and exports Prometheus metrics. What is lost: virtual keys, per-agent"; \
		echo "budgets and spend accounting -- so no per-agent labels either."; \
		exit 1; \
	fi; \
	set -- --from-literal=LITELLM_MASTER_KEY="$$master" \
		--from-literal=LITELLM_SALT_KEY="$$salt"; \
	if [ -n "$$db" ]; then set -- "$$@" --from-literal=DATABASE_URL="$$db"; \
	else echo "  chay KHONG co database: mat virtual key, budget va spend tracking"; fi; \
	kubectl -n llm-serving create secret generic litellm-secrets "$$@" \
		--dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@echo "LiteLLM secret is ready (existing master/salt keys were preserved)."

litellm-up: litellm-secret guardrail-up ## Install guardrail + LiteLLM. MODE=shared|solo-a|solo-b
	helm upgrade --install litellm charts/litellm \
		-n llm-serving --create-namespace --set mode=$(MODE) \
		--wait --timeout 5m
	@echo
	@echo "LiteLLM routes mode=$(MODE) through guardrail:8080 to the matching vLLM service."
	@echo "Run 'make litellm-smoke' after the matching vLLM deployment is ready."

litellm-diff: ## Render LiteLLM without applying it. MODE=shared|solo-a|solo-b
	helm template litellm charts/litellm -n llm-serving --set mode=$(MODE)

litellm-down: ## Remove LiteLLM and its in-cluster secrets
	-helm uninstall litellm -n llm-serving
	-kubectl delete namespace llm-serving --ignore-not-found

agent-keys: ## Create/refresh one virtual key per agent in bench/agents.json
# The price of a session-scoped Aurora, and the reason that trade is affordable: the keys
# die with the database every evening and come back from one file in one command.
#
# This is also what the per-agent acceptance criterion actually depends on. LiteLLM
# v1.90.2 does not fill `end_user` from the OpenAI `user` field -- measured on this
# deployment -- so no amount of dashboard work produces a per-agent breakdown until the
# requests arrive under a virtual key.
	@set -e; \
	base=$${LITELLM_URL:-}; \
	if [ -z "$$base" ]; then \
		if curl -fsS --max-time 2 http://127.0.0.1:4000/health/readiness >/dev/null 2>&1; then \
			base=http://127.0.0.1:4000; \
		else \
			ip=$$(kubectl -n monitoring get ingress grafana \
				-o jsonpath='{.spec.rules[0].host}' 2>/dev/null \
				| sed 's/^grafana\.//; s/\.nip\.io$$//'); \
			[ -n "$$ip" ] || { echo "khong tim thay LiteLLM -- 'make pf' hoac 'make ingress-up'"; exit 1; }; \
			base=http://llm.$$ip.nip.io:30080; \
		fi; \
	fi; \
	key=$$(kubectl -n llm-serving get secret litellm-secrets \
		-o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | base64 -d); \
	[ -n "$$key" ] || { echo "khong doc duoc master key"; exit 1; }; \
	PYTHONPATH=. python3 bench/scripts/provision_keys.py \
		--base-url "$$base" --master-key "$$key" $(if $(DRY_RUN),--dry-run,)

litellm-smoke: ## Verify auth, model routing, completion and streaming via LiteLLM
	@MODEL=$(if $(filter solo-b,$(MODE)),qwen2.5-1.5b,qwen2.5-7b) \
		./bench/scripts/smoke_litellm.sh

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
	kubectl apply -f k8s/monitoring/servicemonitor-dcgm.yaml
	kubectl apply -f k8s/monitoring/servicemonitor-vllm.yaml
	@kubectl create namespace llm-serving --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	kubectl apply -f k8s/monitoring/servicemonitor-litellm.yaml
	kubectl apply -f k8s/monitoring/servicemonitor-guardrail.yaml
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

# --- Tracing ----------------------------------------------------------------
# Metrics say the p95 moved; a trace says which of the ten guardrail stages moved it, on
# which request. Tempo is read inside Grafana, so this adds no hostname and no password.
availability: ## Bao cao availability tu ket qua probe. PROBES=<thu muc hoac tep>
# Doc nhieu phien cung luc: moi phien lam viec la mot mau, va 14 phien phu duoc 14 ngay
# lich ma mot lan chay lien tuc 2 tuan khong phu duoc -- no chi phu mot lan trien khai.
	@test -n "$(PROBES)" || { echo "usage: make availability PROBES=runs/probe/"; exit 1; }
	@PYTHONPATH=. python3 bench/scripts/availability.py $(PROBES) \
		$(if $(TARGET),--target $(TARGET),) $(if $(ALL_HOURS),--all-hours,)

embeddings-up: ## Install text-embeddings-inference, the query half of hybrid retrieval
	kubectl apply -f k8s/embeddings/tei.yaml
	@echo "first start downloads a ~2.2 GB model; readiness allows ten minutes."
	kubectl -n llm-serving rollout status deploy/tei --timeout=12m
	@echo
	@echo "Then turn the guardrail on to use it:"
	@echo "  helm upgrade guardrail charts/guardrail --reuse-values --set dense.enabled=true \\"
	@echo "    --set semanticCache.embeddingEndpoint=http://tei.llm-serving.svc.cluster.local"
	@echo "The image must have been built AFTER 'make dense-build', or it carries no index."

embeddings-down: ## Remove the embeddings service; retrieval falls back to lexical
	kubectl delete -f k8s/embeddings/tei.yaml --ignore-not-found

cache-invalidate: ## Remove cached answers that depended on DOC=<document_id>
	@test -n "$(DOC)" || { echo "DOC=<document_id> is required"; exit 1; }
	kubectl -n llm-serving exec deploy/guardrail -c guardrail -- \
		python -m services.llm_pipeline.cache_admin invalidate --document-id "$(DOC)"

cache-clear: ## Clear all semantic response-cache keys
	kubectl -n llm-serving exec deploy/guardrail -c guardrail -- \
		python -m services.llm_pipeline.cache_admin clear

tracing-up: ## Install Tempo and point Grafana at it
	kubectl apply -f k8s/tracing/tempo.yaml
	kubectl -n monitoring rollout status deploy/tempo --timeout=5m
	@echo
	@echo "The Tempo datasource is provisioned by k8s/monitoring/kps-values.yaml. If this"
	@echo "is the first time, Grafana needs it applied:  make monitoring-up"
	@echo
	@echo "Guardrail and LiteLLM send spans only if they were installed with tracing on,"
	@echo "which is the chart default. If they were already running, restart them:"
	@echo "  kubectl -n llm-serving rollout restart deploy/guardrail deploy/litellm"
	@echo
	@echo "Then:  make trace"

tracing-down: ## Remove Tempo. Traces are in an emptyDir, so they go with it.
	kubectl delete -f k8s/tracing/tempo.yaml --ignore-not-found

trace: ## Send one request and print the link that opens its trace. Q= MODEL= DIRECT=1
	@MODEL="$(MODEL)" Q="$(Q)" AGENT="$(AGENT)" MAX_TOKENS="$(MAX_TOKENS)" \
		DIRECT="$(DIRECT)" ./bench/scripts/trace_request.sh

tracing-check: ## Is anything actually being exported? Reads both ends.
	@echo "--- guardrail: spans no da gui ---"
	@kubectl -n llm-serving exec deploy/guardrail -- \
		python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())" \
		2>/dev/null | grep guardrail_trace_spans_total || echo "  (khong co -- tracing dang tat)"
	@echo "--- tempo: spans da nhan ---"
	@kubectl -n monitoring exec deploy/tempo -- \
		wget -qO- http://127.0.0.1:3200/metrics 2>/dev/null \
		| grep -E "^tempo_distributor_spans_received_total|^tempo_receiver_accepted_spans" \
		|| echo "  (khong doc duoc -- tempo chay chua?)"

pf: ## Port-forward Prometheus 9090, Grafana 3000, LiteLLM 4000, guardrail 8080, vLLM 8000
	@kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/dev/null 2>&1 &
	@kubectl -n monitoring port-forward svc/kps-grafana 3000:80 >/dev/null 2>&1 &
	-@kubectl -n llm-serving port-forward svc/litellm-private 4000:4000 >/dev/null 2>&1 &
	-@kubectl -n llm-serving port-forward svc/guardrail 8080:8080 >/dev/null 2>&1 &
	@kubectl -n inference port-forward svc/vllm-a 8000:8000 >/dev/null 2>&1 &
	-@kubectl -n inference port-forward svc/vllm-b 8001:8000 >/dev/null 2>&1 &
	@sleep 3; echo "prometheus :9090   grafana :3000   LiteLLM :4000   guardrail :8080   vLLM A :8000   vLLM B :8001"
	@echo "stop with: pkill -f 'kubectl.*port-forward'"

# --- Shared ingress ---------------------------------------------------------
# `make pf` tunnels through the Kubernetes API server: free, encrypted, authenticated by
# your IAM identity -- but it dies with the terminal and adds a home-to-us-east-1 round
# trip to anything measured through it. The ingress exists for the week-6 demo and for
# anyone who is not sitting at this laptop.
ingress-up: ## Publish the UIs. EXTRA_CIDRS=a/32,b/32 adds people; PUBLIC=1 opens to all
	@EXTRA_CIDRS="$(EXTRA_CIDRS)" PUBLIC="$(PUBLIC)" k8s/ingress/up.sh

ingress-down: ## Remove the ingress and close the port it opened on the node
	@REGION=$(REGION) k8s/ingress/down.sh

ingress-url: ## Reprint the published URLs
	@ip=$$(kubectl -n monitoring get ingress grafana \
		-o jsonpath='{.spec.rules[0].host}' 2>/dev/null \
		| sed 's/^grafana\.//; s/\.nip\.io$$//'); \
		[ -n "$$ip" ] || { echo "no ingress yet -- run: make ingress-up"; exit 1; }; \
		echo "  Grafana       http://grafana.$$ip.nip.io:30080"; \
		echo "  Prometheus    http://prometheus.$$ip.nip.io:30080"; \
		echo "  Alertmanager  http://alertmanager.$$ip.nip.io:30080"; \
		echo "  LiteLLM       http://llm.$$ip.nip.io:30080/v1/models"; \
		echo "  vLLM          http://vllm.$$ip.nip.io:30080/v1/models"; \
		echo; \
		echo "  /etc/hosts:  $$ip  grafana.da51.lab prometheus.da51.lab alertmanager.da51.lab llm.da51.lab vllm.da51.lab"; \
		echo; \
		cur=$$(curl -s --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]'); \
		sg=$$(aws ec2 describe-security-groups --region $(REGION) \
			--filters "Name=ip-permission.from-port,Values=30080" \
			--query 'SecurityGroups[0].GroupId' --output text 2>/dev/null); \
		allowed=$$(aws ec2 describe-security-groups --region $(REGION) --group-ids $$sg \
			--query "SecurityGroups[0].IpPermissions[?FromPort==\`30080\`].IpRanges[].CidrIp" \
			--output text 2>/dev/null); \
		echo "  cho phep: $$allowed"; \
		case " $$allowed " in \
			*" 0.0.0.0/0 "*) echo "  MO CONG KHAI -- bat ky ai cung vao duoc ba dashboard";; \
			*" $$cur/32 "*) ;; \
			*) echo "  IP cua ban ($$cur) KHONG nam trong danh sach -- chay lai: make ingress-up";; \
		esac

creds: ## Print the lab passwords
	@printf '  Grafana       admin / %s\n' \
		"$$(kubectl -n monitoring get secret grafana-admin \
			-o jsonpath='{.data.admin-password}' | base64 -d)"
	@printf '  Ingress auth  admin / %s\n' \
		"$$(kubectl -n monitoring get secret ingress-basic-auth \
			-o jsonpath='{.data.password}' 2>/dev/null | base64 -d || echo '(none yet)')"
	@printf '  LiteLLM key   %s\n' \
		"$$(kubectl -n llm-serving get secret litellm-secrets \
			-o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | base64 -d || echo '(none yet)')"

# --- Guardrails + RAG -------------------------------------------------------
# Everything here runs on CPU with no cluster and no model download, so it stays
# measurable on a laptop and inside CI. The dense half of retrieval is an interface,
# implemented where the GPU already is; see rag/retrieve.py for why.
RAG_DATA ?= data

rag-data: ## Pull the retrieval corpus and warehouse from S3 into data/
	@bkt=$$($(TFC) output -raw artifacts_bucket_name 2>/dev/null \
		| grep -Ex '[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]'); \
		[ -n "$$bkt" ] || { echo "no artifacts bucket -- is core applied?"; exit 1; }; \
		for d in xanhsm_retrieval_mock xanhsm_mock_warehouse; do \
			echo "s3://$$bkt/datasets/v1/$$d -> $(RAG_DATA)/$$d"; \
			aws s3 sync "s3://$$bkt/datasets/v1/$$d" "$(RAG_DATA)/$$d" --only-show-errors; \
		done

rag-eval: ## Measure retrieval on the 144 gold queries
	@PYTHONPATH=. python3 -m rag.run_eval $(RAG_ARGS)

# Two pythons on purpose. The serving path imports numpy and nothing heavier, so it stays
# a small image that restarts fast; the encode needs torch and runs once, offline. Keeping
# them apart is what stops 2.4 GB of weights from following the gateway into production.
EMBED_PY ?= .venv-embed/bin/python

dense-env: ## One-time: a python 3.12 venv with torch for the offline encode
	uv venv --python 3.12 .venv-embed
	VIRTUAL_ENV=.venv-embed uv pip install "sentence-transformers>=3" torch

dense-build: ## Encode the corpus into data/index/dense-vi.npy (needs dense-env)
	@test -x $(EMBED_PY) || { echo "run 'make dense-env' first"; exit 1; }
	@PYTHONPATH=. $(EMBED_PY) -m rag.build_dense

dense-eval: ## Retrieval with lexical + dense fused by RRF
	@test -x $(EMBED_PY) || { echo "run 'make dense-env' first"; exit 1; }
	@PYTHONPATH=. $(EMBED_PY) -m rag.run_eval --dense --canonical

dense-ablation: ## lexical vs dense vs fused, split by query type
	@test -x $(EMBED_PY) || { echo "run 'make dense-env' first"; exit 1; }
	@PYTHONPATH=. $(EMBED_PY) -m rag.ablation

rag-eval-nopolicy: ## Same, with the metadata layer off -- shows what it is worth
	@PYTHONPATH=. python3 -m rag.run_eval --no-policy

guardrails-test: ## Behaviour tests for PII, injection, policy, grounding and cache
	@PYTHONPATH=. python3 -m tests.test_guardrails
	@PYTHONPATH=. python3 -m unittest tests.test_llm_pipeline tests.test_semantic_cache tests.test_tracing \
		tests.test_dense_serving tests.test_availability

attacks-build: ## Regenerate the adversarial suite (deterministic, seeded)
	@python3 bench/datasets/make_attacks.py

# FOLD=B is the number that means anything. The rules were written against fold A, so
# scoring on A measures whether the author can read their own miss list. Fold B was never
# looked at while writing rules, and the gap between the two is the honest estimate of how
# much the rule layer generalises to attacks nobody anticipated.
attacks-score: ## Score the guardrail. FOLD=A|B to split seen from held-out techniques
	@PYTHONPATH=. python3 bench/scripts/score_guardrail.py $(if $(FOLD),--fold=$(FOLD),)

# L3 needs a live engine: ASV, MR and PNA all require generating from the model under
# attack. --dry-run checks the harness against a stub and reports nothing quotable.
defenses-dryrun: ## Exercise the L2/L3 harness with no GPU
	@PYTHONPATH=. python3 bench/scripts/eval_defenses.py --dry-run

defenses-eval: ## Measure spotlighting + known-answer detection against the running engine
	@PYTHONPATH=. python3 bench/scripts/eval_defenses.py \
		--base-url $(or $(BASE_URL),http://localhost:8000/v1) --n $(or $(N),50)

secrets-scan: ## Check staged changes for anything that must not reach a public repo
	@./bench/scripts/scan_secrets.sh

# Picks whichever interpreter actually has `diagrams` installed. It lives in the system
# python on one machine and in .venv-embed on another, and hard-coding either one breaks
# the target for whoever is using the other.
diagrams: ## Re-render docs/diagrams/*.py (needs Graphviz + the diagrams package)
	@command -v dot >/dev/null || { echo "needs Graphviz: brew install graphviz"; exit 1; }
	@py=""; \
		for cand in python3 $(EMBED_PY); do \
			if "$$cand" -c "import diagrams" 2>/dev/null; then py="$$cand"; break; fi; \
		done; \
		[ -n "$$py" ] || { echo "no python has 'diagrams': pip install diagrams"; exit 1; }; \
		echo "  dung $$py"; \
		for f in docs/diagrams/*.py; do echo "  $$f"; "$$py" "$$f" || exit 1; done

spotlight-cost: ## Token cost of datamarking, per marker choice (needs dense-env)
	@test -x $(EMBED_PY) || { echo "run 'make dense-env' first"; exit 1; }
	@PYTHONPATH=. $(EMBED_PY) bench/scripts/spotlight_cost.py

# --- EBS hygiene ------------------------------------------------------------
# Dynamically provisioned volumes are deleted by the EBS CSI controller when their PVC
# goes away. `terraform destroy` removes the cluster -- and the controller -- without
# ever deleting the PVCs, so the volumes survive as detached, billable orphans. At two
# volumes a session and three sessions a week that is real money for nothing.
cleanup-volumes: ## Delete workload PVCs so the CSI driver releases their EBS volumes
	-kubectl delete namespace llm-serving --ignore-not-found --timeout=2m
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

model-fetch: ## Publish one checkpoint to S3. REPO= PREFIX= LOCAL_DIR= (resumable)
# The bucket lives in the core tier, which outlives every cluster -- so weights can be
# fetched with no cluster running, which is the usual case for a long download.
	@test -n "$(ARTIFACTS)" || { echo "artifacts bucket unknown -- run: make core-up"; exit 1; }
	@BUCKET="$(ARTIFACTS)" REPO="$(REPO)" PREFIX="$(PREFIX)" LOCAL_DIR="$(LOCAL_DIR)" \
		./bench/scripts/fetch_model.sh

models-awq: ## Fetch both 4-bit AWQ checkpoints -- what `mode: shared` needs to fit
# Sequential on purpose: two hf downloads over one home connection share the bandwidth
# and both take longer than running them one after the other.
	@$(MAKE) --no-print-directory model-fetch \
		REPO=Qwen/Qwen2.5-7B-Instruct-AWQ \
		PREFIX=models/Qwen2.5-7B-Instruct-AWQ LOCAL_DIR=/tmp/qwen7b-awq
	@$(MAKE) --no-print-directory model-fetch \
		REPO=Qwen/Qwen2.5-1.5B-Instruct-AWQ \
		PREFIX=models/Qwen2.5-1.5B-Instruct-AWQ LOCAL_DIR=/tmp/qwen1.5b-awq

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


kubeconfig: ## Point kubectl at the cluster and apply the default StorageClass
	aws eks update-kubeconfig --region $(REGION) --name $(CLUSTER)
# The gp3 StorageClass used to be a Terraform resource. It was the only Kubernetes object
# in the cluster state, and it pulled cluster reachability into every destroy -- see the
# header of k8s/storage/gp3.yaml for what that cost. It lives here now, applied with the
# AWS-authenticated kubectl this target just configured.
	kubectl apply -f k8s/storage/gp3.yaml
# EKS ships gp2 as default. Two defaults at once makes provisioning ambiguous, so stand
# the old one down rather than leaving both claiming the role.
	-@kubectl patch storageclass gp2 -p \
		'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}' \
		2>/dev/null || true
	@echo "StorageClass gp3 la default; gp2 da bo danh hieu default."

hooks: ## Install the git pre-commit hooks
	pre-commit install --install-hooks
	pre-commit run --all-files || true
