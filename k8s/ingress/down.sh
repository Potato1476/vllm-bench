#!/usr/bin/env bash
# Take the ingress down and, just as importantly, close the port behind it.
#
# The Helm release owns the controller and the Service. It does NOT own the security
# group rules -- `up.sh` wrote those directly, because a NodePort Service has no
# equivalent of loadBalancerSourceRanges for the cloud provider to act on. So uninstalling
# the chart alone would leave port 30080 open on a node with nothing listening: not a
# charge, but an open door nobody is watching. This script closes it.
#
# Runs on the way to `make lab-down`, where the whole node disappears anyway, and on its
# own when you just want the dashboards off the internet.
set -euo pipefail

NS=ingress-nginx
PORT=30080
REGION="${REGION:-us-east-1}"
TAG="da51-ingress"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "xoa Ingress"
kubectl delete ingress -n monitoring grafana prometheus alertmanager --ignore-not-found
kubectl delete ingress -n inference vllm-a --ignore-not-found

# Revoke before the node goes away: once the instance is gone its private DNS name no
# longer resolves to anything, and the rules become unfindable by the lookup below --
# they would then sit on the security group until Terraform destroys it.
say "dong cong $PORT"
for SG in $(aws ec2 describe-security-groups --region "$REGION" \
              --filters "Name=ip-permission.from-port,Values=$PORT" \
              --query 'SecurityGroups[].GroupId' --output text); do
  # `IpRanges[]` flattens before the filter runs. Without it JMESPath is testing a list
  # of lists, matches nothing, and this loop quietly leaves the port open forever.
  CIDRS=$(aws ec2 describe-security-groups --region "$REGION" --group-ids "$SG" \
           --query "SecurityGroups[0].IpPermissions[?FromPort==\`$PORT\`].IpRanges[] | [?Description=='$TAG'].CidrIp" \
           --output text 2>/dev/null || true)
  for cidr in $CIDRS; do
    echo "  $SG: thu hoi $cidr"
    aws ec2 revoke-security-group-ingress --region "$REGION" --group-id "$SG" \
      --ip-permissions "IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,IpRanges=[{CidrIp=$cidr}]" \
      >/dev/null 2>&1 || true
  done
done

say "go ingress-nginx"
helm uninstall ingress-nginx -n "$NS" --wait --timeout 5m 2>/dev/null || true
kubectl delete namespace "$NS" --ignore-not-found --timeout=5m

say "kiem tra khong con gi mo"
LEFT=$(aws ec2 describe-security-groups --region "$REGION" \
        --filters "Name=ip-permission.from-port,Values=$PORT" \
        --query 'SecurityGroups[].GroupId' --output text)
[ -z "$LEFT" ] && echo "  khong con security group nao mo cong $PORT" \
               || echo "  VAN CON mo tren: $LEFT"

# A NodePort costs nothing, but this project has been bitten by orphaned EBS volumes
# before, so check for the other thing that bills by the hour whether or not it is used.
echo "  load balancer dang ton tai (mong doi: khong co):"
aws elbv2 describe-load-balancers --region "$REGION" \
  --query 'LoadBalancers[].[LoadBalancerName,Type,State.Code]' --output text || true
