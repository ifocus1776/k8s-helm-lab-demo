#!/usr/bin/env python3
"""Unit tests for the QA console. Helm is used only for template preview."""

from __future__ import annotations

import json
import os
import shutil
import threading
import unittest
import urllib.error
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


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    unittest.main()
