"""Phần phán xét của cổng Quality Gate: mức chặn, ngoại lệ, và hạn của ngoại lệ.

Đây là phần dễ sai nhất và cũng là phần duy nhất chạy được mà không cần docker. Một lỗi ở
đây không làm CI đỏ — nó làm CI **xanh** trong khi có vi phạm, và đó là kiểu lỗi không ai
phát hiện bằng cách xem kết quả chạy.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from bench.scripts import gate_check as gc

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 12)

MUC = {
    "hard_gate": ["IMG-001", "K8S-001", "K8S-002", "K8S-003", "K8S-007", "ML-002"],
    "muc": {"K8S-004": "block", "K8S-008": "block", "K8S-016": "warn", "K8S-017": "info",
            "IMG-002": "block", "ML-005": "block"},
}


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.tmp = Path(self._d.name)
        self.gate = self.tmp / "gate"
        (self.gate / "security").mkdir(parents=True)
        (self.gate / "security" / "muc-rule.json").write_text(json.dumps(MUC))
        self.levels = gc.load_levels(self.gate)

    def exceptions(self, entries: list[dict]) -> list[gc.Exception_]:
        path = self.tmp / "exc.yaml"
        path.write_text(yaml.safe_dump({"exceptions": entries}), encoding="utf-8")
        return gc.load_exceptions(path, self.levels)

    def exc(self, rule: str, resources: list[str] | None = None,
            expires: str = "2026-12-31") -> dict:
        return {"rule": rule, "resources": resources or [], "owner": "ai do",
                "expires": expires, "reason": "vi mot ly do"}


class LevelTest(_Tmp):
    def test_levels_come_from_the_gate_not_from_a_copy(self) -> None:
        self.assertEqual(self.levels.of("K8S-004"), "block")
        self.assertEqual(self.levels.of("IMG-001"), "hard_gate")
        self.assertEqual(self.levels.of("K8S-016"), "warn")

    def test_an_unknown_rule_defaults_to_warn(self) -> None:
        """Gate thêm rule mới thì CI ở đây không đỏ đột ngột, nhưng vẫn hiện ra."""
        self.assertEqual(self.levels.of("K8S-099"), "warn")
        self.assertFalse(self.levels.blocking("K8S-099"))

    def test_judging_without_the_gate_levels_is_refused(self) -> None:
        """Không được âm thầm chấm bằng mức chép lại: mức phải đọc từ gate."""
        with self.assertRaises(gc.GateError):
            gc.load_levels(self.tmp / "khong-co")


class ExceptionLoadTest(_Tmp):
    def test_a_hard_gate_can_never_be_excepted(self) -> None:
        for rule in MUC["hard_gate"]:
            with self.subTest(rule), self.assertRaises(gc.GateError) as cm:
                self.exceptions([self.exc(rule)])
            self.assertIn("hard gate", str(cm.exception))

    def test_a_bad_declaration_fails_the_whole_run(self) -> None:
        """Bỏ qua im lặng một dòng khai sai trông y như đã chấp nhận nó."""
        bad = [
            ("thieu owner", {k: v for k, v in self.exc("K8S-004").items() if k != "owner"}),
            ("thieu reason", {k: v for k, v in self.exc("K8S-004").items() if k != "reason"}),
            ("thieu expires", {k: v for k, v in self.exc("K8S-004").items() if k != "expires"}),
            ("rule sai dang", self.exc("khong-phai-rule")),
            ("ngay sai dang", self.exc("K8S-004", expires="hom qua")),
            ("resources khong phai list", {**self.exc("K8S-004"), "resources": "Deployment/x"}),
        ]
        for label, entry in bad:
            with self.subTest(label), self.assertRaises(gc.GateError):
                self.exceptions([entry])

    def test_a_missing_file_means_no_exceptions_not_an_error(self) -> None:
        self.assertEqual(gc.load_exceptions(self.tmp / "khong-co.yaml", self.levels), [])


class JudgeTest(_Tmp):
    def finding(self, rule: str, resource: str = "Deployment/x") -> gc.Finding:
        return gc.Finding(rule, resource, "chi tiet", "test")

    def test_a_blocking_violation_with_no_exception_fails(self) -> None:
        v = gc.judge([self.finding("K8S-004")], self.levels, [], TODAY)
        self.assertFalse(v.ok)
        self.assertEqual(len(v.enforced), 1)

    def test_a_warn_violation_does_not_fail(self) -> None:
        v = gc.judge([self.finding("K8S-016")], self.levels, [], TODAY)
        self.assertTrue(v.ok)
        self.assertEqual(len(v.informational), 1)

    def test_a_hard_gate_violation_always_fails(self) -> None:
        """Không có đường nào cho IMG-001 đi qua: khai ngoại lệ thì load đã từ chối, nên
        ở đây nó chỉ còn một kết cục."""
        v = gc.judge([self.finding("IMG-001", "services/x.py")], self.levels, [], TODAY)
        self.assertFalse(v.ok)

    def test_an_exception_in_date_excuses_but_is_still_reported(self) -> None:
        exc = self.exceptions([self.exc("K8S-004", expires="2026-11-09")])
        v = gc.judge([self.finding("K8S-004")], self.levels, exc, TODAY)
        self.assertTrue(v.ok)
        self.assertEqual(len(v.excused), 1)
        self.assertEqual(len(v.enforced), 0)

    def test_an_expired_exception_blocks_again(self) -> None:
        """Điểm khác biệt duy nhất giữa một ngoại lệ và việc tắt quy tắc."""
        exc = self.exceptions([self.exc("K8S-004", expires="2026-10-11")])
        v = gc.judge([self.finding("K8S-004")], self.levels, exc, TODAY)
        self.assertFalse(v.ok)
        self.assertEqual(len(v.expired), 1)
        self.assertEqual(len(v.excused), 0)

    def test_an_exception_expiring_today_still_holds(self) -> None:
        exc = self.exceptions([self.exc("K8S-004", expires="2026-10-12")])
        self.assertTrue(gc.judge([self.finding("K8S-004")], self.levels, exc, TODAY).ok)

    def test_a_narrow_exception_does_not_cover_another_resource(self) -> None:
        """Một ngoại lệ cho vLLM không được che luôn guardrail."""
        exc = self.exceptions([self.exc("K8S-004", ["Deployment/vllm-*"])])
        v = gc.judge([self.finding("K8S-004", "Deployment/vllm-7b-a"),
                      self.finding("K8S-004", "Deployment/guardrail")],
                     self.levels, exc, TODAY)
        self.assertFalse(v.ok)
        self.assertEqual([f.resource for f, _ in v.enforced], ["Deployment/guardrail"])

    def test_an_exception_matching_nothing_is_surfaced(self) -> None:
        """Một ngoại lệ không dùng là một quy tắc đang tắt mà không ai cần nó tắt."""
        exc = self.exceptions([self.exc("K8S-008")])
        v = gc.judge([], self.levels, exc, TODAY)
        self.assertTrue(v.ok)
        self.assertEqual(v.unused, exc)


class ParseTest(unittest.TestCase):
    def test_kyverno_text_becomes_rule_ids_and_short_resources(self) -> None:
        text = (
            "policy k8s-013-readonly-rootfs -> resource inference/Deployment/vllm-a failed:\n"
            "1 - check validation failure: [K8S-013] ...\n"
            "policy ml-005-emptydir-sizelimit -> resource benchmark/Job/rollout-eval-x failed:\n"
            "policy k8s-011-required-labels applied to inference/Deployment/vllm-a: pass\n"
        )
        got = gc.parse_kyverno(text)
        self.assertEqual([(f.rule, f.resource) for f in got],
                         [("K8S-013", "Deployment/vllm-a"),
                          ("ML-005", "Job/rollout-eval-x")])

    def test_conftest_json_strips_the_container_path(self) -> None:
        payload = [{"filename": "/src/services/llm_pipeline/Dockerfile",
                    "failures": [{"msg": "[IMG-004] base image khong trong allowlist"}],
                    "warnings": []}]
        got = gc.parse_conftest(payload)
        self.assertEqual([(f.rule, f.resource) for f in got],
                         [("IMG-004", "services/llm_pipeline/Dockerfile")])

    def test_every_secret_is_img_001(self) -> None:
        payload = {"Results": [{"Target": "a.yaml",
                                "Secrets": [{"RuleID": "aws-access-key", "StartLine": 7}]}]}
        self.assertEqual([f.rule for f in gc.parse_trivy_secret(payload)], ["IMG-001"])

    def test_only_cves_with_a_fix_are_counted(self) -> None:
        """Một CRITICAL chưa ai vá được thì chặn merge không làm nó biến mất — nó chỉ dạy
        cả đội cách bỏ qua màu đỏ."""
        payload = {"Results": [{"Target": "img", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-1", "Severity": "CRITICAL", "FixedVersion": "2.0",
             "PkgName": "p", "InstalledVersion": "1.0"},
            {"VulnerabilityID": "CVE-2", "Severity": "CRITICAL", "PkgName": "q",
             "InstalledVersion": "1.0"},
            {"VulnerabilityID": "CVE-3", "Severity": "LOW", "FixedVersion": "2.0",
             "PkgName": "r", "InstalledVersion": "1.0"},
        ]}]}
        got = gc.parse_trivy_vuln(payload)
        self.assertEqual(len(got), 1)
        self.assertIn("CVE-1", got[0].detail)

    def test_empty_or_missing_output_is_not_a_crash(self) -> None:
        for payload in (None, {}, []):
            self.assertEqual(gc.parse_trivy_secret(payload), [])
            self.assertEqual(gc.parse_trivy_vuln(payload), [])
            self.assertEqual(gc.parse_conftest(payload), [])


class CommittedExceptionsTest(unittest.TestCase):
    """File ngoại lệ thật trong repo phải hợp lệ, nếu không CI đỏ vì cấu hình chứ không
    vì vi phạm — và thông báo lúc đó không nói rõ chuyện gì."""

    def test_the_committed_file_loads(self) -> None:
        levels = gc.Levels(frozenset(gc.HARD_GATE_FALLBACK), {})
        exceptions = gc.load_exceptions(ROOT / "security" / "gate-exceptions.yaml", levels)
        self.assertTrue(exceptions)
        for e in exceptions:
            self.assertTrue(e.reason.strip(), f"{e.rule} thieu ly do")
            self.assertTrue(e.owner.strip(), f"{e.rule} thieu nguoi chiu trach nhiem")

    def test_the_pinned_gate_ref_is_a_commit_sha(self) -> None:
        """Ghim `main` thì một thay đổi policy bên gate làm CI ở đây đỏ không báo trước."""
        ref = (ROOT / "security" / "gate-ref.txt").read_text(encoding="utf-8").splitlines()[0]
        self.assertRegex(ref.strip(), r"^[0-9a-f]{40}$")

    def test_the_workflow_reads_the_ref_from_that_file(self) -> None:
        """Hai nơi giữ SHA thì sớm muộn hai nơi chấm theo hai bản policy khác nhau."""
        wf = (ROOT / ".github" / "workflows" / "gate.yml").read_text(encoding="utf-8")
        self.assertIn("head -1 security/gate-ref.txt", wf)
        self.assertNotIn("GATE_REF: 6ff9d3b2", wf)


if __name__ == "__main__":
    unittest.main()
