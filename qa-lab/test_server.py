#!/usr/bin/env python3
"""Unit tests for the QA console. Helm is used only for template preview."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import server


class ParseTests(unittest.TestCase):
    def test_accepts_a_qa_experiment(self) -> None:
        experiment = server.parse_experiment(
            {
                "replicaCount": 2,
                "message": "Hello from QA",
                "color": "#6A1B9A",
                "logLevel": "info",
            }
        )
        self.assertEqual(experiment["replicaCount"], 2)
        self.assertEqual(experiment["message"], "Hello from QA")

    def test_rejects_control_characters_and_overlong_messages(self) -> None:
        for message in ("hello\ninjection", "line\r", "bad\tgap", "x" * 201, ""):
            with self.assertRaises(server.LabError):
                server.parse_experiment({"message": message})

    def test_rejects_out_of_range_and_bool_replicas(self) -> None:
        for count in (0, 7, True, "2"):
            with self.assertRaises(server.LabError):
                server.parse_experiment({"replicaCount": count})

    def test_rejects_unknown_target_and_fields(self) -> None:
        with self.assertRaises(server.LabError):
            server.target_from("../prod")
        with self.assertRaises(server.LabError):
            server.parse_experiment({"image": "example.invalid/app:latest"})

    def test_qa_overlay_pins_ingress_host(self) -> None:
        text = server.render_override("qa", server.TARGETS["qa"], {"message": 'Say "hi"'})
        self.assertIn('message: "Say \\"hi\\""', text)
        self.assertIn("host: hello-qa.localtest.me", text)
        self.assertIn("environment: staging", text)
        self.assertNotIn("\nimage:", "\n" + text)

    def test_committed_dev_has_no_overlay(self) -> None:
        self.assertIsNone(server.render_override("dev", server.TARGETS["dev"], None))

    def test_low_replica_experiment_disables_disruption_budget(self) -> None:
        text = server.render_override("prod", server.TARGETS["prod"], {"replicaCount": 1})
        self.assertIn("replicaCount: 1", text)
        self.assertIn("enabled: false", text)


class CanaryEphemeralTests(unittest.TestCase):
    def test_weight_bounds(self) -> None:
        self.assertEqual(server.parse_weight(0), 0)
        self.assertEqual(server.parse_weight(100), 100)
        self.assertEqual(server.parse_weight(25), 25)
        for weight in (-1, 101, True, False, "50", 1.5, None, 50.0):
            with self.assertRaises(server.LabError):
                server.parse_weight(weight)

    def test_replica_weight_uses_half_up_and_keeps_one(self) -> None:
        self.assertEqual(server.canary_replica_count(3, 50), 2)
        self.assertEqual(server.canary_replica_count(1, 1), 1)
        self.assertEqual(server.canary_replica_count(1, 49), 1)
        self.assertEqual(server.canary_replica_count(4, 0), 0)
        self.assertEqual(server.canary_replica_count(2, 100), 2)

    def test_canary_release_name_is_fixed(self) -> None:
        self.assertEqual(server.CANARY_RELEASE, "hello-lab-canary")
        self.assertEqual(server.canary_release(), "hello-lab-canary")
        self.assertEqual(server.allow_release("hello-lab-canary"), "hello-lab-canary")
        with self.assertRaises(server.LabError):
            server.allow_release("hello-lab-canary;rm")
        with self.assertRaises(server.LabError):
            server.allow_release("other")

    def test_canary_overlay_does_not_take_a_caller_host_or_release(self) -> None:
        text = server.render_canary_overlay(
            "qa",
            server.TARGETS["qa"],
            {"message": 'Canary "one"', "replicaCount": 4, "color": "#112233", "logLevel": "warn"},
            50,
            server.canary_replica_count(3, 50),
            4,
        )
        self.assertIn("replicaCount: 2", text)
        self.assertIn('message: "Canary \\"one\\""', text)
        self.assertIn("host: hello-qa-canary.localtest.me", text)
        self.assertNotIn("host: hello-qa.localtest.me\n", text)
        self.assertIn('qa-lab/weight: "50"', text)
        self.assertIn('qa-lab/replica-intent: "4"', text)
        self.assertNotIn("image:", text)

    def test_promote_keeps_only_safe_fields(self) -> None:
        experiment = server.experiment_from_canary_values(
            {
                "replicaCount": 1,
                "app": {
                    "message": "Hello",
                    "color": "#112233",
                    "logLevel": "warn",
                    "environment": "staging",
                },
                "ingress": {"host": "hello-qa-canary.localtest.me"},
                "podAnnotations": {"qa-lab/weight": "50", "qa-lab/replica-intent": "3"},
                "image": {"repository": "example.invalid/app", "tag": "latest"},
            }
        )
        self.assertEqual(
            experiment,
            {"message": "Hello", "color": "#112233", "logLevel": "warn", "replicaCount": 3},
        )
        with self.assertRaises(server.LabError):
            server.experiment_from_canary_values(
                {"podAnnotations": {"qa-lab/replica-intent": "3;rm"}, "app": {"message": "Hello"}}
            )
        with self.assertRaises(server.LabError):
            server.experiment_from_canary_values({"app": {"message": "bad\nmessage"}})

    def test_ephemeral_namespace_regex(self) -> None:
        self.assertTrue(server.ephemeral_namespace_ok("eph-0123abcd"))
        for name in (
            "qa",
            "demo",
            "eph-HELLO",
            "eph-0123ABCD",
            "eph-0123abcd/../../qa",
            "../../eph-0123abcd",
            "eph-0123abcd/..",
            "eph-0123abc",
            "eph-0123abcde",
            "eph-0123abcz",
            "eph-0123abcd ",
            "kube-system",
            "default",
        ):
            self.assertFalse(server.ephemeral_namespace_ok(name), name)
            with self.assertRaises(server.LabError):
                server.require_ephemeral_namespace(name)

    def test_ephemeral_host_is_derived_from_the_generated_name(self) -> None:
        target = server.ephemeral_target("eph-0123abcd")
        self.assertEqual(target["namespace"], "eph-0123abcd")
        self.assertEqual(target["values_file"], "values-dev.yaml")
        self.assertEqual(target["environment"], "staging")
        self.assertEqual(target["host"], "hello-eph-0123abcd.localtest.me")
        text = server.render_override("ephemeral", target, None)
        self.assertIn("host: hello-eph-0123abcd.localtest.me", text)
        self.assertIn("environment: staging", text)

    def test_ephemeral_capacity(self) -> None:
        server.ensure_ephemeral_capacity(0)
        server.ensure_ephemeral_capacity(2)
        with self.assertRaises(server.LabError) as caught:
            server.ensure_ephemeral_capacity(3)
        self.assertEqual(caught.exception.status, 409)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_canary_template_uses_the_fixed_release_name(self) -> None:
        overlay = server.render_canary_overlay(
            "qa",
            server.TARGETS["qa"],
            {"message": "Hello from canary"},
            100,
            1,
            None,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlay.yaml"
            path.write_text(overlay, encoding="utf-8")
            completed = subprocess.run(
                [
                    "helm",
                    "template",
                    server.CANARY_RELEASE,
                    str(server.CHART),
                    "-f",
                    str(server.CHART / "values-dev.yaml"),
                    "-f",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("app.kubernetes.io/instance: hello-lab-canary", completed.stdout)
        self.assertIn('host: "hello-qa-canary.localtest.me"', completed.stdout)
        self.assertNotIn('host: "hello-qa.localtest.me"', completed.stdout)
        self.assertNotIn("app.kubernetes.io/instance: hello-lab\n", completed.stdout)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def request(self, path: str, body: dict | None = None, header: bool = True):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data)
        if body is not None and header:
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Requested-With", "QALab")
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_pages_and_validation(self) -> None:
        status, health = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        status, iac = self.request("/api/iac")
        self.assertEqual(status, 200)
        self.assertIn("values-dev.yaml", iac["values"])
        status, rejected = self.request("/api/preview", {"target": "qa", "experiment": {"message": "a\nb"}})
        self.assertEqual(status, 400)
        status, forbidden = self.request(
            "/api/preview",
            {"target": "qa"},
            header=False,
        )
        self.assertEqual(status, 403)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_preview_renders_qa_host(self) -> None:
        status, preview = self.request(
            "/api/preview",
            {"target": "qa", "experiment": {"message": "Hello from QA", "replicaCount": 1}},
        )
        self.assertEqual(status, 200, preview)
        self.assertIn("hello-qa.localtest.me", preview["yaml"])
        self.assertIn("Hello from QA", preview["yaml"])
        self.assertNotIn("image: latest", preview["yaml"])

    def test_canary_and_ephemeral_reject_unsafe_input_without_a_cluster_call(self) -> None:
        status, rejected = self.request("/api/canary", {"target": "qa", "weight": 101})
        self.assertEqual(status, 400, rejected)
        status, rejected = self.request("/api/canary", {"target": "qa", "weight": True})
        self.assertEqual(status, 400, rejected)
        status, rejected = self.request(
            "/api/canary",
            {"target": "qa", "weight": 10, "release": "hello-lab-canary", "namespace": "kube-system"},
        )
        self.assertEqual(status, 400, rejected)
        self.assertIn("unknown", rejected["error"])
        status, rejected = self.request("/api/canary/discard", {"target": "qa", "namespace": "qa"})
        self.assertEqual(status, 400, rejected)
        status, missing = self.request("/api/canary", {"target": "qa", "weight": 10}, header=False)
        self.assertEqual(status, 403, missing)
        for name in ("qa", "demo", "eph-HELLO", "eph-0123abcd/../../qa", "../qa", "eph-0123abcd/.."):
            status, rejected = self.request("/api/ephemeral/delete", {"namespace": name})
            self.assertEqual(status, 400, name)
            status, rejected = self.request_method(
                "DELETE",
                "/api/ephemeral?namespace=" + urllib.parse.quote(name, safe=""),
            )
            self.assertEqual(status, 400, name)
        status, rejected = self.request("/api/ephemeral", {"namespace": "qa", "experiment": {"message": "x"}})
        self.assertEqual(status, 400, rejected)

    def request_method(self, method: str, path: str):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        req.add_header("X-Requested-With", "QALab")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    unittest.main()
