import io
import unittest
import urllib.error
from unittest.mock import patch

from bench.scripts import provision_keys


class RateLimitProvisioningTests(unittest.TestCase):
    def test_team_is_created_and_shared_by_new_and_existing_keys(self):
        limit = {"team_id": "moc-shared", "rpm_limit": 3000}
        agents = [
            {"id": "agent-a", "models": ["qwen2.5-7b"], "budget_usd": 10},
            {"id": "agent-b", "models": ["qwen2.5-1.5b"], "budget_usd": 5},
        ]

        def api_call(base, master, path, payload=None, method="POST", timeout=30.0):
            if path.startswith("/team/info?"):
                raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())
            if path == "/key/list?return_full_object=true&size=100":
                return {"keys": [{"key_alias": "agent-a", "token": "hashed-a"}]}
            if path == "/key/generate":
                return {"key": "sk-new"}
            return {}

        with patch.object(provision_keys, "_call", side_effect=api_call) as call:
            self.assertEqual(provision_keys.ensure_rate_limit_team("proxy", "master", limit, agents), "created")
            result = provision_keys.provision("proxy", "master", agents, team_id=limit["team_id"])

        self.assertEqual([row[1] for row in result], ["updated", "created"])
        requests = [c.args[2:] for c in call.call_args_list]
        self.assertIn(("/team/new", {**limit, "members_with_roles": [
            {"role": "user", "user_id": "agent-a"},
            {"role": "user", "user_id": "agent-b"},
        ]}), requests)
        key_requests = [c.args[3] for c in call.call_args_list
                        if c.args[2] in ("/key/update", "/key/generate")]
        self.assertEqual([body["team_id"] for body in key_requests], ["moc-shared"] * 2)
        self.assertEqual([body["max_budget"] for body in key_requests], [10, 5])

    def test_existing_team_limit_is_updated_only_when_needed(self):
        limit = {"team_id": "moc-shared", "rpm_limit": 3000}
        agents = [{"id": "agent-a"}]
        team_info = {"members_with_roles": [{"user_id": "agent-a", "role": "user"}]}
        with patch.object(provision_keys, "_call", return_value={"team_info": {**team_info, "rpm_limit": 1000}}) as call:
            self.assertEqual(provision_keys.ensure_rate_limit_team("proxy", "master", limit, agents), "updated")
            self.assertEqual(call.call_args_list[-1].args[2:], ("/team/update", limit))
        with patch.object(provision_keys, "_call", return_value={"team_info": {**team_info, "rpm_limit": 3000}}) as call:
            self.assertEqual(provision_keys.ensure_rate_limit_team("proxy", "master", limit, agents), "unchanged")
            self.assertEqual(call.call_count, 1)

    def test_existing_team_gets_missing_member_before_keys(self):
        limit = {"team_id": "moc-shared", "rpm_limit": 3000}
        agents = [{"id": "agent-a"}, {"id": "agent-b"}]
        team = {"team_info": {"rpm_limit": 3000, "members_with_roles": [
            {"role": "user", "user_id": "agent-a"}]}}
        with patch.object(provision_keys, "_call", return_value=team) as call:
            self.assertEqual(provision_keys.ensure_rate_limit_team("proxy", "master", limit, agents), "updated")
            self.assertEqual(call.call_args_list[-1].args[2:], (
                "/team/member_add", {"team_id": "moc-shared", "member": {"role": "user", "user_id": "agent-b"}}))

    def test_team_lookup_failure_does_not_create_keys(self):
        error = urllib.error.HTTPError("/team/info", 500, "db unavailable", {}, io.BytesIO())
        with patch.object(provision_keys, "_call", side_effect=error) as call:
            with self.assertRaises(urllib.error.HTTPError):
                provision_keys.ensure_rate_limit_team(
                    "proxy", "master", {"team_id": "moc-shared", "rpm_limit": 3000}, [])
            self.assertEqual(call.call_count, 1)

    def test_dry_run_does_not_modify_team(self):
        with patch.object(provision_keys, "_call") as call:
            status = provision_keys.ensure_rate_limit_team(
                "proxy", "master", {"team_id": "moc-shared", "rpm_limit": 3000},
                [], dry_run=True)
        self.assertEqual(status, "would configure")
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
