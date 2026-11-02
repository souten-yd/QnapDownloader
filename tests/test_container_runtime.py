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
        for name in ("bridge", "host", "qnapdl-other", "qnapdl-evil-net"):
            with self.subTest(name=name), self.assertRaises(cr.ContainerError):
                self.rt.network_ensure(name)
        self.fake.networks[cr.JD2_NET] = {"Driver": "bridge", "Labels": {}, "Internal": False}
        with self.assertRaises(cr.ContainerError):
            self.rt.network_ensure(cr.JD2_NET)
        self.fake.networks[cr.JD2_NET] = {"Driver": "host", "Labels": {cr.LABEL_KEY: "1"}}
        with self.assertRaises(cr.ContainerError):
            self.rt.network_ensure(cr.JD2_NET)


class NotFoundTests(RuntimeBase):
    def _res(self, rc, err="", out=""):
        return SimpleNamespace(returncode=rc, stderr=err, stdout=out)

    def test_not_found_variants(self):
        for err in ("Error response from daemon: network qnapdl-jd2-net not found",
                    "Error: No such container: x", "Error: No such image: y",
                    "Error: No such object: z", "Error: image foo:1 not found",
                    "NETWORK X NOT FOUND"):
            with self.subTest(err=err):
                self.assertTrue(cr._not_found(self._res(1, err)))
                self.assertTrue(cr._not_found(self._res(1, "", err)))

    def test_other_errors_are_not_not_found(self):
        for err in ("permission denied", "Cannot connect to the Docker daemon at unix:///x",
                    "notfound", "cannot find", ""):
            with self.subTest(err=err):
                self.assertFalse(cr._not_found(self._res(1, err)))
        self.assertFalse(cr._not_found(self._res(0, "not found")))

    def test_network_ensure_creates_on_container_station_message(self):
        real = "Error response from daemon: network qnapdl-jd2-net not found"
        runtime = self.rt
        inner = runtime._docker

        def patched(args, timeout=60):
            if args[:2] == ["network", "inspect"] and cr.JD2_NET not in self.fake.networks:
                return self._res(1, real)
            return inner(args, timeout)
        runtime._docker = patched
        self.assertTrue(runtime.network_ensure(cr.JD2_NET))
        self.assertIn(cr.JD2_NET, self.fake.networks)

    def test_network_ensure_raises_on_permission_or_daemon_errors(self):
        for err in ("permission denied", "Cannot connect to the Docker daemon"):
            with self.subTest(err=err):
                self.rt._docker = lambda a, t=60, e=err: self._res(1, e)
                with self.assertRaises(cr.ContainerError):
                    self.rt.network_ensure(cr.JD2_NET)

    def test_container_inspect_not_found_vs_error(self):
        self.rt._docker = lambda a, t=60: self._res(1, "Error: container qnapdl-jd2 not found")
        self.assertIsNone(self.rt._inspect("qnapdl-jd2"))
        self.rt._docker = lambda a, t=60: self._res(1, "permission denied")
        with self.assertRaises(cr.ContainerError):
            self.rt._inspect("qnapdl-jd2")


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


class BrowserRuntimeTests(RuntimeBase):
    def context(self, dockerfile="FROM x\n", entry="#!/bin/sh\n"):
        ctx = Path(self.temp.name) / "ctx"
        ctx.mkdir(exist_ok=True)
        (ctx / "Dockerfile").write_text(dockerfile)
        (ctx / "entrypoint.sh").write_text(entry)
        return str(ctx)

    def test_tag_is_hash_of_dockerfile_and_entrypoint(self):
        import hashlib
        ctx = self.context("A", "B")
        self.assertEqual(self.rt.browser_tag(ctx),
                         "qnapdl-browser:" + hashlib.sha256(b"AB").hexdigest()[:12])
        (Path(ctx) / "entrypoint.sh").write_text("C")
        self.assertNotEqual(self.rt.browser_tag(ctx), "qnapdl-browser:" + hashlib.sha256(b"AB").hexdigest()[:12])

    def test_context_must_hold_exactly_the_shipped_files(self):
        ctx = self.context()
        (Path(ctx) / "extra").write_text("x")
        with self.assertRaises(cr.ContainerError):
            self.rt.browser_tag(ctx)
        (Path(ctx) / "extra").unlink()
        (Path(ctx) / "entrypoint.sh").unlink()
        (Path(ctx) / "entrypoint.sh").symlink_to(Path(ctx) / "Dockerfile")
        with self.assertRaises(cr.ContainerError):
            self.rt.browser_tag(ctx)

    def test_build_command_label_and_fixed_context(self):
        ctx = self.context()
        tag = self.rt.browser_build(ctx)
        build = [c for c in self.fake.docker_calls() if c[0] == "build"][0]
        self.assertEqual(build, ["build", "--label", "org.qnapdownloader.managed=1", "--tag", tag, ctx])
        self.assertTrue(self.rt.browser_image_present(tag))

    def test_only_own_tag_shapes_are_accepted(self):
        for bad in ("alpine", "qnapdl-browser:latest", "qnapdl-browser:" + "g" * 12,
                    "evil/qnapdl-browser:" + "a" * 12, "qnapdl-browser:" + "a" * 13):
            with self.subTest(tag=bad), self.assertRaises(cr.ContainerError):
                self.rt.browser_image_present(bad)
            with self.assertRaises(cr.ContainerError):
                self.rt.browser_run_args(bad)

    def test_exact_run_arguments(self):
        tag = "qnapdl-browser:0123456789ab"
        self.assertEqual(self.rt.browser_run_args(tag), [
            "run", "-d", "--name", "qnapdl-browser", "--label", "org.qnapdownloader.managed=1",
            "--network", "qnapdl-browser-net", "--restart", "no", "--read-only",
            "--log-driver", "local", "--log-opt", "max-size=256k", "--log-opt", "max-file=1",
            "--log-opt", "compress=false",
            "--memory", "2g", "--memory-swap", "2g",
            "--shm-size", "512m", "--pids-limit", "1024", "--cap-drop", "ALL",
            "--cap-add", "SYS_ADMIN",
            "--security-opt", "no-new-privileges", "--user", "1000:1000",
            "--tmpfs", "/profile:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=0700",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777", tag])

    def test_selftest_keeps_log_driver_none_and_jd2_is_unchanged(self):
        args = self.rt.browser_selftest_args("qnapdl-browser:0123456789ab")
        self.assertEqual(args[args.index("--log-driver") + 1], "none")
        self.assertNotIn("--log-opt", args)
        jd2 = self.rt.jd2_run_args("/a/jd2", "/a/dl", 1000, 1000)
        self.assertEqual(jd2[jd2.index("--log-driver") + 1], "none")
        self.assertNotIn("--log-opt", jd2)

    def test_exit_code_messages(self):
        self.assertEqual(set(cr.BROWSER_EXIT_MESSAGES), {70, 71, 72, 73})
        self.assertIn("時間切れ", cr.BROWSER_EXIT_MESSAGES[70])
        self.assertIn("Xvfb", cr.BROWSER_EXIT_MESSAGES[71])
        self.assertIn("Chromium", cr.BROWSER_EXIT_MESSAGES[72])
        self.assertIn("socat", cr.BROWSER_EXIT_MESSAGES[73])

    def test_logs_tail_is_label_checked_fixed_name_and_sanitized(self):
        self.fake.containers["qnapdl-browser"] = {
            "labels": {cr.LABEL_KEY: cr.LABEL_VALUE}, "running": False, "net": "n", "ip": "172.30.0.2",
            "ports": {}, "extra_nets": [], "exit": 72}
        self.fake.container_logs = "boom\x1b[31m red\x00\r\n" + "x" * 3000 + "END"
        text = self.rt.logs_tail("qnapdl-browser")
        self.assertLessEqual(len(text), cr.BROWSER_LOG_CHARS)
        self.assertTrue(text.endswith("END"))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x00", text)
        self.assertEqual(self.fake.docker_calls()[-1], ["logs", "--tail", "80", "qnapdl-browser"])
        with self.assertRaises(cr.ContainerError):
            self.rt.logs_tail("qnapdl-jd2")
        with self.assertRaises(cr.ContainerError):
            self.rt.logs_tail("other")

    def test_logs_tail_refuses_unlabelled_container_without_reading(self):
        self.fake.add_foreign("qnapdl-browser")
        with self.assertRaises(cr.ContainerError):
            self.rt.logs_tail("qnapdl-browser")
        self.assertFalse([c for c in self.fake.docker_calls() if c[0] == "logs"])

    def test_run_args_have_no_ports_mounts_privileges_or_sandbox_flags(self):
        args = self.rt.browser_run_args("qnapdl-browser:0123456789ab")
        joined = " ".join(args)
        for forbidden in ("-p", "-P", "--publish", "--mount", "-v", "--volume", "--privileged",
                          "--network=host", "seccomp", "apparmor", "--device",
                          "--pid", "--ipc"):
            self.assertNotIn(forbidden, args)
        for forbidden in ("no-sandbox", "disable-web-security", "user-agent"):
            self.assertNotIn(forbidden, joined)
        # SYS_ADMIN only, after ALL is dropped, on a non-root user with no-new-privileges.
        caps = [args[i + 1] for i, a in enumerate(args) if a == "--cap-add"]
        self.assertEqual(caps, ["SYS_ADMIN"])
        self.assertLess(args.index("--cap-drop"), args.index("--cap-add"))
        self.assertIn("no-new-privileges", args)
        self.assertEqual(args[args.index("--user") + 1], "1000:1000")

    def test_jd2_container_gets_no_cap_add(self):
        self.rt.run_jd2(str(self.jd2), str(self.dl), 1000, 1000)
        run = [c for c in self.fake.docker_calls() if c[0] == "run"][0]
        self.assertNotIn("--cap-add", run)

    def test_run_checks_image_and_two_gib_memory(self):
        tag = self.rt.browser_build(self.context())
        self.set_mem(1900 * 1024)
        with self.assertRaises(cr.ContainerError) as ctx:
            self.rt.run_browser(tag)
        self.assertIn("空きメモリ", str(ctx.exception))
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])
        self.set_mem(2200 * 1024)
        self.rt.run_browser(tag)
        self.assertIn("qnapdl-browser", self.fake.containers)
        self.assertEqual(self.rt.inspect_ip(cr.BROWSER_NAME, cr.BROWSER_NET), "172.30.0.2")
        with self.assertRaises(cr.ContainerError):
            self.rt.inspect_ip(cr.BROWSER_NAME, cr.JD2_NET)   # name/network must pair up
        with self.assertRaises(cr.ContainerError):
            self.rt.run_browser(tag)    # leftover with the same name

    def test_failed_run_removes_leftover_created_container(self):
        tag = self.rt.browser_build(self.context())
        self.set_mem(2200 * 1024)
        self.fake.run_fail = ("failed to create task for container: failed to initialize "
                              "logging driver: compression cannot be enabled when max file count is 1")
        with self.assertRaises(cr.ContainerError) as ctx:
            self.rt.run_browser(tag)
        self.assertIn("compression cannot be enabled", str(ctx.exception))
        self.assertNotIn(cr.BROWSER_NAME, self.fake.containers)
        self.assertIn(["rm", "-f", cr.BROWSER_NAME], self.fake.docker_calls())
        self.fake.run_fail = None
        self.rt.run_browser(tag)    # no name conflict on the next attempt
        self.assertIn(cr.BROWSER_NAME, self.fake.containers)

    def test_failed_run_never_removes_a_foreign_container(self):
        tag = self.rt.browser_build(self.context())
        self.set_mem(2200 * 1024)
        self.fake.run_fail = "boom"
        with self.assertRaises(cr.ContainerError):
            self.rt.run_browser(tag)
        self.fake.containers[cr.BROWSER_NAME] = {"labels": {}, "running": False, "net": "x",
                                                 "ip": "", "ports": {}, "extra_nets": [], "exit": 0}
        self.assertFalse(self.rt._inspect(cr.BROWSER_NAME)["managed"])
        with self.assertRaises(cr.ContainerError):
            self.rt.remove(cr.BROWSER_NAME)
        self.assertIn(cr.BROWSER_NAME, self.fake.containers)

    def test_run_without_image_is_refused(self):
        with self.assertRaises(cr.ContainerError):
            self.rt.run_browser("qnapdl-browser:0123456789ab")

    def test_selftest_uses_same_hardening_and_maps_userns_failure(self):
        tag = "qnapdl-browser:0123456789ab"
        self.assertTrue(self.rt.browser_selftest(tag))
        run = [c for c in self.fake.docker_calls() if c[0] == "run"][0]
        self.assertEqual(run[-2:], [tag, "selftest"])
        self.assertIn("--rm", run)
        self.assertIn("--read-only", run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.fake.selftest_code = cr.BROWSER_EXIT_NO_USERNS
        with self.assertRaises(cr.ContainerError) as err:
            self.rt.browser_selftest(tag)
        self.assertIn("ユーザー名前空間", str(err.exception))
        self.assertIn("サンドボックスを無効にする方法へは切り替えません", str(err.exception))
        self.assertIn("カーネル", str(err.exception))
        self.fake.selftest_code = cr.BROWSER_EXIT_CAPS
        with self.assertRaises(cr.ContainerError) as err:
            self.rt.browser_selftest(tag)
        self.assertIn("CapEff", str(err.exception))
        self.assertIn("ユーザー名前空間", str(err.exception))
        self.assertEqual(self.rt.browser_selftest_args(tag).count("--cap-add"), 1)

    def test_stats_for_own_container_only_and_parsing(self):
        self.fake.containers["qnapdl-browser"] = {
            "labels": {cr.LABEL_KEY: "1"}, "running": True, "net": cr.BROWSER_NET,
            "ip": "172.30.0.2", "ports": {}, "extra_nets": []}
        stats = self.rt.stats(cr.BROWSER_NAME)
        self.assertEqual(stats["limit_bytes"], 2 * 1024 ** 3)
        self.assertEqual(stats["percent"], 25.0)
        call = [c for c in self.fake.docker_calls() if c[0] == "stats"][0]
        self.assertEqual(call, ["stats", "--no-stream", "--format", "{{.MemUsage}}|{{.MemPerc}}",
                                "qnapdl-browser"])
        with self.assertRaises(cr.ContainerError):
            self.rt.stats("other-container")
        self.assertEqual(cr.parse_stats("1.5GiB / 2GiB|75.00%")["percent"], 75.0)
        with self.assertRaises(cr.ContainerError):
            cr.parse_stats("garbage")

    def test_prune_removes_only_older_hub_built_browser_images(self):
        self.fake.images.update({"qnapdl-browser:aaaaaaaaaaaa", "qnapdl-browser:bbbbbbbbbbbb"})
        self.assertEqual(self.rt.browser_prune_old("qnapdl-browser:bbbbbbbbbbbb"), 1)
        self.assertNotIn("qnapdl-browser:aaaaaaaaaaaa", self.fake.images)
        self.assertIn("qnapdl-browser:bbbbbbbbbbbb", self.fake.images)

    def test_stop_with_short_grace_and_foreign_container_refused(self):
        self.fake.containers["qnapdl-browser"] = {
            "labels": {cr.LABEL_KEY: "1"}, "running": True, "net": cr.BROWSER_NET,
            "ip": "172.30.0.2", "ports": {}, "extra_nets": []}
        self.assertTrue(self.rt.stop(cr.BROWSER_NAME, grace=cr.BROWSER_STOP_GRACE))
        self.assertIn(["stop", "-t", "5", "qnapdl-browser"], self.fake.docker_calls())
        self.fake.containers["qnapdl-browser"]["labels"] = {}
        with self.assertRaises(cr.ContainerError):
            self.rt.stop(cr.BROWSER_NAME)


class BrowserDockerfileStaticTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1] / "container" / "browser"

    def setUp(self):
        self.dockerfile = (self.ROOT / "Dockerfile").read_text()
        self.entry = (self.ROOT / "entrypoint.sh").read_text()

    def test_only_the_two_shipped_files(self):
        self.assertEqual({p.name for p in self.ROOT.iterdir()}, {"Dockerfile", "entrypoint.sh"})

    def test_base_image_is_the_pinned_digest(self):
        self.assertIn("FROM " + cr.IMAGE_DEBIAN + "\n", self.dockerfile)
        self.assertTrue(cr.IMAGE_DEBIAN.endswith(
            "7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8"))

    def test_packages_user_and_entrypoint(self):
        self.assertIn("apt-get install -y --no-install-recommends", self.dockerfile)
        for pkg in ("chromium", "xvfb", "socat", "fonts-noto-cjk", "ca-certificates", "dumb-init"):
            self.assertIn(pkg, self.dockerfile)
        self.assertIn("useradd -u 1000", self.dockerfile)
        self.assertIn("USER 1000:1000", self.dockerfile)
        self.assertIn('ENTRYPOINT ["dumb-init", "--"', self.dockerfile)

    def test_chromium_flags_exact_set_and_sandbox_kept(self):
        for flag in ("--remote-debugging-port=9222", "--user-data-dir=/profile", "--disk-cache-size=1",
                     "--media-cache-size=1", "--disable-breakpad", "--disable-crash-reporter",
                     "--no-first-run", "--no-default-browser-check", "--disable-sync",
                     "--disable-background-networking", "--lang=ja", "--window-size=1280,2000",
                     "about:blank"):
            self.assertIn(flag, self.entry)
        self.assertIn("Xvfb :99 -screen 0 1280x2000x24", self.entry)
        self.assertIn("socat TCP-LISTEN:9223,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222", self.entry)
        self.assertIn("exit 78", self.entry)

    def test_entrypoint_logs_to_tmpfs_and_distinct_exit_codes_with_tail(self):
        for text in ('>"$XVFB_LOG" 2>&1', '>"$CHROMIUM_LOG" 2>&1', "XVFB_LOG=/tmp/xvfb.log",
                     "CHROMIUM_LOG=/tmp/chromium.log", "tail -n 60", "qnapdl-browser: $1 exited",
                     "fail xvfb", "fail chromium", "fail socat", " 71", " 72", " 73", " 70",
                     "mkdir -p", "/tmp/.config", "/tmp/.cache", "--disable-gpu",
                     "--password-store=basic", "--no-default-browser-check"):
            self.assertIn(text, self.entry)
        self.assertNotIn("--disable-dev-shm-usage", self.entry)
        self.assertNotIn("no" + "-sandbox", self.entry)
        self.assertLess(self.entry.index("/tmp/.cache"), self.entry.index("chromium \\"))

    def test_entrypoint_refuses_to_run_with_effective_capabilities(self):
        guard = "grep -q '^CapEff:[[:space:]]*0000000000000000$' /proc/self/status"
        self.assertIn(guard, self.entry)
        self.assertIn("exit 79", self.entry)
        # the guard runs before the userns probe, the selftest exit and any process start
        for later in ("unshare --user", '= "selftest"', "Xvfb :99", "chromium \\"):
            self.assertLess(self.entry.index(guard), self.entry.index(later), later)

    def test_runtime_source_has_the_reasoning_comment_and_no_sandbox_off_switch(self):
        source = (Path(__file__).resolve().parents[1] / "container_runtime.py").read_text()
        self.assertIn("CAP_SYS_ADMIN", source)
        self.assertIn("bounding", source)
        self.assertNotIn("no-sandbox", source.lower())

    def test_no_sandbox_off_switch_ua_spoofing_or_web_security_flags(self):
        import re
        for text in (self.dockerfile, self.entry):
            low = text.lower()
            for forbidden in ("no-sandbox", "disable-web-security", "user-agent", "useragent",
                              "disable-setuid-sandbox", "disable-site-isolation", "ignore-certificate",
                              "--single-process", "remote-allow-origins"):
                self.assertNotIn(forbidden, low)
        self.assertIsNone(re.search(r"--remote-debugging-address", self.entry))
