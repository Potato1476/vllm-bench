#!/usr/bin/env bash
# Chạy đúng những công cụ mà Quality Gate chạy, lên mã của DA#51, rồi ghi output thô ra
# $OUT. Phán xét là việc của gate_check.py.
#
#   GATE=<thu-muc-gate> OUT=<thu-muc-ket-qua> ./bench/scripts/gate_scan.sh
#
# DÙNG DOCKER CHO CẢ BA CÔNG CỤ, KHÔNG DÙNG BẢN CÀI SẴN
#
# Phiên bản phải khớp bản gate ghim. Trivy trên máy này là 0.75.0 còn gate ghim 0.74.0, và
# IMG-003 so với một baseline CVE — hai bản Trivy khác nhau cho hai tập CVE khác nhau, nên
# "so với baseline" sẽ so hai thứ không cùng thước. Docker làm local và CI chạy đúng một
# bản, không phụ thuộc máy ai.
#
# CHIỀU NGHỊCH CHẠY TRƯỚC CHIỀU THUẬN
#
# Mỗi máy quét phải chứng minh nó còn bắt được, bằng fixture vi phạm của chính gate, TRƯỚC
# khi kết quả "sạch" trên mã thật được tin. Không bắt đủ thì thoát 2. Repo này từng có
# `scan_secrets.sh` báo "sach" khi quét 0 file, nên thứ tự này không phải thủ tục.
set -euo pipefail

GATE="${GATE:?can GATE=<thu muc gate da clone>}"
OUT="${OUT:-$PWD/.gate-out}"
TRIVY_IMAGE="${TRIVY_IMAGE:-aquasec/trivy:0.74.0}"
CONFTEST_IMAGE="${CONFTEST_IMAGE:-openpolicyagent/conftest:v0.70.0}"
KYVERNO_IMAGE="${KYVERNO_IMAGE:-ghcr.io/kyverno/kyverno-cli:v1.19.1}"
TRIVY_CACHE="${TRIVY_CACHE:-$OUT/.trivycache}"

GATE=$(cd "$GATE" && pwd)
REPO=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$OUT" "$TRIVY_CACHE"
RENDER="$OUT/rendered"; rm -rf "$RENDER"; mkdir -p "$RENDER"

# Cặp file cô lập, ADR-042 bên gate: nếu không truyền, một `.trivyignore` đặt trong repo
# service sẽ âm thầm xoá CVE mà không ai duyệt.
CO_LAP=(--config /gate/security/trivy-co-lap.yaml
        --ignorefile /gate/security/trivy-co-lap.trivyignore)

trivy_run() { docker run --rm -v "$GATE:/gate:ro" -v "$TRIVY_CACHE:/root/.cache" "$@"; }

echo "==> render manifest (đúng thứ Argo CD sẽ apply)"
for app in "$REPO"/deploy/argocd/apps/vllm-*.yaml; do
  name=$(basename "$app" .yaml)
  python3 -I -c 'import sys,yaml; print(yaml.safe_dump(yaml.safe_load(open(sys.argv[1]))["spec"]["source"]["helm"]["valuesObject"]))' \
    "$app" > "$OUT/$name.values.yaml"
  helm template "$name" "$REPO/charts/vllm" -n inference -f "$OUT/$name.values.yaml" \
    > "$RENDER/$name.yaml"
done
helm template guardrail "$REPO/charts/guardrail" -n llm-serving \
  --set image.repository=111111111111.dkr.ecr.us-east-1.amazonaws.com/vllm-bench/guardrail \
  --set image.tag=gate-check > "$RENDER/guardrail.yaml"
cp "$REPO"/deploy/rollout/*.yaml "$RENDER/"
# Job rollout chỉ tồn tại khi có ứng viên, nên state lúc idle không render ra cái nào.
# Dựng một state có ứng viên để các Job đó cũng bị soi -- chúng sẽ chạy trên production.
python3 -I - "$RENDER" "$REPO" <<'PY'
import sys, pathlib, tempfile
sys.path.insert(0, sys.argv[2])
from bench.scripts import rollout_state as rs
dest = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as tmp:
    out = pathlib.Path(tmp)
    s = rs.load()
    # Track nao rollout duoc THAT SU -- khong phai track dau theo thu tu chu cai. Mot
    # track mot card tu choi ung vien (stable se con 0 pod), nen chon bua se lam buoc nay
    # vo. Khong co track nao rollout duoc thi bo qua, dung lam ca lan quet do.
    track = next((n for n in sorted(s.tracks)
                  if not rs.can_host_candidate(s.track(n))), None)
    if track is None:
        print("khong track nao rollout duoc -- bo qua phan render Job", file=sys.stderr)
        raise SystemExit(0)
    rs.set_candidate(s, track, rs.Ref(f"models/{track}/gate-check",
                                      s.track(track).stable.engine, weights_gib=1.0), now="t")
    rs.write_rendered(s, out)
    for p in (out / "rollout").glob("job-*.yaml"):
        (dest / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    rs.advance(s, track, rs.CANARY, weight=25, now="t"); rs.write_rendered(s, out)
    for p in (out / "rollout").glob("job-*.yaml"):
        (dest / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
PY
echo "    $(ls -1 "$RENDER" | wc -l | tr -d ' ') file"

echo "==> IMG-001 quét secret — chiều nghịch trước"
NGHICH="$OUT/nghich"; rm -rf "$NGHICH"; mkdir -p "$NGHICH/tests"
FIX="$GATE/tests/fixtures/violations/ml-002-apikey-in-env.yaml"
[ -f "$FIX" ] || { echo "LOI: khong thay fixture key gia cua gate -- GATE_REF sai?"; exit 2; }
cp "$FIX" "$NGHICH/"; cp "$FIX" "$NGHICH/tests/lo-key.yaml"
cp "$FIX" "$NGHICH/secret.example.yaml"; cp "$FIX" "$NGHICH/HUONG-DAN.md"
trivy_run -v "$NGHICH:/n:ro" "$TRIVY_IMAGE" fs "${CO_LAP[@]}" \
  --secret-config /gate/security/trivy-secret.yaml --scanners secret \
  --exit-code 0 --format json --quiet /n > "$OUT/secret-nghich.json"
N=$(python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); print(sum(len(r.get('Secrets') or []) for r in d.get('Results') or []))" "$OUT/secret-nghich.json")
if [ "$N" -lt 4 ]; then
  echo "LOI: chi bat duoc $N/4 key gia. May quet dang cau hinh sai -- dung tin ket qua 'sach' ben duoi."
  exit 2
fi
echo "    chiều nghịch đạt: bắt $N key giả, kể cả trong tests/, *.example, *.md"
trivy_run -v "$REPO:/src:ro" "$TRIVY_IMAGE" fs "${CO_LAP[@]}" \
  --secret-config /gate/security/trivy-secret.yaml --scanners secret \
  --skip-dirs .venv-embed --skip-dirs .venv --skip-dirs .git --skip-dirs data \
  --exit-code 0 --format json --quiet /src > "$OUT/secret.json"

echo "==> IMG-002 quét CVE của phụ thuộc khai trong repo"
trivy_run -v "$REPO:/src:ro" "$TRIVY_IMAGE" fs "${CO_LAP[@]}" --scanners vuln \
  --skip-dirs .venv-embed --skip-dirs .venv --skip-dirs .git --skip-dirs data \
  --severity CRITICAL,HIGH --exit-code 0 --format json --quiet /src > "$OUT/vuln.json"

echo "==> IMG-004/005/ML-007 Conftest trên Dockerfile — chiều nghịch trước"
NS=(--namespace dockerfile.img004 --namespace dockerfile.img005 --namespace dockerfile.ml007)
conftest_run() {
  docker run --rm -v "$GATE:/gate:ro" -v "$REPO:/src:ro" "$CONFTEST_IMAGE" test "$@" \
    -p /gate/policies/rego/dockerfile --data /gate/security/base-image-allowlist.yaml \
    "${NS[@]}" --no-color -o json
}
conftest_run /gate/tests/rego/Dockerfile.invalid > "$OUT/conftest-nghich.json" || true
R=$(python3 -I -c "
import json,sys,re
d=json.load(open(sys.argv[1]))
ids=sorted({m.group(0) for b in d for k in ('failures','warnings') for i in (b.get(k) or [])
            for m in [re.search(r'(IMG|ML)-\d{3}', i.get('msg',''))] if m})
print(','.join(ids))" "$OUT/conftest-nghich.json")
if [ "$R" != "IMG-004,IMG-005,ML-007" ]; then
  echo "LOI: Dockerfile vi pham cua gate chi ra '$R', mong IMG-004,IMG-005,ML-007."
  echo "     Rego nap hong hoac thieu --data. Dung tin ket qua ben duoi."
  exit 2
fi
echo "    chiều nghịch đạt: fixture ra $R"
conftest_run /src/services/llm_pipeline/Dockerfile /src/bench/runner/Dockerfile \
  > "$OUT/conftest.json" || true

echo "==> Kyverno trên manifest đã render"
docker run --rm -v "$GATE:/gate:ro" -v "$RENDER:/res:ro" "$KYVERNO_IMAGE" apply \
  /gate/policies/kyverno/ml /gate/policies/kyverno/operational /gate/policies/kyverno/pod-security \
  --resource /res > "$OUT/kyverno.txt" 2>&1 || true
grep -E "^pass:" "$OUT/kyverno.txt" | sed 's/^/    /' || true

echo "==> output ở $OUT"
