"""OpenCode v2 MCP configuration: format selection, rendering and cleanup."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.opencode import OpenCodeClientAdapter

pytestmark = pytest.mark.component

LOCAL_ENTRY = {"command": "npx", "args": ["-y", "pkg"], "env": {"KEY": "{env:KEY}"}}
REMOTE_ENTRY = {"url": "https://example.com/mcp", "headers": {"X-Api": "{env:API}"}}


class TestUsesNativeV2(unittest.TestCase):
    def test_servers_map_selects_v2(self):
        for config in ({"mcp": {"servers": {}}}, {"mcp": {"servers": {"a": {}}, "b": {}}}):
            with self.subTest(config=config):
                self.assertTrue(OpenCodeClientAdapter.uses_native_v2(config))

    def test_v1_or_missing_layout_keeps_v1(self):
        for config in (
            {},
            {"mcp": {}},
            {"mcp": {"local": {"type": "local", "command": ["x"]}}},
            {"mcp": {"servers": []}},
            {"mcp": []},
        ):
            with self.subTest(config=config):
                self.assertFalse(OpenCodeClientAdapter.uses_native_v2(config))

    def test_v1_server_named_servers_is_not_a_v2_map(self):
        config = {"mcp": {"servers": {"type": "remote", "url": "https://example.com"}}}
        self.assertFalse(OpenCodeClientAdapter.uses_native_v2(config))


class TestToOpencodeV2Format(unittest.TestCase):
    def test_local_entry(self):
        result = OpenCodeClientAdapter._to_opencode_v2_format(LOCAL_ENTRY)
        self.assertEqual(
            result,
            {"type": "local", "command": ["npx", "-y", "pkg"], "environment": {"KEY": "{env:KEY}"}},
        )

    def test_remote_entry(self):
        result = OpenCodeClientAdapter._to_opencode_v2_format(REMOTE_ENTRY)
        self.assertEqual(
            result,
            {"type": "remote", "url": "https://example.com/mcp", "headers": {"X-Api": "{env:API}"}},
        )

    def test_enabled_maps_to_disabled(self):
        disabled = OpenCodeClientAdapter._to_opencode_v2_format(LOCAL_ENTRY, enabled=False)
        enabled = OpenCodeClientAdapter._to_opencode_v2_format(LOCAL_ENTRY, enabled=True)
        self.assertIs(disabled["disabled"], True)
        self.assertNotIn("disabled", enabled)
        self.assertNotIn("enabled", disabled)
        self.assertNotIn("enabled", enabled)

    def test_non_boolean_enabled_is_rejected(self):
        for value in (None, "false", 0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OpenCodeClientAdapter._to_opencode_v2_format(LOCAL_ENTRY, enabled=value)

    def test_codemode_passthrough_is_preserved(self):
        result = OpenCodeClientAdapter._to_opencode_v2_format({**REMOTE_ENTRY, "codemode": False})
        self.assertIs(result["codemode"], False)


class _ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".opencode").mkdir()
        self.config_path = self.root / "opencode.json"
        cwd = patch("os.getcwd", return_value=self.tmp.name)
        cwd.start()
        self.addCleanup(cwd.stop)
        self.addCleanup(self.tmp.cleanup)
        self.adapter = OpenCodeClientAdapter()

    def write_config(self, config: dict) -> None:
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))


class TestUpdateConfigV2(_ProjectFixture):
    def test_writes_under_servers_and_preserves_unrelated_settings(self):
        self.write_config(
            {
                "theme": "dark",
                "mcp": {
                    "timeout": {"startup": 5000},
                    "servers": {"personal": {"type": "local", "command": ["me"]}},
                },
            }
        )

        self.assertTrue(self.adapter.update_config({"pkg": LOCAL_ENTRY}))

        config = self.read_config()
        self.assertEqual(config["theme"], "dark")
        self.assertEqual(config["mcp"]["timeout"], {"startup": 5000})
        self.assertEqual(config["mcp"]["servers"]["personal"], {"type": "local", "command": ["me"]})
        self.assertEqual(config["mcp"]["servers"]["pkg"]["command"], ["npx", "-y", "pkg"])
        self.assertNotIn("pkg", config["mcp"])

    def test_disabled_server(self):
        self.write_config({"mcp": {"servers": {}}})

        self.adapter.update_config({"pkg": REMOTE_ENTRY}, enabled=False)

        self.assertIs(self.read_config()["mcp"]["servers"]["pkg"]["disabled"], True)

    def test_non_boolean_enabled_leaves_file_unchanged(self):
        original = {"mcp": {"servers": {}}}
        self.write_config(original)

        self.assertFalse(self.adapter.update_config({"pkg": LOCAL_ENTRY}, enabled="yes"))

        self.assertEqual(self.read_config(), original)

    def test_moves_matching_legacy_entry(self):
        legacy = OpenCodeClientAdapter._to_opencode_format(LOCAL_ENTRY)
        self.write_config({"mcp": {"pkg": legacy, "servers": {}}})

        self.adapter.update_config({"pkg": LOCAL_ENTRY})

        config = self.read_config()
        self.assertNotIn("pkg", {k for k in config["mcp"] if k != "servers"})
        self.assertIn("pkg", config["mcp"]["servers"])

    def test_keeps_edited_legacy_entry(self):
        edited = {**OpenCodeClientAdapter._to_opencode_format(LOCAL_ENTRY), "cwd": "tools"}
        self.write_config({"mcp": {"pkg": edited, "servers": {}}})

        self.adapter.update_config({"pkg": LOCAL_ENTRY})

        config = self.read_config()
        self.assertEqual(config["mcp"]["pkg"], edited)
        self.assertIn("pkg", config["mcp"]["servers"])

    def test_v1_file_keeps_v1_layout(self):
        self.write_config({"mcp": {"personal": {"type": "local", "command": ["me"]}}})

        self.adapter.update_config({"pkg": LOCAL_ENTRY})

        config = self.read_config()
        self.assertNotIn("servers", config["mcp"])
        self.assertIs(config["mcp"]["pkg"]["enabled"], True)


class TestReadAndRenderV2(_ProjectFixture):
    def test_current_config_lists_effective_servers(self):
        self.write_config(
            {
                "mcp": {
                    "timeout": {"startup": 5000},
                    "legacy": {"type": "local", "command": ["old"]},
                    "both": {"type": "local", "command": ["v1"]},
                    "servers": {"both": {"type": "local", "command": ["v2"]}},
                }
            }
        )

        servers = self.adapter.get_current_config()["mcp"]

        self.assertEqual(set(servers), {"legacy", "both"})
        self.assertEqual(servers["both"]["command"], ["v2"])

    def test_render_matches_written_v2_entry(self):
        self.write_config({"mcp": {"servers": {}}})
        server_info = {
            "name": "pkg",
            "_raw_stdio": {"command": "npx", "args": ["-y", "pkg"], "env": {}},
        }

        rendered = self.adapter.render_server_config(server_info)

        self.assertNotIn("enabled", rendered)
        self.assertEqual(rendered["type"], "local")


class TestRemoveStaleV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".opencode").mkdir()
        self.config_path = self.root / "opencode.json"

    def remove_stale(self, names: set[str]) -> dict:
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        MCPIntegrator.remove_stale(names, runtime="opencode", project_root=self.root)
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_removes_from_servers_and_legacy_layout(self):
        self.config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "timeout": {"startup": 5000},
                        "stale": {"type": "local"},
                        "servers": {"stale": {"type": "local"}, "keep": {"type": "local"}},
                    }
                }
            ),
            encoding="utf-8",
        )

        mcp = self.remove_stale({"stale"})["mcp"]

        self.assertEqual(mcp["timeout"], {"startup": 5000})
        self.assertNotIn("stale", mcp)
        self.assertEqual(set(mcp["servers"]), {"keep"})

    def test_stale_name_never_removes_the_servers_map(self):
        self.config_path.write_text(
            json.dumps({"mcp": {"servers": {"servers": {"type": "local"}}}}), encoding="utf-8"
        )

        mcp = self.remove_stale({"servers"})["mcp"]

        self.assertEqual(mcp, {"servers": {}})

    def test_v1_server_named_servers_is_not_descended(self):
        entry = {"type": "remote", "url": "https://example.com", "stale": "value"}
        self.config_path.write_text(json.dumps({"mcp": {"servers": entry}}), encoding="utf-8")

        mcp = self.remove_stale({"stale"})["mcp"]

        self.assertEqual(mcp["servers"], entry)


if __name__ == "__main__":
    unittest.main()
