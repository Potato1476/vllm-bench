#!/usr/bin/env bash
# Bring up the shared ingress: one NGINX controller on a NodePort of the tooling node,
# five hostnames, and the security group rules that decide who may reach it.
# Idempotent -- safe to re-run after an IP change, which is the usual reason to.
set -euo pipefail

CHART_VERSION="${CHART_VERSION:-4.15.1}"
NS=ingress-nginx
PORT=30080
CLUSTER="${CLUSTER:-da51-lab}"
REGION="${REGION:-us-east-1}"
TPL="$(dirname "$0")/ingress.tpl.yaml"
VALUES="$(dirname "$0")/ingress-nginx-values.yaml"
OUT="${TMPDIR:-/tmp}/da51-ingress.yaml"

# Every rule this script writes carries this description, and `ingress-down` revokes by
# matching it. Without a marker, cleanup would have to guess which rules on a shared
# security group were ours -- and that group also carries the rules EKS needs for the
# control plane and for node-to-node traffic.
TAG="da51-ingress"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

IP=$(curl -s --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')
[ -n "$IP" ] || { echo "khong xac dinh duoc IP cong khai"; exit 1; }

# macOS, Windows and most modern Linux clients use IPv6 privacy addresses: the lower
# 64 bits rotate periodically so an allow-list containing the observed /128 works today
# and returns 403 tomorrow.  Keep the ISP-assigned /64 instead.  It admits the devices on
# this one subscriber network, not the provider's wider address pool.  IPv4 remains /32.
#
# The public EC2 node itself is IPv4-only.  This IPv6 prefix belongs only in NGINX's
# source-address allow-list; the EC2 security-group rule below therefore remains IPv4.
IPV6=$(curl -6 -s --max-time 10 https://api64.ipify.org 2>/dev/null \
       | tr -d '[:space:]' || true)
IPV6_CIDR=""
if [ -n "$IPV6" ]; then
  IPV6_CIDR=$(python3 -c \
    'import ipaddress, sys; print(ipaddress.ip_network(sys.argv[1] + "/64", strict=False))' \
    "$IPV6" 2>/dev/null || true)
fi

# Everyone who is allowed in, as an explicit list. The security group drops everything
# else before it reaches the node's OS, so this -- not the basic-auth password -- is the
# lock that matters.
#
# To let a teammate or the mentor in, pass their address:
#   make ingress-up EXTRA_CIDRS=203.0.113.7/32,198.51.100.4/32
#
# Ask each person for the output of `curl https://checkip.amazonaws.com`. Vietnamese
# consumer ISPs rotate addresses often -- ours changed four times in one week -- so
# treat any entry here as good for today, not for the project.
#
# PUBLIC=1 opens the port to the whole internet. It is a separate flag rather than an
# EXTRA_CIDRS entry so that it cannot happen by typo, and so this warning gets printed.
#
# What opening it actually costs, since it is worth knowing before rather than after:
#   - This is plain HTTP. The basic-auth password travels base64-encoded, which is
#     readable by anyone on the path. Treat it as a speed bump, not a secret.
#   - Grafana keeps its own login and anonymous access stays off, so a stranger who
#     finds it gets a login form.
#   - LiteLLM and vLLM are the expensive endpoints: an open OpenAI-compatible API is
#     found by scanners within hours and every request is GPU time billed to this
#     project. Both stay pinned to CLIENT_CIDRS even when PUBLIC=1.
CLIENT_CIDR="$IP/32"
# This machine, on both address families. A v6 privacy address rotates on its own
# schedule, so pinning only the v4 leaves the gateway rejecting the very laptop that
# published it.
CLIENT_CIDRS="$CLIENT_CIDR"
[ -n "$IPV6_CIDR" ] && CLIENT_CIDRS="$CLIENT_CIDRS,$IPV6_CIDR"

# Plus anyone else who may reach the two EXPENSIVE hosts, llm and vllm, at the nginx
# layer.
#
# EXTRA_CIDRS deliberately does not extend to these -- it opens the node port so other
# people can read dashboards, and an open LLM endpoint is the one exposure that costs
# money per request. But "only the machine that ran this script" turned out to be too
# narrow the moment a second person needed to send a request, and the failure is a silent
# 403 that looks like a broken gateway.
#
# A separate variable from EXTRA_CIDRS on purpose: letting somebody read Grafana and
# letting them spend GPU time are different decisions.
#   make ingress-up LLM_CIDRS=203.0.113.7/32
LLM_ALLOW="$CLIENT_CIDRS"
[ -n "${LLM_CIDRS:-}" ] && LLM_ALLOW="$LLM_ALLOW,$LLM_CIDRS"
if [ "${PUBLIC:-0}" = "1" ]; then
  RANGES="0.0.0.0/0"
  say "MO CONG KHAI: bat ky ai cung ket noi duoc toi cong $PORT"
  echo "  Grafana       -- co trang dang nhap rieng"
  echo "  Prometheus    -- basic auth (mat khau di qua mang dang cleartext tren HTTP)"
  echo "  Alertmanager  -- basic auth"
  echo "  LiteLLM       -- VAN khoa ve $LLM_ALLOW + Bearer key"
  echo "  vLLM          -- VAN khoa ve $LLM_ALLOW o tang nginx, khong theo cong khai"
else
  RANGES="$CLIENT_CIDR"
  [ -n "${EXTRA_CIDRS:-}" ] && RANGES="$RANGES,$EXTRA_CIDRS"
  [ "${RANGES#*0.0.0.0/0}" = "$RANGES" ] || {
    echo "  Dung PUBLIC=1 thay vi dat 0.0.0.0/0 vao EXTRA_CIDRS"; exit 1; }
  say "cho phep truy cap tu: $RANGES"
fi

# --- basic auth ------------------------------------------------------------------
# One credential shared by Prometheus, Alertmanager and vLLM. LiteLLM uses its own
# Bearer key because basic auth would consume the Authorization header. Generated once and kept
# in the Secret so re-running this script does not silently change the password under
# a browser that has already saved it.
if kubectl -n monitoring get secret ingress-basic-auth >/dev/null 2>&1; then
  PW=$(kubectl -n monitoring get secret ingress-basic-auth -o jsonpath='{.data.password}' | base64 -d)
  say "dung lai mat khau basic auth da co"
else
  # Every stage here consumes its whole input. The obvious spelling of this --
  # `tr -dc ... </dev/urandom | head -c 20` -- makes head close the pipe after 20 bytes,
  # tr dies of SIGPIPE, and `set -o pipefail` turns that into exit 141 for the script.
  PW=$(openssl rand -base64 32 | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c1-20)
  say "tao mat khau basic auth moi"
fi
AUTH=$(htpasswd -nbB admin "$PW")

# ingress-nginx reads the Secret from the Ingress's own namespace, so it needs a copy in
# each -- cross-namespace secret references are disabled by default.
for ns in monitoring inference; do
  kubectl -n "$ns" create secret generic ingress-basic-auth \
    --from-literal=auth="$AUTH" --from-literal=password="$PW" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

# --- controller ------------------------------------------------------------------
say "cai ingress-nginx $CHART_VERSION"
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --version "$CHART_VERSION" \
  --namespace "$NS" --create-namespace \
  -f "$VALUES" \
  --wait --timeout 10m

# --- which node answers ----------------------------------------------------------
# externalTrafficPolicy: Local means only the node running the controller replies on the
# NodePort. Ask Kubernetes where the pod landed rather than assuming the tooling node,
# so this still prints the truth if the nodeSelector is ever changed.
NODE=$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=controller \
        -o jsonpath='{.items[0].spec.nodeName}')
[ -n "$NODE" ] || { echo "khong tim thay pod controller"; exit 1; }

NODEIP=$(aws ec2 describe-instances --region "$REGION" \
          --filters "Name=private-dns-name,Values=$NODE" "Name=instance-state-name,Values=running" \
          --query 'Reservations[].Instances[].PublicIpAddress' --output text)
[ -n "$NODEIP" ] && [ "$NODEIP" != "None" ] || { echo "$NODE khong co IP cong khai"; exit 1; }
say "controller nam tren $NODE -- dia chi cong khai $NODEIP"

# --- security group --------------------------------------------------------------
# The node security group is created by the EKS Terraform module, so its id is different
# after every `lab-up`. Discover it from the instance rather than hard-coding it.
SG=$(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=private-dns-name,Values=$NODE" "Name=instance-state-name,Values=running" \
      --query 'Reservations[].Instances[].SecurityGroups[0].GroupId' --output text)
say "mo cong $PORT tren $SG"

# Revoke our previous rules first. Re-running after an IP change must not leave the old
# address behind -- that is the whole point of running it again.
# The `[]` is load-bearing. `IpPermissions[?...].IpRanges[?...]` filters a list of lists
# and silently matches nothing, so the revoke became a no-op and stale addresses piled
# up on the group -- including, after `PUBLIC=1`, a redundant /32 next to 0.0.0.0/0.
# Flattening first gives the second filter plain objects to test.
OLD=$(aws ec2 describe-security-groups --region "$REGION" --group-ids "$SG" \
       --query "SecurityGroups[0].IpPermissions[?FromPort==\`$PORT\`].IpRanges[] | [?Description=='$TAG'].CidrIp" \
       --output text 2>/dev/null || true)
for cidr in $OLD; do
  echo "  thu hoi $cidr"
  aws ec2 revoke-security-group-ingress --region "$REGION" --group-id "$SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,IpRanges=[{CidrIp=$cidr}]" \
    >/dev/null 2>&1 || true
done

IFS=',' read -ra CIDRS <<< "$RANGES"
for cidr in "${CIDRS[@]}"; do
  echo "  cho phep $cidr"
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,IpRanges=[{CidrIp=$cidr,Description=$TAG}]" \
    >/dev/null
done

# --- ingresses -------------------------------------------------------------------
sed -e "s/%%IP%%/$NODEIP/g" -e "s|%%CLIENT%%|$LLM_ALLOW|g" "$TPL" > "$OUT"
kubectl apply -f "$OUT" >/dev/null
say "da tao Ingress"

cat <<EOF

  Dang nhap
    Grafana              admin / (mat khau rieng -- make creds)
    Ba dich vu con lai   admin / $PW
    LiteLLM              Bearer key (make creds)

  Grafana       http://grafana.$NODEIP.nip.io:$PORT
  Prometheus    http://prometheus.$NODEIP.nip.io:$PORT
  Alertmanager  http://alertmanager.$NODEIP.nip.io:$PORT
  LiteLLM       http://llm.$NODEIP.nip.io:$PORT/v1/models
  vLLM          http://vllm.$NODEIP.nip.io:$PORT/v1/models

  Neu mang chan nip.io, them dong nay vao /etc/hosts roi dung ten .da51.lab:$PORT

    $NODEIP  grafana.da51.lab prometheus.da51.lab alertmanager.da51.lab llm.da51.lab vllm.da51.lab

  Ai vao duoc: $RANGES
  LiteLLM: chi $LLM_ALLOW + Bearer key (xem bang 'make creds')
  vLLM rieng: chi $LLM_ALLOW (khoa o tang nginx)

  Dia chi nay thuoc ve node. No doi khi node bi thay -- tuc la sau moi lan lab-up,
  va sau 'make gpu n=0' neu controller dang o node gpu. Chay 'make ingress-url' de xem lai.
EOF
