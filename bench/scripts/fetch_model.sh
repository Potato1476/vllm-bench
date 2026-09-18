#!/usr/bin/env bash
# Fetch the model weights from HuggingFace and publish them to S3.
#
# This exists because the plain one-liner is not reliable over a home connection. A
# 15 GB transfer at a few MB/s takes over an hour, and HuggingFace's Xet backend drops
# the connection often enough that an unattended run usually fails partway. Every design
# choice below is a reaction to that:
#
#   * Xet is disabled. The failure seen in practice was inside xet_get -- a CAS client
#     error against us.aws.cdn.hf.co. Plain HTTPS is slower per connection but survives.
#   * Retries with backoff, because `hf download` resumes: completed files are skipped,
#     so a retry costs only what is still missing.
#   * Completeness is checked against the Hub's own file list rather than against "the
#     command exited 0". A truncated shard uploads happily and only fails much later,
#     inside vLLM, as an unhelpful load error.
set -uo pipefail

REPO=${REPO:-Qwen/Qwen2.5-7B-Instruct}
LOCAL_DIR=${LOCAL_DIR:-/tmp/qwen7b}
BUCKET=${BUCKET:-}
PREFIX=${PREFIX:-models/Qwen2.5-7B-Instruct}
ATTEMPTS=${ATTEMPTS:-8}
WORKERS=${WORKERS:-4}

export HF_HUB_DISABLE_XET=1

say() { printf '\n[fetch_model] %s\n' "$*"; }
die() { printf '[fetch_model] ERROR: %s\n' "$*" >&2; exit 1; }

command -v hf  >/dev/null || die "the 'hf' CLI is not installed (pip install -U huggingface_hub)"
command -v jq  >/dev/null || die "jq is required"
command -v aws >/dev/null || die "the AWS CLI is required"

[ -n "${HF_TOKEN:-}" ] || say "HF_TOKEN is not set; downloads are rate-limited. Set it to go faster."

# ----------------------------------------------------------------- expected file list
say "reading the file list for $REPO"
manifest=$(curl -sf --max-time 60 "https://huggingface.co/api/models/${REPO}?blobs=true") \
  || die "could not reach the HuggingFace API"
expected=$(echo "$manifest" | jq -r '.siblings[] | "\(.rfilename)\t\(.size // 0)"')
total=$(echo "$manifest" | jq -r '[.siblings[].size // 0] | add')
printf '  %d files, %.1f GiB\n' "$(echo "$expected" | wc -l | tr -d ' ')" \
  "$(echo "$total/1073741824" | bc -l)"

# ----------------------------------------------------------------- download with retry
attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
  say "download attempt $attempt/$ATTEMPTS  (resumes; finished files are skipped)"
  if hf download "$REPO" --local-dir "$LOCAL_DIR" --max-workers "$WORKERS"; then
    say "hf download reported success"
    break
  fi
  if [ "$attempt" -eq "$ATTEMPTS" ]; then
    say "giving up after $ATTEMPTS attempts; whatever arrived is still on disk and the "
    say "next run will resume from there"
    exit 1
  fi
  wait=$((attempt * 15))
  say "attempt failed; retrying in ${wait}s"
  sleep "$wait"
  attempt=$((attempt + 1))
done

# ----------------------------------------------------------------- verify completeness
say "verifying every file against the Hub's sizes"
missing=0
while IFS=$'\t' read -r name size; do
  path="$LOCAL_DIR/$name"
  if [ ! -f "$path" ]; then
    printf '  MISSING   %s\n' "$name"; missing=$((missing + 1)); continue
  fi
  actual=$(wc -c < "$path" | tr -d ' ')
  if [ "$size" != "0" ] && [ "$actual" != "$size" ]; then
    printf '  TRUNCATED %s  (%s of %s bytes)\n' "$name" "$actual" "$size"
    missing=$((missing + 1)); continue
  fi
  printf '  ok        %s\n' "$name"
done <<< "$expected"

[ "$missing" -eq 0 ] || die "$missing file(s) incomplete -- re-run this script, it resumes"

# ----------------------------------------------------------------- publish
[ -n "$BUCKET" ] || { say "BUCKET not set; download verified, skipping upload"; exit 0; }

say "uploading to s3://$BUCKET/$PREFIX/"
# .cache/ holds hf's lock and resume metadata. Without this filter it is copied to S3 and
# then pulled back down by the initContainer on every single pod start.
aws s3 sync "$LOCAL_DIR" "s3://$BUCKET/$PREFIX/" --exclude ".cache/*" --only-show-errors \
  || die "upload failed"

say "confirming what landed in S3"
remote=$(aws s3 ls --recursive "s3://$BUCKET/$PREFIX/" | grep -c 'safetensors$')
[ "$remote" -ge 1 ] || die "no safetensors found under the prefix after upload"
aws s3 ls --summarize --human-readable --recursive "s3://$BUCKET/$PREFIX/" | tail -3
say "done -- $remote safetensors shard(s) published"
