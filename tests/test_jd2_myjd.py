import json
import os
from pathlib import Path
import tempfile
import unittest

import jd2_myjd as jd


class FakeConfig:
    def __init__(self):
        self.values = {
            "linkgrabberautoconfirmenabled": True,
            "linkgrabberautostartenabled": True,
        }
        self.writes = []

    def list(self):
        return [{"interfaceName": "org.jdownloader.settings.LinkgrabberSettings",
                 "storage": "cfg/linkgrabber",
                 "key": key, "value": value}
                for key, value in self.values.items()]

    def get(self, interface, storage, key):
        return self.values[key]

    def set(self, interface, storage, key, value):
        self.values[key] = value
        self.writes.append(key)
        return True


class FakeAPI:
    def __init__(self):
        self.config = FakeConfig()
        self.calls = []
        self.jd = type("JD", (), {"get_core_revision": lambda _: 123456})()
        self.update = type("Update", (), {
            "is_update_available": lambda _: True,
            "run_update_check": lambda _: None,
            "restart_and_update": lambda _: True})()

    def connect(self, email, password):
        self.calls.append("connect")

    def update_devices(self):
        self.calls.append("update_devices")

    def list_devices(self):
        return [{"id": "dev1", "name": "NAS JD2", "type": "jd"}]

    def get_device(self, device_id):
        if device_id != "dev1":
            raise ValueError("wrong device")
        return self

    def disable_direct_connection(self):
        self.calls.append("disable_direct_connection")


class MyJDTests(unittest.TestCase):
    def opts(self):
        return {"email": "example@example.com", "password": "secret",
                "device_name": "NAS JD2"}

    def test_secure_config_only_and_no_default_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(jd.load_configuration(tmp))
            path = Path(tmp, "config", "myjd.json")
            path.parent.mkdir()
            path.write_text(json.dumps(self.opts()))
            path.chmod(0o644)
            with self.assertRaises(jd.MyJDUnavailable):
                jd.load_configuration(tmp)
            path.chmod(0o600)
            self.assertEqual(jd.load_configuration(tmp)["device_name"], "NAS JD2")
            path.unlink()
            path.symlink_to("/etc/passwd")
            with self.assertRaises(jd.MyJDUnavailable):
                jd.load_configuration(tmp)

    def test_authentication_and_read_only_introspection(self):
        api = FakeAPI()
        adapter = jd.MyJDAdapter(self.opts(), api_factory=lambda: api)
        self.assertEqual(adapter.core_revision(), 123456)
        self.assertEqual(api.calls, ["connect", "update_devices", "disable_direct_connection"])
        self.assertTrue(adapter.is_update_available())
        data = adapter.inspect_preset()
        self.assertEqual(data["jd_revision"], 123456)
        self.assertEqual(data["entries"]["linkgrabber.auto_start"]["current"], True)
        self.assertFalse(data["entries"]["packagizer.package_subfolders"]["supported"])
        self.assertEqual(api.config.writes, [])
        # Unverified settings cannot be mutated
        with self.assertRaises(jd.MyJDUnavailable):
            adapter.update_preset("packagizer.package_subfolders", False)

    def test_missing_secure_transport_capability_refuses_device(self):
        api = FakeAPI()
        api.disable_direct_connection = None
        adapter = jd.MyJDAdapter(self.opts(), api_factory=lambda: api)
        with self.assertRaises(jd.MyJDUnavailable):
            adapter.core_revision()
        self.assertIsNone(adapter._device)

    def test_direct_transport_is_disabled_before_any_jd_action(self):
        api = FakeAPI()
        def revision():
            if "disable_direct_connection" not in api.calls:
                raise AssertionError("Tried JD2 call while direct LAN was enabled")
            return 123456
        api.jd = type("JD", (), {"get_core_revision": lambda _: revision()})()
        adapter = jd.MyJDAdapter(self.opts(), api_factory=lambda: api)
        self.assertEqual(adapter.core_revision(), 123456)
        self.assertIn("disable_direct_connection", api.calls)

    def test_explicit_consent_required_for_jd2_updates(self):
        api = FakeAPI()
        adapter = jd.MyJDAdapter(self.opts(), api_factory=lambda: api)
        self.assertFalse(adapter.allow_update)
        with self.assertRaises(jd.MyJDUnavailable):
            adapter.restart_and_update()
        self.assertEqual(api.calls, [])
        config = dict(self.opts(), allow_jd2_update=True)
        allowed = jd.MyJDAdapter(config, api_factory=lambda: api)
        self.assertTrue(allowed.allow_update)
        allowed.restart_and_update()
        self.assertIn("connect", api.calls)
        self.assertIsNone(allowed._device)

    def test_default_factory_uses_real_myjdapi_class(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        generated = []
        def factory():
            api = FakeAPI()
            generated.append(api)
            return api
        fake_module = SimpleNamespace(Myjdapi=factory)
        with patch.dict(sys.modules, {"myjdapi": fake_module}):
            adapter = jd.MyJDAdapter(self.opts())
            self.assertEqual(adapter.core_revision(), 123456)
        self.assertEqual(len(generated), 1)

    def test_no_deprecated_direct_api_or_unsafe_credentials_log(self):
        import inspect
        src = inspect.getsource(jd.MyJDAdapter)
        self.assertNotIn("direct_connect(", src)
        self.assertNotIn("http://127.", src)
        self.assertNotIn("print(self._options", src)


if __name__ == "__main__":
    unittest.main()
