"""In-memory stand-in for the Docker CLI used by container_runtime tests."""
import json
from types import SimpleNamespace

import container_runtime as cr

DOCKER = "/share/CS/bin/docker"


class FakeDocker:
    def __init__(self, has_cs=True):
        self.calls = []
        self.has_cs = has_cs
        self.images = set()
        self.networks = {}      # name -> {"Driver", "Labels", "Internal"}
        self.containers = {}    # name -> dict
        self.fail_pull = False
        self.mem_ok = True

    def isfile(self, path):
        return self.has_cs and path == DOCKER

    def result(self, code=0, out="", err=""):
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    def __call__(self, argv, timeout, env):
        self.calls.append(list(argv))
        assert isinstance(argv, list) and timeout > 0
        assert set(env) <= {"PATH", "HOME", "LANG", "DOCKER_CONFIG"}
        if argv[0] == cr.GETCFG:
            return self.result(0, "/share/CS\n") if self.has_cs else self.result(1)
        assert argv[0] == DOCKER, argv
        a = argv[1:]
        if a[0] == "version":
            return self.result(0, "20.10.24\n")
        if a[:2] == ["image", "inspect"]:
            return self.result(0 if a[-1] in self.images else 1, "", "Error: No such image")
        if a[0] == "pull":
            if self.fail_pull:
                return self.result(1, "", "pull access denied")
            self.images.add(a[1])
            return self.result(0, "done")
        if a[:2] == ["network", "inspect"]:
            net = self.networks.get(a[2])
            if net is None:
                return self.result(1, "[]", "Error: No such network: " + a[2])
            return self.result(0, json.dumps([net]))
        if a[:2] == ["network", "create"]:
            labels = {}
            for i, item in enumerate(a):
                if item == "--label":
                    k, _, v = a[i + 1].partition("=")
                    labels[k] = v
            self.networks[a[-1]] = {"Driver": a[a.index("--driver") + 1], "Labels": labels,
                                    "Internal": False}
            return self.result(0, "id")
        if a[:2] == ["container", "inspect"]:
            box = self.containers.get(a[2])
            if box is None:
                return self.result(1, "[]", "Error: No such container: " + a[2])
            return self.result(0, json.dumps([self.inspect_doc(a[2], box)]))
        if a[0] == "run":
            name = a[a.index("--name") + 1]
            if name in self.containers:
                return self.result(125, "", "Conflict")
            labels = {}
            for i, item in enumerate(a):
                if item == "--label":
                    k, _, v = a[i + 1].partition("=")
                    labels[k] = v
            self.containers[name] = {"labels": labels, "running": True,
                                     "net": a[a.index("--network") + 1],
                                     "ip": "172.30.0.2", "ports": {}, "extra_nets": []}
            return self.result(0, "abcdef123456\n")
        if a[0] == "stop":
            self.containers[a[-1]]["running"] = False
            return self.result(0, a[-1])
        if a[:2] == ["rm", "-f"]:
            self.containers.pop(a[2], None)
            return self.result(0, a[2])
        raise AssertionError("unexpected docker call: %r" % (a,))

    @staticmethod
    def inspect_doc(name, box):
        nets = {box["net"]: {"IPAddress": box["ip"]}}
        for extra in box["extra_nets"]:
            nets[extra] = {"IPAddress": "172.31.0.2"}
        return {"Name": "/" + name, "Config": {"Labels": box["labels"]},
                "State": {"Status": "running" if box["running"] else "exited",
                          "Running": box["running"]},
                "HostConfig": {"PortBindings": box["ports"]},
                "NetworkSettings": {"Networks": nets}}

    def docker_calls(self):
        return [c[1:] for c in self.calls if c[0] == DOCKER]

    def add_foreign(self, name, **extra):
        box = {"labels": {}, "running": True, "net": "bridge", "ip": "172.17.0.2",
               "ports": {}, "extra_nets": []}
        box.update(extra)
        self.containers[name] = box
