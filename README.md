# Kubernetes + Helm Desktop Lab (k3s on Debian)

[![CI/CD Pipeline](https://github.com/ifocus1776/k8s-helm-lab-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/ifocus1776/k8s-helm-lab-demo/actions/workflows/ci.yml)

A self-contained training kit that shows developers **what Kubernetes is** and **why infrastructure as code (IaC) matters**, by building a single-node cluster with Ansible and deploying an app with a Helm chart.

- `ansible/`: playbook that installs **k3s**, **kubectl** and **Helm** (pinned versions, SHA-256 verified), writes your kubeconfig, waits for the node to be Ready, and can optionally deploy the chart.
- `charts/hello-lab/`: a small, hardened Helm chart (podinfo web app) with dev/prod values for environment promotion.
- `demo/DEMO_SCRIPT.md`: a 15–20 minute live demo runbook with talking points and troubleshooting.
- `advanced-kubeadm/`: optional advanced module that builds a "real" kubeadm cluster (containerd + Calico) to show what k3s and managed cloud services do for you. See its README.
- `Makefile`: one-word commands for everything.

> Generic educational material. No real secrets. The k3s admin kubeconfig is generated on your machine and never leaves it.

## Pinned versions

| Component | Version | Where pinned | Verified against |
|---|---|---|---|
| k3s | `v1.36.6+k3s1` (current k3s *stable* channel, Nov 2026) | `ansible/group_vars/all.yml` | release `sha256sum-<arch>.txt` |
| k3s install.sh | from git tag `v1.36.6+k3s1` | `k3s_install_script_sha256` | pinned SHA-256 |
| kubectl | `v1.36.6` (matches the cluster minor) | `ansible/group_vars/all.yml` | `dl.k8s.io/…/kubectl.sha256` |
| Helm | `v3.22.1` | `ansible/group_vars/all.yml` | `get.helm.sh/…tar.gz.sha256sum` |
| ansible-core | `2.19.15` | `requirements.txt` | n/a |
| kubernetes.core collection | `6.6.0` | `ansible/requirements.yml` | n/a |
| App image | `ghcr.io/stefanprodan/podinfo:6.17.0` | `charts/hello-lab/values.yaml` | n/a |

Helm v3 is used because it is still maintained and is what most teams and CI images run today. Helm v4 (`v4.3.x`) also works with this chart and with `kubernetes.core` 6.x. To try it, set `helm_version: "v4.3.2"`.

## Prerequisites

- Debian 12 (bookworm) or Debian 13 (trixie), amd64 (arm64 also works), with systemd
- A normal user with `sudo`. Run everything as that user, **not** as root or with `sudo make …`.
- 2+ CPU cores, 4 GB+ RAM free, about 5 GB disk, internet access for the first run
- Ports 6443 (API), 80/443 (Traefik ingress) free on the desktop
- `make`, `python3-venv` and `python3-apt` (`make bootstrap` installs them)

## Quick start

```bash
cd k8s-helm-lab-demo
make bootstrap        # apt: python3-venv python3-apt make; .venv with ansible-core 2.19 + kubernetes.core
make lab-up           # k3s + kubectl + helm; asks for your sudo password (-K)
kubectl get nodes     # "lab   Ready   control-plane"
make deploy           # helm upgrade --install hello-lab -> namespace demo
curl -s http://hello-lab.localtest.me/ | jq        # or open it in a browser
make upgrade REPLICAS=3 MESSAGE="Hello v2"
make history
make rollback         # back to the previous revision
make lab-down         # remove everything
```

Deploy the app from Ansible instead of by hand: `make lab-up DEPLOY=true` (uses `kubernetes.core.helm`, then runs `helm test`).

## QA console

`make qa` starts a loopback console at <http://127.0.0.1:8787> for reviewing and deploying this chart.

- **Dev, QA, and prod** are the same Helm release name in different namespaces. QA starts from `values-dev.yaml` and sets its own ingress host, so a QA experiment does not replace the dev or prod release.
- **Review manifests** runs `helm template` and shows the YAML before anything is deployed.
- **Deploy committed** applies the values file in git. **Deploy overlay** adds only the replicas, message, color, and log level you typed. Those fields are checked against the chart schema limits and written to a temporary values file. The image tag stays pinned.
- **Run QA test** runs the chart's `helm test` hook. **Check the app** reads the Service response and shows the message the pods are serving.
- **Release history** rolls the selected namespace back to an earlier Helm revision.
- **Canary** installs a second release, `hello-lab-canary`, in the same namespace as stable `hello-lab`. Weight is an integer from 0 to 100. At 0 the canary has 0 replicas; above 0 its replica count is `max(1, round(stableReplicas × weight / 100))` with half-up rounding. This is a replica canary: the chart has no service-mesh traffic split, and the stable release is left unchanged. Promote copies the canary's message, color, log level, and replica intent onto the stable release, then uninstalls the canary. Discard only uninstalls the canary.
- **Ephemeral** creates a namespace named `eph-` plus 8 lowercase hex characters on this same lab k3s cluster, deploys `hello-lab` there with the QA values file (`values-dev.yaml`, staging environment, host `hello-<namespace>.localtest.me`), and deletes that namespace when you are done. Delete refuses every name that is not `eph-` and 8 hex characters. At most three ephemeral namespaces exist at once.

The console shells out to `helm` and `kubectl` on the machine where it runs. It does not accept a kubeconfig, a chart path, a release name, a namespace, or a raw manifest from the browser.

## CI/CD Pipeline

The repository includes a comprehensive GitHub Actions pipeline (`.github/workflows/ci.yml`) that proves the demo works end-to-end on GitHub-hosted runners:

- **🔍 Lint Job**: Static analysis with yamllint, ansible-lint, helm lint, and kubeconform schema validation
- **🚀 Integration Test**: Full k3s installation on ubuntu-latest, followed by deploy → helm test → upgrade (dev → prod) → rollback → teardown verification
- **⚡ Helm-Kind Test**: Fast validation on a kind cluster for quick feedback

All action versions are pinned for reproducibility. The workflow runs on push to main, pull requests, and can be triggered manually. Perfect for showcasing CI/CD best practices in the demo!

## Make targets

| Target | What it does |
|---|---|
| `make bootstrap` | Installs apt prerequisites, creates `.venv`, installs pinned Ansible and collections |
| `make lab-up [DEPLOY=true]` | Runs `ansible/site.yml` (k3s, kubectl, helm, kubeconfig, readiness) |
| `make lab-check` | Dry run: `--check --diff` |
| `make deploy [ENV=dev\|prod]` | `helm upgrade --install` the chart |
| `make upgrade [REPLICAS=n] [MESSAGE="…"] [ENV=…]` | `helm upgrade` with overrides |
| `make rollback [REV=n]` | `helm rollback` (default: previous revision) |
| `make history` / `make status` / `make test` | Release history, resources, `helm test` smoke test |
| `make dev` / `make prod` | Deploy the dev or prod environment into its own namespace |
| `make lint` | `helm lint`, kubeconform (if installed), yamllint, ansible syntax-check, ansible-lint |
| `make template` | Render the chart for all environments |
| `make lab-down` | `ansible/teardown.yml` → `k3s-uninstall.sh` |

Variables: `RELEASE` (default `hello-lab`), `NS` (default `demo`), `BECOME` (default `-K`; set `BECOME=` for passwordless sudo).

## What the playbook does

`ansible/site.yml` runs four roles on `localhost` (`ansible_connection: local`):

1. **prereqs**: apt packages (`curl`, `ca-certificates`, `jq`, `iptables`, …) and a note if swap is on (k3s tolerates swap).
2. **k8s_tools**: kubectl and Helm into `/usr/local/bin`, checksums verified, skipped when already at the pinned version. It replaces a k3s `kubectl` symlink safely, never writing through it.
3. **k3s**: downloads the pinned k3s binary (checksum verified), renders `/etc/rancher/k3s/config.yaml`, runs the official `install.sh` **from the matching git tag** (also checksum pinned) with `INSTALL_K3S_SKIP_DOWNLOAD=true` so it only creates the systemd unit and `k3s-uninstall.sh`. Then it writes `~/.kube/k3s-lab.yaml` and `~/.kube/config` (mode 0600, context `k3s-lab`; an existing `~/.kube/config` is backed up first) and waits for the node and CoreDNS.
4. **hello_lab** (only when `deploy_sample_app=true`): `kubernetes.core.helm` install/upgrade with `atomic: true`, then `helm test`.

Useful variables (`ansible/group_vars/all.yml`): `k3s_disable_traefik`, `k3s_extra_disable`, `lab_manage_default_kubeconfig`, `hello_lab_values_files` (e.g. `["values-prod.yaml"]`), `teardown_remove_tools`.

Tags: `prereqs`, `tools`, `k3s`, `deploy`. For example, `ansible-playbook site.yml -K --tags tools`.

### Idempotency and check mode
A second `make lab-up` reports `changed=0`. `make lab-check` works on a fresh machine: tasks that need a running cluster are skipped in check mode, and everything else reports what would change.

## The chart (`charts/hello-lab`)

- Deployment, Service, ConfigMap-driven message/color, Ingress (Traefik), ServiceAccount (no token mounted), optional HPA and PodDisruptionBudget, `helm test` hook, `NOTES.txt`, `_helpers.tpl`
- Security: `runAsNonRoot`, fixed UID/GID, `readOnlyRootFilesystem` (with emptyDir `/tmp` and `/data`), `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`
- Liveness/readiness probes, CPU/memory requests and limits, zero-downtime rolling updates
- `values.schema.json` validates input. For example, `image.tag=latest` and non-integer replica counts are rejected.
- Config changes roll pods automatically (`checksum/config` annotation)
- `values-dev.yaml` / `values-prod.yaml` show promotion of one chart version across environments

## Debian notes and caveats

- **Run as your user with `-K`, not with sudo.** The playbook derives the kubeconfig owner from `$USER`/`$HOME`.
- **Debian 12's apt Ansible (ansible-core 2.14) is too old for kubernetes.core 6.x.** Use `make bootstrap` (venv with ansible-core 2.19, works on Python 3.11 and 3.13). On Debian 13, apt's `ansible-core` 2.19 also works: `sudo apt install ansible-core python3-apt` then `make collections`.
- **The apt module needs `python3-apt`** on the system Python (Ansible re-executes with it automatically even when running from the venv). `make bootstrap` installs it.
- **Firewall:** if `ufw` or `firewalld` is enabled, allow the k3s pod and service networks (`10.42.0.0/16`, `10.43.0.0/16`), or pods won't reach DNS or services.
- **Ports 80/443:** Traefik's service load balancer binds them on the host. If Apache, nginx or another dev server owns them, stop it, or set `k3s_disable_traefik: true` and use `kubectl port-forward`.
- **Docker coexistence:** k3s uses its own containerd and runs fine alongside Docker. Docker's iptables rules can occasionally interfere with pod networking. Reboot, or restart k3s, if pods can't reach each other.
- **VPNs** that push 10.42/10.43 routes will collide with k3s defaults. Disconnect during the demo.
- **Teardown** (`k3s-uninstall.sh`) deletes the k3s binary, all cluster data and cached images. A later `make lab-up` downloads them again.
- **Secure Boot / AppArmor:** default Debian AppArmor works with k3s, and no changes are needed.

## Verifying the kit yourself

```bash
make lint                     # all static checks
make lab-check                # dry run
make lab-up DEPLOY=true       # full run incl. helm test
make lab-up                   # second run -> changed=0
```

## Layout

```
k8s-helm-lab-demo/
├── Makefile  README.md  requirements*.txt  .yamllint
├── ansible/
│   ├── ansible.cfg  .ansible-lint  requirements.yml  site.yml  teardown.yml
│   ├── inventory/hosts.yml
│   ├── group_vars/all.yml
│   └── roles/{prereqs,k8s_tools,k3s,hello_lab}/
├── charts/hello-lab/
│   ├── Chart.yaml  values.yaml  values-dev.yaml  values-prod.yaml  values.schema.json
│   └── templates/{deployment,service,configmap,ingress,hpa,pdb,serviceaccount}.yaml, _helpers.tpl, NOTES.txt, tests/
├── demo/DEMO_SCRIPT.md
└── advanced-kubeadm/          # optional advanced module (kubeadm + containerd + Calico)
```
