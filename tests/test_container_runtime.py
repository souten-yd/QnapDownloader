import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import container_runtime as cr
from fake_docker import DOCKER, FakeDocker


class RuntimeBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.jd2 = root / "jd2"
        self.jd2.mkdir()
        self.dl = root / "dl"
        self.dl.mkdir()
        os.chmod(str(self.dl), 0o777)
        self.meminfo = root / "meminfo"
        self.set_mem(4 * 1024 * 1024)
        self.fake = FakeDocker()
        self.fake.images.add(cr.IMAGE_JD2)
        self.rt = cr.Runtime(self.fake, isfile=self.fake.isfile, access=lambda p, m: True,
                             meminfo_path=str(self.meminfo))

    def set_mem(self, kib):
        self.meminfo.write_text("MemTotal: 16000000 kB\nMemAvailable: %d kB\n" % kib)


class LocateTests(RuntimeBase):
    def test_locates_via_getcfg(self):
        self.assertEqual(self.rt.locate(), DOCKER)
        self.assertEqual(self.fake.calls[0],
                         ["/sbin/getcfg", "container-station", "Install_Path",
                          "-f", "/etc/config/qpkg.conf"])
        self.assertTrue(self.rt.available())
        self.assertEqual(self.rt.version(), "20.10.24")

    def test_fallback_then_none(self):
        fake = FakeDocker()
        fake.result_getcfg_fail = True
        rt = cr.Runtime(lambda a, t, e: SimpleNamespace(returncode=1, stdout="", stderr=""),
                        isfile=lambda p: p == cr.FALLBACK_DOCKER, access=lambda p, m: True)
        self.assertEqual(rt.locate(), cr.FALLBACK_DOCKER)
        rt = cr.Runtime(lambda a, t, e: SimpleNamespace(returncode=1, stdout="", stderr=""),
                        isfile=lambda p: False, access=lambda p, m: True)
        self.assertIsNone(rt.locate())
        self.assertFalse(rt.available())
        self.assertIsNone(rt.version())
        with self.assertRaises(cr.ContainerError):
            rt.image_present(cr.IMAGE_JD2)

    def test_hostile_getcfg_output_is_ignored(self):
        for text in ("relative/path", "/a/../b", "/a\n/b"):
            rt = cr.Runtime(lambda a, t, e, text=text: SimpleNamespace(returncode=0, stdout=text, stderr=""),
                            isfile=lambda p: p != cr.FALLBACK_DOCKER, access=lambda p, m: True)
            self.assertIsNone(rt.locate(), text)


class ImageNetworkTests(RuntimeBase):
    def test_pull_only_pinned_images(self):
        self.fake.images.clear()
        for ref in ("alpine", "eclipse-temurin:21-jre", "eclipse-temurin:21-jre@sha256:" + "0" * 64,
                    "evil.example/x@sha256:" + "a" * 64):
            with self.subTest(ref=ref), self.assertRaises(cr.ContainerError):
                self.rt.pull(ref)
            with self.assertRaises(cr.ContainerError):
                self.rt.image_present(ref)
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "pull"], [])
        self.assertFalse(self.rt.image_present(cr.IMAGE_JD2))
        self.assertTrue(self.rt.pull(cr.IMAGE_JD2))
        self.assertIn(["pull", cr.IMAGE_JD2], self.fake.docker_calls())
        self.assertTrue(cr.IMAGE_JD2.endswith(
            "0b99b3376fbb50dbf9c0af8535fdd5b8eb0d3fdfc8b1e4967f383b6919f6901a"))

    def test_pull_failure_reports(self):
        self.fake.images.clear()
        self.fake.fail_pull = True
        with self.assertRaises(cr.ContainerError) as ctx:
            self.rt.pull(cr.IMAGE_JD2)
        self.assertIn("イメージを取得できません", str(ctx.exception))
        self.assertIn("pull access denied", str(ctx.exception))

    def test_docker_error_text_is_sanitized_and_limited_to_300_chars(self):
        noisy = "line one\n" + ("x" * 500) + "\nPATH=/usr/bin:/bin:/usr/sbin:/sbin"
        rt = cr.Runtime(lambda a, t, e: SimpleNamespace(returncode=1, stdout="", stderr=noisy),
                        isfile=lambda p: p == cr.FALLBACK_DOCKER, access=lambda p, m: True)
        rt.locate()
        with self.assertRaises(cr.ContainerError) as ctx:
            rt.pull(cr.IMAGE_JD2)
        text = str(ctx.exception).split(": ", 1)[1]
        self.assertTrue(text.startswith("line one xxx"))
        self.assertLessEqual(len(text), 300)
        self.assertNotIn("\n", text)
        rt = cr.Runtime(lambda a, t, e: SimpleNamespace(
            returncode=1, stdout="", stderr="env PATH=/usr/bin:/bin:/usr/sbin:/sbin:/x leaked"),
            isfile=lambda p: p == cr.FALLBACK_DOCKER, access=lambda p, m: True)
        rt.locate()
        with self.assertRaises(cr.ContainerError) as ctx:
            rt.pull(cr.IMAGE_JD2)
        self.assertNotIn("/usr/sbin:/sbin", str(ctx.exception))

    def test_network_create_label_and_reuse(self):
        self.assertTrue(self.rt.network_ensure(cr.JD2_NET))
        self.assertEqual(self.fake.networks[cr.JD2_NET]["Labels"], {cr.LABEL_KEY: "1"})
        self.assertIn(["network", "create", "--driver", "bridge", "--label", cr.LABEL, cr.JD2_NET],
                      self.fake.docker_calls())
        self.assertFalse(self.rt.network_ensure(cr.JD2_NET))

    def test_network_names_and_foreign_network_refused(self):
        for name in ("bridge", "host", "qnapdl-other", "qnapdl-browser-net"):
            with self.subTest(name=name), self.assertRaises(cr.ContainerError):
                self.rt.network_ensure(name)
        self.fake.networks[cr.JD2_NET] = {"Driver": "bridge", "Labels": {}, "Internal": False}
        with self.assertRaises(cr.ContainerError):
            self.rt.network_ensure(cr.JD2_NET)
        self.fake.networks[cr.JD2_NET] = {"Driver": "host", "Labels": {cr.LABEL_KEY: "1"}}
        with self.assertRaises(cr.ContainerError):
            self.rt.network_ensure(cr.JD2_NET)


class RunCommandTests(RuntimeBase):
    def test_exact_run_arguments(self):
        self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        run = [c for c in self.fake.docker_calls() if c[0] == "run"][0]
        self.assertEqual(run, [
            "run", "-d", "--name", "qnapdl-jd2", "--label", "org.qnapdownloader.managed=1",
            "--network", "qnapdl-jd2-net", "--restart", "no", "--read-only",
            "--log-driver", "none", "--memory", "1g", "--memory-swap", "1g",
            "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "1000:1000", "--workdir", "/jd2",
            "--env", "HOME=/jd2", "--env", "LANG=C.UTF-8",
            "--mount", "type=bind,source=%s,target=/jd2" % self.jd2,
            "--mount", "type=bind,source=%s,target=/downloads" % self.dl,
            "--tmpfs", "/jd2/logs:size=64m", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=64m",
            "eclipse-temurin:21-jre@sha256:0b99b3376fbb50dbf9c0af8535fdd5b8eb0d3fdfc8b1e4967f383b6919f6901a",
            "java", "-Xmx512m", "-XX:+UseSerialGC", "-Djava.awt.headless=true",
            "-jar", "/jd2/JDownloader.jar", "-norestart"])

    def test_no_published_ports_no_privileges_no_shell(self):
        self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        run = [c for c in self.fake.docker_calls() if c[0] == "run"][0]
        for forbidden in ("-p", "-P", "--publish", "--publish-all", "--privileged",
                          "--network=host", "--pid", "--ipc", "--device", "-v", "--volume",
                          "--restart=always", "unless-stopped"):
            self.assertNotIn(forbidden, run)
        self.assertNotIn("host", run)
        for call in self.fake.calls:
            self.assertIsInstance(call, list)

    def test_container_never_runs_as_root(self):
        for uid, gid in ((0, 0), (0, 1000), (1000, 0), ("1000", 1000), (None, None)):
            with self.subTest(uid=uid, gid=gid), self.assertRaises(cr.ContainerError):
                self.rt.run_jd2(str(self.jd2), str(self.dl), uid, gid)
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])

    def test_paths_that_could_inject_mount_options_are_refused(self):
        for bad in ("/share/a,readonly=false", '/share/"x', "relative", "/a/../b", "/a\nb"):
            with self.subTest(path=bad), self.assertRaises(cr.ContainerError):
                self.rt.run_jd2(str(self.jd2), bad, 1000, 1000)
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])

    def test_memory_guard(self):
        self.set_mem(int(1.49 * 1024 * 1024))
        with self.assertRaises(cr.ContainerError) as ctx:
            self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        self.assertIn("空きメモリが不足", str(ctx.exception))
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])
        self.set_mem(int(1.5 * 1024 * 1024))
        self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        self.assertIn(cr.JD2_NAME, self.fake.containers)

    def test_unreadable_meminfo_refuses(self):
        self.meminfo.write_text("MemTotal: 1 kB\n")
        with self.assertRaises(cr.ContainerError):
            self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)

    def test_refuses_without_pinned_image_or_when_name_taken(self):
        self.fake.images.clear()
        with self.assertRaises(cr.ContainerError):
            self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        self.fake.images.add(cr.IMAGE_JD2)
        self.fake.add_foreign(cr.JD2_NAME)
        with self.assertRaises(cr.ContainerError):
            self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)

    def test_download_dir_must_be_writable_for_container_user(self):
        os.chmod(str(self.dl), 0o755)   # owned by the test user, not uid 4242
        with self.assertRaises(cr.ContainerError) as ctx:
            self.rt.run_jd2(str(self.jd2), str(self.dl), 4242, 4242)
        self.assertIn("書き込み権限", str(ctx.exception))

    def test_writable_by(self):
        st = lambda mode, uid=1, gid=2: SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
        self.assertTrue(cr.writable_by(st(0o40777), 1000, 1000))
        self.assertFalse(cr.writable_by(st(0o40755), 1000, 1000))
        self.assertTrue(cr.writable_by(st(0o40755, uid=1000), 1000, 1000))
        self.assertTrue(cr.writable_by(st(0o40775, gid=1000), 1000, 1000))
        self.assertFalse(cr.writable_by(st(0o40664, gid=1000), 1000, 1000))   # no x


class ForeignContainerTests(RuntimeBase):
    def test_never_lists_other_containers(self):
        self.fake.add_foreign("jdownloader2")
        self.fake.add_foreign("other", labels={cr.LABEL_KEY: "1"})
        self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        self.rt.container_state(cr.JD2_NAME)
        self.rt.stop(cr.JD2_NAME)
        self.rt.remove(cr.JD2_NAME)
        for call in self.fake.docker_calls():
            self.assertNotIn(call[:2], (["ps"], ["container", "ls"]))
            self.assertNotEqual(call[0], "ps")
            self.assertNotIn("jdownloader2", call)
            self.assertNotIn("other", call)

    def test_arbitrary_names_refused(self):
        self.fake.add_foreign("jdownloader2")
        self.fake.add_foreign("qnapdl-other", labels={cr.LABEL_KEY: "1"})
        for name in ("jdownloader2", "qnapdl-other", "qnapdl-", "", "qnapdl-jd2;rm"):
            for action in (self.rt.container_state, self.rt.stop, self.rt.remove):
                with self.subTest(name=name, action=action.__name__), self.assertRaises(cr.ContainerError):
                    action(name)
            with self.assertRaises(cr.ContainerError):
                self.rt.inspect_ip(name, cr.JD2_NET)
        self.assertEqual(self.fake.docker_calls(), [])  # nothing reached Docker

    def test_unlabeled_container_with_our_name_is_not_touched(self):
        self.fake.add_foreign(cr.JD2_NAME)   # same name, no label
        state = self.rt.container_state(cr.JD2_NAME)
        self.assertFalse(state["managed"])
        for action in (self.rt.stop, self.rt.remove):
            with self.assertRaises(cr.ContainerError):
                action(cr.JD2_NAME)
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.JD2_NAME, cr.JD2_NET)
        self.assertTrue(self.fake.containers[cr.JD2_NAME]["running"])
        self.assertNotIn(["rm", "-f", cr.JD2_NAME], self.fake.docker_calls())
        self.assertFalse([c for c in self.fake.docker_calls() if c[0] == "stop"])

    def test_wrong_label_value_is_foreign(self):
        self.fake.add_foreign(cr.JD2_NAME, labels={cr.LABEL_KEY: "0"})
        with self.assertRaises(cr.ContainerError):
            self.rt.remove(cr.JD2_NAME)

    def test_stop_graceful_then_remove(self):
        self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        self.assertTrue(self.rt.stop(cr.JD2_NAME))
        self.assertIn(["stop", "-t", "30", cr.JD2_NAME], self.fake.docker_calls())
        self.assertFalse(self.rt.stop(cr.JD2_NAME))   # already stopped
        self.assertTrue(self.rt.remove(cr.JD2_NAME))
        self.assertIn(["rm", "-f", cr.JD2_NAME], self.fake.docker_calls())
        self.assertFalse(self.rt.remove(cr.JD2_NAME))
        self.assertEqual(self.rt.container_state(cr.JD2_NAME)["status"], "absent")


class InspectIpTests(RuntimeBase):
    def setUp(self):
        super().setUp()
        self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)

    def test_returns_verified_ip(self):
        self.assertEqual(self.rt.inspect_ip(cr.JD2_NAME, cr.JD2_NET), "172.30.0.2")

    def test_refuses_unsafe_states(self):
        box = self.fake.containers[cr.JD2_NAME]
        box["ports"] = {"3128/tcp": [{"HostPort": "3128"}]}
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.JD2_NAME, cr.JD2_NET)
        box["ports"] = {}
        box["extra_nets"] = ["bridge"]
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.JD2_NAME, cr.JD2_NET)
        box["extra_nets"] = []
        box["ip"] = "8.8.8.8"
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.JD2_NAME, cr.JD2_NET)
        box["ip"] = "127.0.0.1"
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.JD2_NAME, cr.JD2_NET)
        box["ip"] = "172.30.0.2"
        box["running"] = False
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.JD2_NAME, cr.JD2_NET)
        box["running"] = True
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.JD2_NAME, "bridge")


class OwnerTests(unittest.TestCase):
    def test_root_owned_tree_is_handed_to_1000_without_following_links(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "cfg").mkdir()
            (base / "cfg" / "a.json").write_text("{}")
            os.symlink("/etc", str(base / "link"))
            changed = []
            real = os.stat(str(base))
            root_stat = SimpleNamespace(st_uid=0, st_gid=0)
            uid, gid = cr.prepare_data_owner(
                str(base), euid=0, stat=lambda p: root_stat,
                lstat=lambda p: SimpleNamespace(st_uid=0, st_gid=0),
                lchown=lambda p, u, g: changed.append((p, u, g)))
            self.assertEqual((uid, gid), (1000, 1000))
            paths = {c[0] for c in changed}
            self.assertIn(str(base), paths)
            self.assertIn(str(base / "cfg" / "a.json"), paths)
            self.assertIn(str(base / "link"), paths)   # the link itself, never its target
            self.assertTrue(all(p.startswith(str(base)) for p in paths))
            self.assertTrue(all((u, g) == (1000, 1000) for _, u, g in changed))

    def test_non_root_owner_is_used_and_root_container_refused(self):
        owner = SimpleNamespace(st_uid=1026, st_gid=100)
        self.assertEqual(cr.prepare_data_owner("/x", euid=0, stat=lambda p: owner), (1026, 100))
        root = SimpleNamespace(st_uid=0, st_gid=0)
        with self.assertRaises(cr.ContainerError):
            cr.prepare_data_owner("/x", euid=1026, stat=lambda p: root)


class StaticTests(unittest.TestCase):
    def test_no_shell_and_no_open_ended_docker_surface(self):
        source = (Path(__file__).resolve().parents[1] / "container_runtime.py").read_text()
        for forbidden in ("shell=True", "os.system", "os.popen", "docker.sock", "--privileged",
                          '"ps"', '"ls"', "no-sandbox"):
            self.assertNotIn(forbidden, source)

    def test_pinned_digests_match_the_spec(self):
        spec = (Path(__file__).resolve().parents[1] / "docs" / "container-runtime.md").read_text()
        for ref in cr.PINNED_IMAGES:
            self.assertIn(ref, spec)


if __name__ == "__main__":
    unittest.main()
