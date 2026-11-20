#!/usr/bin/env python3
"""Review the hello-lab chart, deploy it, and run its smoke test.

The console binds to loopback and only accepts three fixed targets
(dev, qa, prod). Experiment fields are validated and passed to Helm as
a values file, never through a shell.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART = REPO_ROOT / "charts" / "hello-lab"
STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"
OVERRIDE_DIR = Path(os.environ.get("QA_LAB_OVERRIDE_DIR", "/tmp/qa-lab"))
RELEASE = "hello-lab"
SERVICE = "hello-lab"
HOST = os.environ.get("QA_LAB_HOST", "127.0.0.1")
PORT = int(os.environ.get("QA_LAB_PORT", "8787"))
MAX_BODY = 16_384
MAX_OUTPUT = 120_000
REQUEST_HEADER = "QALab"

TARGETS = {
    "dev": {
        "namespace": "dev",
        "values_file": "values-dev.yaml",
        "host": "hello-dev.localtest.me",
        "environment": "dev",
        "label": "Dev",
    },
    "qa": {
        "namespace": "qa",
        "values_file": "values-dev.yaml",
        "host": "hello-qa.localtest.me",
        "environment": "staging",
        "label": "QA",
    },
    "prod": {
        "namespace": "prod",
        "values_file": "values-prod.yaml",
        "host": "hello-prod.localtest.me",
        "environment": "prod",
        "label": "Prod",
    },
}

LOG_LEVELS = ("debug", "info", "warn", "error")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MESSAGE_RE = re.compile(r"^[ -~]{1,200}$")
IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
MUTATION = threading.Lock()


class LabError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def tool(name: str) -> str | None:
    found = shutil.which(name)
    return found


def ensure_kubeconfig() -> None:
    kubeconfig = Path.home() / ".kube" / "config"
    if kubeconfig.is_file():
        os.environ.setdefault("KUBECONFIG", str(kubeconfig))


def parse_experiment(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LabError("experiment must be an object")
    unknown = set(raw) - {"replicaCount", "message", "color", "logLevel"}
    if unknown:
        raise LabError("unknown experiment fields")
    if not raw:
        return None
    experiment: dict = {}
    if "replicaCount" in raw:
        count = raw["replicaCount"]
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 6:
            raise LabError("replicaCount must be an integer from 1 to 6")
        experiment["replicaCount"] = count
    if "message" in raw:
        message = raw["message"]
        if not isinstance(message, str) or MESSAGE_RE.fullmatch(message) is None:
            raise LabError("message must be 1-200 visible ASCII characters")
        experiment["message"] = message
    if "color" in raw:
        color = raw["color"]
        if not isinstance(color, str) or COLOR_RE.fullmatch(color) is None:
            raise LabError("color must be a #rrggbb hex color")
        experiment["color"] = color
    if "logLevel" in raw:
        level = raw["logLevel"]
        if level not in LOG_LEVELS:
            raise LabError("logLevel must be debug, info, warn, or error")
        experiment["logLevel"] = level
    return experiment or None


def target_from(value: object) -> tuple[str, dict]:
    if not isinstance(value, str) or value not in TARGETS:
        raise LabError("target must be dev, qa, or prod")
    return value, TARGETS[value]


def yaml_double(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_override(target_id: str, target: dict, experiment: dict | None) -> str | None:
    """Values overlay. QA always gets its own ingress host."""
    if experiment is None and target_id != "qa":
        return None
    lines: list[str] = []
    if experiment and "replicaCount" in experiment:
        count = experiment["replicaCount"]
        lines.append(f"replicaCount: {count}")
        lines.append("autoscaling:")
        lines.append("  enabled: false")
        if count < 2:
            lines.append("podDisruptionBudget:")
            lines.append("  enabled: false")
    lines.append("app:")
    lines.append(f"  environment: {target['environment']}")
    if experiment and "message" in experiment:
        lines.append(f"  message: {yaml_double(experiment['message'])}")
    if experiment and "color" in experiment:
        lines.append(f"  color: {yaml_double(experiment['color'])}")
    if experiment and "logLevel" in experiment:
        lines.append(f"  logLevel: {experiment['logLevel']}")
    if target_id == "qa":
        lines.append("ingress:")
        lines.append("  enabled: true")
        lines.append(f"  host: {target['host']}")
    return "\n".join(lines) + "\n"


def run(args: list[str], timeout: int) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LabError(f"timed out after {timeout}s", 504) from exc
    except FileNotFoundError as exc:
        raise LabError(f"{args[0]} is not installed", 503) from exc
    output = ((completed.stdout or "") + (completed.stderr or ""))[-MAX_OUTPUT:]
    return completed.returncode, output


def require_helm() -> str:
    found = tool("helm")
    if not found:
        raise LabError("helm is not installed", 503)
    return found


def require_kubectl() -> str:
    found = tool("kubectl")
    if not found:
        raise LabError("kubectl is not installed", 503)
    return found


def helm_values_args(target_id: str, target: dict, experiment: dict | None) -> tuple[list[str], str | None]:
    values = CHART / target["values_file"]
    if not values.is_file():
        raise LabError(f"missing {target['values_file']}", 500)
    args = ["-f", str(values)]
    overlay = render_override(target_id, target, experiment)
    if overlay is None:
        return args, None
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f"{target_id}-",
        suffix=".yaml",
        dir=OVERRIDE_DIR,
        delete=False,
    )
    try:
        handle.write(overlay)
    finally:
        handle.close()
    return args + ["-f", handle.name], handle.name


def chart_meta() -> dict:
    text = (CHART / "Chart.yaml").read_text(encoding="utf-8")

    def grab(key: str) -> str:
        match = re.search(rf'^{key}:\s*"?([^"\n]+?)"?\s*$', text, re.MULTILINE)
        return match.group(1).strip() if match else ""

    return {
        "name": grab("name") or "hello-lab",
        "version": grab("version"),
        "appVersion": grab("appVersion"),
        "description": grab("description"),
    }


def read_values() -> dict[str, str]:
    names = ("values.yaml", "values-dev.yaml", "values-prod.yaml")
    return {name: (CHART / name).read_text(encoding="utf-8") for name in names}


def iac_payload() -> dict:
    return {
        "ok": True,
        "chart": chart_meta(),
        "release": RELEASE,
        "targets": [
            {
                "id": key,
                "label": value["label"],
                "namespace": value["namespace"],
                "host": value["host"],
                "valuesFile": value["values_file"],
                "environment": value["environment"],
            }
            for key, value in TARGETS.items()
        ],
        "values": read_values(),
    }


def preview(target_id: str, experiment: dict | None) -> dict:
    _, target = target_from(target_id)
    helm = require_helm()
    values_args, overlay_path = helm_values_args(target_id, target, experiment)
    args = [helm, "template", RELEASE, str(CHART), *values_args]
    try:
        code, output = run(args, 60)
    finally:
        if overlay_path:
            Path(overlay_path).unlink(missing_ok=True)
    if code != 0:
        raise LabError(output.strip() or "helm template failed")
    return {"ok": True, "yaml": output, "command": " ".join(shlex.quote(part) for part in args)}


def deploy(target_id: str, experiment: dict | None) -> dict:
    _, target = target_from(target_id)
    helm = require_helm()
    values_args, overlay_path = helm_values_args(target_id, target, experiment)
    args = [
        helm,
        "upgrade",
        "--install",
        RELEASE,
        str(CHART),
        "-n",
        target["namespace"],
        "--create-namespace",
        *values_args,
        "--wait",
        "--timeout",
        "4m",
    ]
    try:
        code, output = run(args, 300)
    finally:
        if overlay_path:
            Path(overlay_path).unlink(missing_ok=True)
    if code != 0:
        raise LabError(output.strip() or "helm upgrade failed", 502)
    return {"ok": True, "output": output, "namespace": target["namespace"], "command": " ".join(shlex.quote(part) for part in args)}


def run_test(target_id: str) -> dict:
    _, target = target_from(target_id)
    helm = require_helm()
    args = [helm, "test", RELEASE, "-n", target["namespace"], "--timeout", "3m"]
    code, output = run(args, 200)
    if code != 0:
        raise LabError(output.strip() or "helm test failed", 502)
    return {"ok": True, "output": output}


def valid_ip(value: str) -> bool:
    if IP_RE.fullmatch(value) is None:
        return False
    return all(0 <= int(part) <= 255 for part in value.split("."))


def probe(target_id: str) -> dict:
    _, target = target_from(target_id)
    curl = tool("curl")
    if not curl:
        raise LabError("curl is not installed", 503)
    ingress_args = [
        curl,
        "-sS",
        "--max-time",
        "5",
        "-H",
        f"Host: {target['host']}",
        "-w",
        "\n%{http_code}",
        "http://127.0.0.1/",
    ]
    code, output = run(ingress_args, 15)
    via = f"http://127.0.0.1/ host {target['host']}"
    if code != 0:
        kubectl = require_kubectl()
        ip_code, ip_out = run(
            [
                kubectl,
                "-n",
                target["namespace"],
                "get",
                "svc",
                SERVICE,
                "-o",
                "jsonpath={.spec.clusterIP}",
            ],
            20,
        )
        cluster_ip = ip_out.strip()
        if ip_code != 0 or not valid_ip(cluster_ip):
            raise LabError(output.strip() or "service is not reachable", 502)
        code, output = run(
            [curl, "-sS", "--max-time", "5", "-w", "\n%{http_code}", f"http://{cluster_ip}/"],
            15,
        )
        via = f"http://{cluster_ip}/"
        if code != 0:
            raise LabError(output.strip() or "service is not reachable", 502)
    body, _, status_text = output.rpartition("\n")
    try:
        status = int(status_text)
    except ValueError:
        body = output
        status = 0
    summary = _summarize_body(body)
    return {"ok": status == 200, "status": status, "via": via, "body": summary}


def _summarize_body(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:2000]}
    if not isinstance(parsed, dict):
        return {"raw": text[:2000]}
    keep = {
        key: parsed[key]
        for key in ("hostname", "message", "version", "revision", "color")
        if key in parsed and isinstance(parsed[key], (str, int, float))
    }
    return keep or {"raw": text[:2000]}


def _loads(output: str, label: str) -> object:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise LabError(f"{label} returned unreadable output", 502) from exc


def cluster_status(target_id: str) -> dict:
    _, target = target_from(target_id)
    kubectl = require_kubectl()
    helm = tool("helm")
    node_code, node_out = run([kubectl, "get", "nodes", "-o", "json"], 20)
    if node_code != 0:
        return {
            "ok": True,
            "cluster": False,
            "error": node_out.strip() or "cluster is not reachable",
            "namespace": target["namespace"],
            "pods": [],
            "release": None,
        }
    nodes = _loads(node_out, "kubectl")
    ready = []
    if isinstance(nodes, dict):
        for item in nodes.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            conditions = item.get("status", {}).get("conditions", [])
            is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            ready.append({"name": name, "ready": is_ready})
    pod_code, pod_out = run(
        [
            kubectl,
            "-n",
            target["namespace"],
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/instance=" + RELEASE,
            "-o",
            "json",
        ],
        20,
    )
    pods = []
    if pod_code == 0:
        payload = _loads(pod_out, "kubectl")
        if isinstance(payload, dict):
            for item in payload.get("items", []):
                statuses = item.get("status", {}).get("containerStatuses") or []
                ready_count = sum(1 for container in statuses if container.get("ready"))
                restarts = sum(int(container.get("restartCount") or 0) for container in statuses)
                pods.append(
                    {
                        "name": item.get("metadata", {}).get("name", ""),
                        "phase": item.get("status", {}).get("phase", ""),
                        "ready": f"{ready_count}/{len(statuses)}" if statuses else "0/0",
                        "restarts": restarts,
                    }
                )
    release = None
    if helm:
        rel_code, rel_out = run([helm, "status", RELEASE, "-n", target["namespace"], "-o", "json"], 20)
        if rel_code == 0:
            info = _loads(rel_out, "helm")
            if isinstance(info, dict):
                release = {
                    "name": info.get("name"),
                    "namespace": info.get("namespace"),
                    "revision": info.get("version"),
                    "status": info.get("info", {}).get("status"),
                    "chart": info.get("chart", {}).get("metadata", {}).get("version"),
                }
    return {
        "ok": True,
        "cluster": True,
        "nodes": ready,
        "namespace": target["namespace"],
        "pods": pods,
        "release": release,
    }


def history(target_id: str) -> dict:
    _, target = target_from(target_id)
    helm = require_helm()
    code, output = run([helm, "history", RELEASE, "-n", target["namespace"], "-o", "json"], 20)
    if code != 0:
        if "not found" in output.lower():
            return {"ok": True, "revisions": []}
        raise LabError(output.strip() or "helm history failed", 502)
    rows = _loads(output, "helm")
    revisions = []
    if isinstance(rows, list):
        for row in rows[-20:]:
            if not isinstance(row, dict):
                continue
            revisions.append(
                {
                    "revision": row.get("revision"),
                    "status": row.get("status"),
                    "description": row.get("description") or "",
                    "updated": row.get("updated") or "",
                    "chart": row.get("chart") or "",
                }
            )
    return {"ok": True, "revisions": revisions}


def rollback(target_id: str, revision: object) -> dict:
    _, target = target_from(target_id)
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 500:
        raise LabError("revision must be an integer from 1 to 500")
    helm = require_helm()
    args = [helm, "rollback", RELEASE, str(revision), "-n", target["namespace"], "--wait", "--timeout", "4m"]
    code, output = run(args, 300)
    if code != 0:
        raise LabError(output.strip() or "helm rollback failed", 502)
    return {"ok": True, "output": output}


def health() -> dict:
    helm = tool("helm")
    kubectl = tool("kubectl")
    cluster = False
    if kubectl:
        code, _output = run([kubectl, "get", "--raw=/readyz"], 8)
        cluster = code == 0
    return {"ok": True, "helm": bool(helm), "kubectl": bool(kubectl), "cluster": cluster}


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length < 0 or length > MAX_BODY:
        raise LabError("request body is too large")
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LabError("request body must be JSON") from exc
    if not isinstance(parsed, dict):
        raise LabError("request body must be a JSON object")
    return parsed


class Handler(BaseHTTPRequestHandler):
    server_version = "QALab/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        if not STATIC_INDEX.is_file():
            self._send(500, {"ok": False, "error": "console page is missing"})
            return
        body = STATIC_INDEX.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _query_target(self) -> str:
        query = parse_qs(urlparse(self.path).query)
        value = (query.get("target") or ["qa"])[0]
        target_id, _target = target_from(value)
        return target_id

    def _guard_post(self) -> dict:
        if self.headers.get("X-Requested-With") != REQUEST_HEADER:
            raise LabError("missing QALab request header", 403)
        return _json_body(self)

    def _with_lock(self, action):
        if not MUTATION.acquire(timeout=1):
            raise LabError("another cluster action is still running", 409)
        try:
            return action()
        finally:
            MUTATION.release()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_html()
                return
            if path == "/api/health":
                self._send(200, health())
                return
            if path == "/api/iac":
                self._send(200, iac_payload())
                return
            if path == "/api/status":
                self._send(200, cluster_status(self._query_target()))
                return
            if path == "/api/history":
                self._send(200, history(self._query_target()))
                return
            if path == "/api/probe":
                self._send(200, probe(self._query_target()))
                return
            self._send(404, {"ok": False, "error": "not found"})
        except LabError as exc:
            self._send(exc.status, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._guard_post()
            target_id, _target = target_from(body.get("target", "qa"))
            if path == "/api/preview":
                experiment = parse_experiment(body.get("experiment"))
                self._send(200, preview(target_id, experiment))
                return
            if path == "/api/deploy":
                experiment = parse_experiment(body.get("experiment"))
                self._send(200, self._with_lock(lambda: deploy(target_id, experiment)))
                return
            if path == "/api/test":
                self._send(200, self._with_lock(lambda: run_test(target_id)))
                return
            if path == "/api/rollback":
                self._send(200, self._with_lock(lambda: rollback(target_id, body.get("revision"))))
                return
            self._send(404, {"ok": False, "error": "not found"})
        except LabError as exc:
            self._send(exc.status, {"ok": False, "error": str(exc)})


def main() -> None:
    ensure_kubeconfig()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"QA lab console at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
