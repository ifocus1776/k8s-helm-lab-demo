#!/usr/bin/env python3
"""Review the hello-lab chart, deploy it, and run its smoke test.

The console binds to loopback and only accepts three fixed targets
(dev, qa, prod). Experiment fields are validated and passed to Helm as
a values file, never through a shell.

Canary deploys are a second fixed release in the selected namespace.
Ephemeral environments are server-named namespaces on this same cluster.
"""

from __future__ import annotations

import json
import os
import re
import secrets
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
CANARY_RELEASE = "hello-lab-canary"
SERVICE = "hello-lab"
MAX_EPHEMERAL = 3
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
EPH_RE = re.compile(r"^eph-[0-9a-f]{8}$")
NAMESPACE_RE = re.compile(r"^(?:dev|qa|prod|eph-[0-9a-f]{8})$")
HOST_RE = re.compile(r"^hello-[a-z0-9-]{1,48}\.localtest\.me$")
PREFIX_RE = re.compile(r"^[a-z0-9-]{1,40}$")
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


def reject_unknown(body: dict, allowed: set[str]) -> None:
    if set(body) - allowed:
        raise LabError("unknown fields")


def parse_weight(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 100:
        raise LabError("weight must be an integer from 0 to 100")
    return raw


def canary_replica_count(stable_replicas: int, weight: int) -> int:
    """Replica canary size. Half-up rounding; weight 0 keeps zero replicas."""
    if isinstance(stable_replicas, bool) or not isinstance(stable_replicas, int):
        raise LabError("stable replica count is unreadable", 502)
    if weight <= 0:
        return 0
    scaled = (stable_replicas * weight + 50) // 100
    return max(1, scaled)


def canary_release() -> str:
    return CANARY_RELEASE


def canary_host(target_id: str) -> str:
    if target_id not in TARGETS:
        raise LabError("target must be dev, qa, or prod", 500)
    return allow_host(f"hello-{target_id}-canary.localtest.me")


def ephemeral_namespace_ok(name: object) -> bool:
    return isinstance(name, str) and EPH_RE.fullmatch(name) is not None


def require_ephemeral_namespace(name: object) -> str:
    if not ephemeral_namespace_ok(name):
        raise LabError("namespace must be an ephemeral lab namespace")
    return name


def allow_namespace(namespace: str) -> str:
    if NAMESPACE_RE.fullmatch(namespace) is None:
        raise LabError("namespace is not allowed", 500)
    return namespace


def allow_release(release: str) -> str:
    if release not in {RELEASE, CANARY_RELEASE}:
        raise LabError("release is not allowed", 500)
    return release


def allow_host(host: str) -> str:
    if HOST_RE.fullmatch(host) is None:
        raise LabError("ingress host is not allowed", 500)
    return host


def allow_chart() -> str:
    chart = CHART.resolve()
    if chart.name != "hello-lab" or not (chart / "Chart.yaml").is_file():
        raise LabError("chart path is not available", 500)
    return str(chart)


def ensure_ephemeral_capacity(count: int) -> None:
    if count >= MAX_EPHEMERAL:
        raise LabError("ephemeral namespace limit reached", 409)


def yaml_double(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_override(target_id: str, target: dict, experiment: dict | None) -> str | None:
    """Values overlay. QA and ephemeral deploys always pin an ingress host."""
    if experiment is None and target_id not in {"qa", "ephemeral"}:
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
    if target_id in {"qa", "ephemeral"}:
        lines.append("ingress:")
        lines.append("  enabled: true")
        lines.append(f"  host: {allow_host(target['host'])}")
    return "\n".join(lines) + "\n"


def render_canary_overlay(
    target_id: str,
    target: dict,
    experiment: dict | None,
    weight: int,
    replicas: int,
    replica_intent: int | None,
) -> str:
    """Committed values stay in the chart file. This overlay is the canary delta."""
    environment = target["environment"]
    if environment not in {"lab", "dev", "staging", "prod"}:
        raise LabError("environment is not allowed", 500)
    lines = [
        f"replicaCount: {replicas}",
        "autoscaling:",
        "  enabled: false",
    ]
    if replicas < 2:
        lines.extend(["podDisruptionBudget:", "  enabled: false"])
    lines.extend(["app:", f"  environment: {environment}"])
    if experiment and "message" in experiment:
        lines.append(f"  message: {yaml_double(experiment['message'])}")
    if experiment and "color" in experiment:
        lines.append(f"  color: {yaml_double(experiment['color'])}")
    if experiment and "logLevel" in experiment:
        lines.append(f"  logLevel: {experiment['logLevel']}")
    lines.extend(
        [
            "ingress:",
            "  enabled: true",
            f"  host: {canary_host(target_id)}",
            "podAnnotations:",
            f'  qa-lab/weight: "{weight}"',
        ]
    )
    if replica_intent is not None:
        lines.append(f'  qa-lab/replica-intent: "{replica_intent}"')
    return "\n".join(lines) + "\n"


def experiment_from_canary_values(values: dict) -> dict | None:
    """Copy only message, color, log level, and replica intent. Ignore everything else."""
    if not isinstance(values, dict):
        raise LabError("canary values were not an object", 502)
    raw: dict = {}
    app = values.get("app")
    if isinstance(app, dict):
        for key in ("message", "color", "logLevel"):
            if key in app:
                raw[key] = app[key]
    annotations = values.get("podAnnotations")
    if isinstance(annotations, dict) and "qa-lab/replica-intent" in annotations:
        intent = annotations["qa-lab/replica-intent"]
        if not isinstance(intent, str) or re.fullmatch(r"[1-6]", intent) is None:
            raise LabError("canary replica intent is not safe to promote")
        raw["replicaCount"] = int(intent)
    if not raw:
        return None
    return parse_experiment(raw)


def run(args: list[str], timeout: int) -> tuple[int, str]:
    try:
        # Close stdin so kubectl cannot block on a basic-auth prompt.
        completed = subprocess.run(
            args,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
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


def _values_file(target: dict) -> Path:
    name = target["values_file"]
    if name not in {"values-dev.yaml", "values-prod.yaml"}:
        raise LabError("values file is not allowed", 500)
    values = (CHART / name).resolve()
    if values.parent != CHART.resolve() or not values.is_file():
        raise LabError(f"missing {name}", 500)
    return values


def helm_values_args(
    target_id: str,
    target: dict,
    experiment: dict | None,
    overlay: str | None = None,
    prefix: str | None = None,
) -> tuple[list[str], str | None]:
    values = _values_file(target)
    args = ["-f", str(values)]
    if overlay is None:
        overlay = render_override(target_id, target, experiment)
    if overlay is None:
        return args, None
    safe_prefix = prefix or target_id
    if PREFIX_RE.fullmatch(safe_prefix) is None:
        raise LabError("overlay prefix is not allowed", 500)
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f"{safe_prefix}-",
        suffix=".yaml",
        dir=OVERRIDE_DIR,
        delete=False,
    )
    try:
        handle.write(overlay)
    finally:
        handle.close()
    return args + ["-f", handle.name], handle.name


def _assert_values_args(values_args: list[str]) -> None:
    if len(values_args) % 2 != 0:
        raise LabError("values arguments are not allowed", 500)
    chart_root = CHART.resolve()
    override_root = OVERRIDE_DIR.resolve()
    for flag, path in zip(values_args[0::2], values_args[1::2]):
        if flag != "-f":
            raise LabError("values arguments are not allowed", 500)
        resolved = Path(path).resolve()
        allowed_chart = resolved.parent == chart_root and resolved.suffix == ".yaml"
        allowed_overlay = override_root in resolved.parents and resolved.suffix == ".yaml"
        if not allowed_chart and not allowed_overlay:
            raise LabError("values file is not allowed", 500)


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


def helm_upgrade_install(release: str, namespace: str, values_args: list[str]) -> dict:
    helm = require_helm()
    release = allow_release(release)
    namespace = allow_namespace(namespace)
    _assert_values_args(values_args)
    args = [
        helm,
        "upgrade",
        "--install",
        release,
        allow_chart(),
        "-n",
        namespace,
        "--create-namespace",
        *values_args,
        "--wait",
        "--timeout",
        "4m",
    ]
    code, output = run(args, 300)
    if code != 0:
        raise LabError(output.strip() or "helm upgrade failed", 502)
    return {
        "ok": True,
        "output": output,
        "namespace": namespace,
        "release": release,
        "command": " ".join(shlex.quote(part) for part in args),
    }


def helm_uninstall(release: str, namespace: str) -> str:
    helm = require_helm()
    release = allow_release(release)
    namespace = allow_namespace(namespace)
    args = [helm, "uninstall", release, "-n", namespace, "--wait", "--timeout", "4m"]
    code, output = run(args, 300)
    if code != 0:
        if "not found" in output.lower():
            raise LabError(f"{release} is not installed", 404)
        raise LabError(output.strip() or "helm uninstall failed", 502)
    return output


def helm_get_values(release: str, namespace: str) -> dict | None:
    """User-supplied values for a release, or None when it is not installed."""
    helm = require_helm()
    release = allow_release(release)
    namespace = allow_namespace(namespace)
    code, output = run([helm, "get", "values", release, "-n", namespace, "-o", "json"], 20)
    if code != 0:
        if "not found" in output.lower():
            return None
        raise LabError(output.strip() or "helm get values failed", 502)
    if output.strip() in {"", "null"}:
        return {}
    parsed = _loads(output, "helm")
    if not isinstance(parsed, dict):
        raise LabError("helm values were not an object", 502)
    return parsed


def deploy(target_id: str, experiment: dict | None) -> dict:
    _, target = target_from(target_id)
    values_args, overlay_path = helm_values_args(target_id, target, experiment)
    try:
        return helm_upgrade_install(RELEASE, target["namespace"], values_args)
    finally:
        if overlay_path:
            Path(overlay_path).unlink(missing_ok=True)


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


def stable_replica_count(target: dict) -> int:
    values = helm_get_values(RELEASE, target["namespace"])
    if isinstance(values, dict):
        count = values.get("replicaCount")
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 20:
            return count
    text = _values_file(target).read_text(encoding="utf-8")
    match = re.search(r"(?m)^replicaCount:\s*([0-9]+)\s*$", text)
    if match is None:
        raise LabError("stable replica count is unreadable", 502)
    count = int(match.group(1))
    if count > 20:
        raise LabError("stable replica count is unreadable", 502)
    return count


def _release_installed(release: str, namespace: str) -> bool:
    helm = require_helm()
    code, output = run(
        [helm, "status", allow_release(release), "-n", allow_namespace(namespace), "-o", "json"],
        20,
    )
    if code == 0:
        return True
    if "not found" in output.lower():
        return False
    lowered = output.lower()
    if "unreachable" in lowered or "connection refused" in lowered or "unable to connect" in lowered:
        raise LabError(output.strip() or "cluster is not reachable", 503)
    raise LabError(output.strip() or "helm status failed", 502)


def deploy_canary(target_id: str, experiment: dict | None, weight: int) -> dict:
    _, target = target_from(target_id)
    weight = parse_weight(weight)
    stable = stable_replica_count(target)
    replicas = canary_replica_count(stable, weight)
    intent = experiment.get("replicaCount") if experiment else None
    overlay = render_canary_overlay(target_id, target, experiment, weight, replicas, intent)
    values_args, overlay_path = helm_values_args(
        target_id,
        target,
        None,
        overlay=overlay,
        prefix=f"canary-{target_id}",
    )
    try:
        result = helm_upgrade_install(CANARY_RELEASE, target["namespace"], values_args)
    finally:
        if overlay_path:
            Path(overlay_path).unlink(missing_ok=True)
    result.update(
        {
            "weight": weight,
            "replicas": replicas,
            "stableReplicas": stable,
            "host": canary_host(target_id),
            "mode": "replica",
        }
    )
    return result


def canary_status(target_id: str) -> dict:
    _, target = target_from(target_id)
    namespace = target["namespace"]
    base = {
        "ok": True,
        "release": CANARY_RELEASE,
        "namespace": namespace,
        "mode": "replica",
        "host": canary_host(target_id),
        "installed": False,
        "pods": [],
    }
    try:
        installed = _release_installed(CANARY_RELEASE, namespace)
    except LabError as exc:
        if exc.status == 503:
            base["cluster"] = False
            base["error"] = str(exc)
            return base
        raise
    base["cluster"] = True
    if not installed:
        return base
    values = helm_get_values(CANARY_RELEASE, namespace) or {}
    annotations = values.get("podAnnotations") if isinstance(values.get("podAnnotations"), dict) else {}
    weight_text = annotations.get("qa-lab/weight") if isinstance(annotations, dict) else None
    intent_text = annotations.get("qa-lab/replica-intent") if isinstance(annotations, dict) else None
    app = values.get("app") if isinstance(values.get("app"), dict) else {}
    base.update(
        {
            "installed": True,
            "weight": int(weight_text) if isinstance(weight_text, str) and weight_text.isdigit() else None,
            "replicas": values.get("replicaCount") if isinstance(values.get("replicaCount"), int) else None,
            "replicaIntent": int(intent_text) if isinstance(intent_text, str) and re.fullmatch(r"[1-6]", intent_text) else None,
            "message": app.get("message") if isinstance(app.get("message"), str) else None,
            "color": app.get("color") if isinstance(app.get("color"), str) else None,
            "logLevel": app.get("logLevel") if isinstance(app.get("logLevel"), str) else None,
            "pods": _pods(namespace, CANARY_RELEASE),
        }
    )
    return base


def promote_canary(target_id: str) -> dict:
    _, target = target_from(target_id)
    values = helm_get_values(CANARY_RELEASE, target["namespace"])
    if values is None:
        raise LabError("hello-lab-canary is not installed", 404)
    experiment = experiment_from_canary_values(values)
    upgraded = deploy(target_id, experiment)
    uninstall = helm_uninstall(CANARY_RELEASE, target["namespace"])
    return {
        "ok": True,
        "namespace": target["namespace"],
        "release": RELEASE,
        "promoted": experiment or {},
        "output": upgraded["output"],
        "uninstall": uninstall,
    }


def discard_canary(target_id: str) -> dict:
    _, target = target_from(target_id)
    output = helm_uninstall(CANARY_RELEASE, target["namespace"])
    return {"ok": True, "namespace": target["namespace"], "release": CANARY_RELEASE, "output": output}


def _pods(namespace: str, release: str) -> list[dict]:
    kubectl = require_kubectl()
    namespace = allow_namespace(namespace)
    release = allow_release(release)
    code, output = run(
        [
            kubectl,
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/instance=" + release,
            "-o",
            "json",
        ],
        20,
    )
    if code != 0:
        return []
    payload = _loads(output, "kubectl")
    pods = []
    if isinstance(payload, dict):
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            statuses = item.get("status", {}).get("containerStatuses") or []
            ready_count = sum(1 for container in statuses if isinstance(container, dict) and container.get("ready"))
            restarts = sum(int(container.get("restartCount") or 0) for container in statuses if isinstance(container, dict))
            pods.append(
                {
                    "name": item.get("metadata", {}).get("name", ""),
                    "phase": item.get("status", {}).get("phase", ""),
                    "ready": f"{ready_count}/{len(statuses)}" if statuses else "0/0",
                    "restarts": restarts,
                }
            )
    return pods


def ephemeral_namespaces() -> list[str]:
    kubectl = require_kubectl()
    code, output = run([kubectl, "get", "namespaces", "-o", "json"], 20)
    if code != 0:
        raise LabError(output.strip() or "kubectl get namespaces failed", 502)
    payload = _loads(output, "kubectl")
    names: list[str] = []
    if isinstance(payload, dict):
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            name = item.get("metadata", {}).get("name")
            if ephemeral_namespace_ok(name):
                names.append(name)
    names.sort()
    return names


def list_ephemeral() -> dict:
    return {"ok": True, "namespaces": ephemeral_namespaces(), "limit": MAX_EPHEMERAL}


def ephemeral_target(namespace: str) -> dict:
    namespace = require_ephemeral_namespace(namespace)
    return {
        "namespace": namespace,
        "values_file": "values-dev.yaml",
        "host": allow_host(f"hello-{namespace}.localtest.me"),
        "environment": "staging",
        "label": "Ephemeral",
    }


def create_ephemeral(experiment: dict | None) -> dict:
    existing = ephemeral_namespaces()
    ensure_ephemeral_capacity(len(existing))
    namespace = ""
    for _ in range(5):
        candidate = "eph-" + secrets.token_hex(4)
        if candidate not in existing and ephemeral_namespace_ok(candidate):
            namespace = candidate
            break
    if not namespace:
        raise LabError("could not allocate an ephemeral namespace", 500)
    target = ephemeral_target(namespace)
    overlay = render_override("ephemeral", target, experiment)
    values_args, overlay_path = helm_values_args(
        "ephemeral",
        target,
        experiment,
        overlay=overlay,
        prefix=namespace,
    )
    try:
        result = helm_upgrade_install(RELEASE, namespace, values_args)
    finally:
        if overlay_path:
            Path(overlay_path).unlink(missing_ok=True)
    result.update({"host": target["host"], "limit": MAX_EPHEMERAL})
    return result


def delete_ephemeral(namespace: object) -> dict:
    name = require_ephemeral_namespace(namespace)
    kubectl = require_kubectl()
    args = [kubectl, "delete", "namespace", name, "--wait=true", "--timeout=180s"]
    code, output = run(args, 200)
    if code != 0:
        if "not found" in output.lower():
            raise LabError("ephemeral namespace is not installed", 404)
        raise LabError(output.strip() or "namespace delete failed", 502)
    return {"ok": True, "namespace": name, "output": output}


def health() -> dict:
    """Tool presence for the console. An unreachable cluster is still a successful response."""
    helm = tool("helm")
    kubectl = tool("kubectl")
    cluster = False
    if kubectl:
        try:
            code, _output = run(
                [kubectl, "--request-timeout=2s", "get", "--raw=/readyz"],
                4,
            )
            cluster = code == 0
        except LabError:
            cluster = False
    return {"ok": True, "helm": bool(helm), "kubectl": bool(kubectl), "cluster": cluster}


def _required_target(body: dict) -> str:
    if "target" not in body:
        raise LabError("target must be dev, qa, or prod")
    target_id, _target = target_from(body["target"])
    return target_id


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
            if path == "/api/canary":
                self._send(200, canary_status(self._query_target()))
                return
            if path == "/api/ephemeral":
                self._send(200, list_ephemeral())
                return
            self._send(404, {"ok": False, "error": "not found"})
        except LabError as exc:
            self._send(exc.status, {"ok": False, "error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if self.headers.get("X-Requested-With") != REQUEST_HEADER:
                raise LabError("missing QALab request header", 403)
            if path != "/api/ephemeral":
                self._send(404, {"ok": False, "error": "not found"})
                return
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            namespace = (query.get("namespace") or [""])[0]
            self._send(200, self._with_lock(lambda: delete_ephemeral(namespace)))
        except LabError as exc:
            self._send(exc.status, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._guard_post()
            if path == "/api/canary":
                reject_unknown(body, {"target", "experiment", "weight"})
                target_id = _required_target(body)
                if "weight" not in body:
                    raise LabError("weight must be an integer from 0 to 100")
                experiment = parse_experiment(body.get("experiment"))
                weight = parse_weight(body["weight"])
                self._send(200, self._with_lock(lambda: deploy_canary(target_id, experiment, weight)))
                return
            if path == "/api/canary/promote":
                reject_unknown(body, {"target"})
                target_id = _required_target(body)
                self._send(200, self._with_lock(lambda: promote_canary(target_id)))
                return
            if path == "/api/canary/discard":
                reject_unknown(body, {"target"})
                target_id = _required_target(body)
                self._send(200, self._with_lock(lambda: discard_canary(target_id)))
                return
            if path == "/api/ephemeral":
                reject_unknown(body, {"experiment"})
                experiment = parse_experiment(body.get("experiment"))
                self._send(200, self._with_lock(lambda: create_ephemeral(experiment)))
                return
            if path == "/api/ephemeral/delete":
                reject_unknown(body, {"namespace"})
                self._send(200, self._with_lock(lambda: delete_ephemeral(body.get("namespace"))))
                return
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
