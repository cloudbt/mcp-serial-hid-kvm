"""Switching targets validates reachability before changing active context."""

import unittest
import json
import os
import tempfile
from unittest.mock import Mock, patch
from mcp_serial_hid_kvm.config import Config, targets_file_json

import mcp_serial_hid_kvm.server as srv


class MultiTargetTests(unittest.TestCase):
    def setUp(self):
        keys = ("_client", "_current_target", "_state", "_screen_size",
                "_cursor_pos", "_focused_shell_hint", "_input_lock")
        saved = {key: getattr(srv, key) for key in keys}
        self.addCleanup(lambda: [setattr(srv, key, value) for key, value in saved.items()])
        self.old_client = Mock()
        srv._client = self.old_client
        srv._current_target = {"name": "target1", "host": "127.0.0.1", "port": 9329}
        srv._state = srv.StateEngine()
        from PIL import Image
        srv._state.set_baseline(Image.new("RGB", (1920, 1080)))
        srv._state.stable_count = 3
        srv._state.last_regions = [{"x": 10}]
        self.old_baseline = srv._state.baseline
        self.old_generation = srv._state.generation
        srv._screen_size = (1920, 1080)
        srv._cursor_pos = (10, 20)
        srv._focused_shell_hint = "powershell"
        srv._input_lock = {"reason": "read-only test"}
        self.target_config = patch.object(srv.config, "targets", {
            "target1": {"host": "127.0.0.1", "port": 9329},
            "target2": {"host": "127.0.0.1", "port": 9331}})
        self.target_config.start()
        self.addCleanup(self.target_config.stop)

    def test_failure_retains_current_target_client_and_caches(self):
        candidate = Mock()
        candidate.ping.side_effect = RuntimeError("offline")
        with patch.object(srv, "KvmClient", return_value=candidate):
            result = srv._do_select_target(name="target2")
        self.assertFalse(result["ok"])
        self.assertEqual(result["current"]["name"], "target1")
        self.assertIs(srv._client, self.old_client)
        self.old_client.close.assert_not_called()
        self.assertIs(srv._state.baseline, self.old_baseline)
        self.assertEqual(srv._state.generation, self.old_generation)
        self.assertEqual(srv._state.stable_count, 3)
        self.assertEqual(srv._state.last_regions, [{"x": 10}])
        self.assertEqual(srv._screen_size, (1920, 1080))
        candidate.close.assert_called_once()

    def test_success_clears_target_specific_caches_but_keeps_input_lock(self):
        candidate = Mock()
        order = []
        candidate.ping.side_effect = lambda: order.append("ping")
        self.old_client.close.side_effect = lambda: order.append("close")
        with patch.object(srv, "KvmClient", return_value=candidate):
            with patch.object(srv, "_apply_hardware_timing", side_effect=lambda c: order.append("timing")):
                result = srv._do_select_target(name="target2")
        self.assertTrue(result["ok"])
        self.assertEqual(order, ["ping", "close", "timing"])
        self.assertIs(srv._client, candidate)
        self.assertEqual(srv._current_target["port"], 9331)
        self.assertIsNone(srv._state.baseline)
        self.assertEqual(srv._state.stable_count, 0)
        self.assertEqual(srv._state.last_regions, [])
        self.assertGreater(srv._state.generation, self.old_generation)
        for key in ("_screen_size", "_cursor_pos", "_focused_shell_hint"):
            self.assertIsNone(getattr(srv, key))
        self.assertEqual(srv._input_lock["reason"], "read-only test")


class SharedConfigTests(unittest.TestCase):
    def test_direct_python_mcp_loads_named_endpoints_without_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "targets.json")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"version": 1, "targets": {
                    "target1": {"api_port": 9329}, "target2": {"api_port": 9331}}}, output)
            with patch.dict(os.environ, {"SHKVM_TARGETS_CONFIG": path}):
                with patch.dict(os.environ, {}, clear=False):
                    previous = os.environ.pop("SHKVM_TARGETS", None)
                    try:
                        config = Config()
                    finally:
                        if previous is not None:
                            os.environ["SHKVM_TARGETS"] = previous
            self.assertEqual(config.targets["target2"]["port"], 9331)

    def test_invalid_file_keeps_default_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "targets.json")
            with open(path, "w", encoding="utf-8") as output:
                output.write('{"version":1,"targets":{"target1":{"api_port":0}}}')
            self.assertIsNone(targets_file_json(path))


if __name__ == "__main__":
    unittest.main()
