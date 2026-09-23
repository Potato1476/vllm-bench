#!/usr/bin/env bash
# Check staged changes for things that must not reach a public repository.
#
#   ./bench/scripts/scan_secrets.sh            # staged changes
#   ./bench/scripts/scan_secrets.sh --tree     # the whole working tree
#
# NO SECRET IS WRITTEN IN THIS FILE. The first version listed the account id and the lab
# passwords as literal patterns, and then flagged itself -- correctly, because a scanner
# carrying the values it hunts for is the leak. Committed patterns are SHAPES only; the
# project's actual values are resolved at runtime:
#
#   account id    aws sts get-caller-identity      (never written down)
#   your IP       checkip.amazonaws.com
#   node IPs      ec2 describe-instances
#   lab passwords the Kubernetes secrets that hold them
#   anything else .secrets-extra, one regex per line, gitignored
#
# Each lookup is optional. With no AWS credentials and no cluster the shape patterns still
# run, and the script says which checks it could not perform rather than implying it
# checked everything.
#
# WHY A SCRIPT AND NOT A ONE-LINER. The one-liner this replaces was
#
#     n=$(git diff --cached --text | grep -c "$pat" || true); [ "$n" = "0" ] && echo clean
#
# and it reported a leak in all five patterns on a commit that had none: grep over a diff
# containing a PNG broke the pipeline, `|| true` swallowed it, $n came back EMPTY, and the
# comparison against "0" failed. That time it erred safe. The same shape errs the other way
# as soon as the count is non-empty for an unrelated reason, and a scanner that can be
# wrong in either direction is not a scanner.
set -uo pipefail

# Shapes. Safe to commit: they describe a secret without being one.
PATTERNS=(
  'AKIA[0-9A-Z]{16}'
  'ASIA[0-9A-Z]{16}'
  'aws_secret_access_key'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'xox[baprs]-[0-9A-Za-z-]{10,}'
  'ghp_[0-9A-Za-z]{36}'
)

SKIPPED=()

add_pattern() {  # value, description
  if [ -n "${1:-}" ]; then
    PATTERNS+=("$(printf '%s' "$1" | sed 's/[.[\*^$()+?{|]/\\&/g')")
  else
    SKIPPED+=("$2")
  fi
}

# --- resolve this project's actual values, all optional ---------------------------
acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
add_pattern "${acct:-}" "account id (khong co credential AWS)"

myip=$(curl -s --max-time 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]')
add_pattern "${myip:-}" "IP hien tai (khong co mang)"

nodeips=$(aws ec2 describe-instances --filters "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null)
if [ -n "${nodeips:-}" ] && [ "$nodeips" != "None" ]; then
  for ip in $nodeips; do [ "$ip" = "None" ] || add_pattern "$ip" ""; done
else
  SKIPPED+=("IP cac node (cum dang tat)")
fi

for ns_secret in "monitoring grafana-admin admin-password" \
                 "monitoring ingress-basic-auth password"; do
  read -r ns name field <<< "$ns_secret"
  val=$(kubectl -n "$ns" get secret "$name" -o "jsonpath={.data.$field}" 2>/dev/null \
        | base64 -d 2>/dev/null)
  add_pattern "${val:-}" "mat khau $name (khong doc duoc secret)"
done

# Anything else the operator wants watched. Gitignored, one extended regex per line.
if [ -f .secrets-extra ]; then
  while IFS= read -r line; do
    [ -n "$line" ] && [ "${line:0:1}" != "#" ] && PATTERNS+=("$line")
  done < .secrets-extra
fi

# --- scan --------------------------------------------------------------------------
SKIP_EXT='png|jpg|jpeg|gif|pdf|npy|npz|gz|zip|woff2?|ico|safetensors|jsonl'

mode="${1:-staged}"
if [ "$mode" = "--tree" ]; then
  files=$(git ls-files)
else
  files=$(git diff --cached --name-only --diff-filter=ACM)
fi

hits=0
scanned=0
SELF="bench/scripts/scan_secrets.sh"

while IFS= read -r f; do
  [ -n "$f" ] && [ -f "$f" ] || continue
  printf '%s' "$f" | grep -qiE "\.($SKIP_EXT)$" && continue
  # A scanner that lists secret SHAPES necessarily contains those shapes. Excluding it
  # is not a loophole: it carries no values, only descriptions of values, which is the
  # whole reason the literals were moved out to runtime lookups.
  [ "$f" = "$SELF" ] && continue
  scanned=$((scanned + 1))
  for pat in "${PATTERNS[@]}"; do
    # -I makes grep skip binary files it detects regardless of extension.
    if matches=$(grep -nEI "$pat" "$f" 2>/dev/null); then
      hits=$((hits + 1))
      echo "  RO RI  $f"
      printf '           %s\n' "$matches" | head -3
    fi
  done
done <<< "$files"

echo "  da quet $scanned file, ${#PATTERNS[@]} mau"
if [ ${#SKIPPED[@]} -gt 0 ]; then
  echo "  KHONG kiem duoc: $(IFS=', '; echo "${SKIPPED[*]}")"
fi
if [ "$hits" -gt 0 ]; then
  echo "  $hits PHAT HIEN -- khong commit"
  exit 1
fi
echo "  sach"
