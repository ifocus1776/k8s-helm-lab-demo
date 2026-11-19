# Live Demo Runbook: Kubernetes + Helm as Infrastructure as Code

**Length:** 15–20 minutes · **Host:** Debian 12/13 desktop · **Audience:** application developers

**Story in one line:** *the cluster and the app are both described in files in git, so anyone can rebuild, change, review and undo them the same way every time.*

---

## 0. Before the audience arrives (T-30 min)

```bash
cd ~/k8s-helm-lab-demo
make bootstrap            # once per machine: .venv with pinned ansible-core + collections
make lab-up               # installs k3s + kubectl + helm (prompts for sudo password)
kubectl get nodes         # node "lab" should be Ready
helm upgrade --install hello-lab charts/hello-lab -n demo --create-namespace --wait \
  && helm uninstall hello-lab -n demo     # pre-pulls the image so the live install is instant
```

Terminal setup: large font, two panes. Pane B runs a live watch for the whole demo:

```bash
watch -n1 kubectl get deploy,pods,svc,ingress -n demo -o wide
```

Optional browser tab: `http://hello-lab.localtest.me/` (podinfo UI shows the message and color).

> If the venue Wi-Fi is unreliable, do all of section 0 at home. Nothing below needs the internet once the image is cached, **except** `*.localtest.me` DNS. Use the `curl -H 'Host: …' http://127.0.0.1/` form shown below if DNS is unavailable.

---

## 1. The repo is the system (2 min)

```bash
tree -L 2 -I 'collections|.venv'
sed -n '1,20p' ansible/group_vars/all.yml          # pinned k3s / kubectl / helm versions
cat charts/hello-lab/values.yaml | head -40
```

**Talking points**
- **Versioned:** every version is pinned in a file. Upgrading k3s = change one line, open a PR.
- **Reviewable:** the whole platform and the app are plain text, so they go through code review like any other change.
- Nobody SSHes in and "just fixes" things. If it isn't in git, it doesn't exist.

## 2. Build the cluster with one command (2 min)

```bash
make lab-up               # already ran before the demo, so run it again on purpose
```

Point at the recap: `changed=0`.

**Talking points**
- **Repeatable / idempotent:** running it again changes nothing. The same playbook on a new laptop gives an identical cluster.
- The playbook verifies SHA-256 checksums of every binary it downloads (supply-chain hygiene).
- `make lab-check` shows a dry run (`--check --diff`) of what would change.

## 3. What is a cluster? (2 min)

```bash
kubectl get nodes -o wide
kubectl get pods -A
kubectl api-resources | head -15
```

**Talking points**
- A node runs containers. The **control plane** (API server, scheduler, controllers, etcd/SQLite) stores the *desired state* and works constantly to make reality match it.
- `kube-system` contains the cluster's own add-ons: CoreDNS, Traefik ingress, metrics-server.
- In the cloud (EKS, AKS, GKE) the provider runs the control plane for you. Your Helm charts work the same there; that's the portability argument.

## 4. Review before you deploy (2 min)

```bash
make lint                                                     # helm lint, kubeconform, yamllint, ansible-lint
helm template hello-lab charts/hello-lab | less               # exact YAML that will be applied
helm template hello-lab charts/hello-lab --set image.tag=latest   # rejected by values.schema.json
```

**Talking points**
- `helm template` renders the exact manifests, so a reviewer sees the real change, and CI can lint it.
- The schema rejects bad input (for example `image.tag=latest`) **before** anything reaches the cluster.
- Security defaults are already in the chart: non-root, read-only root filesystem, all Linux capabilities dropped, resource limits, health probes. Every team inherits them for free.

## 5. Deploy the app (2 min)

```bash
helm install hello-lab charts/hello-lab -n demo --create-namespace --wait
kubectl -n demo get all,ingress
for i in 1 2 3 4; do curl -s http://hello-lab.localtest.me/ | jq -c '{hostname,message}'; done
# no DNS? ->  curl -s -H 'Host: hello-lab.localtest.me' http://127.0.0.1/ | jq -c '{hostname,message}'
```

Alternative path without the ingress (direct to the Service):

```bash
kubectl -n demo port-forward svc/hello-lab 8080:80 &
curl -s localhost:8080/ | jq -c '{hostname,message,version}'
kill %1
```

**Talking points**
- One command created a ServiceAccount, ConfigMap, Deployment, Service and Ingress: a **release**.
- The changing `hostname` shows the Service load-balancing across two replicas.

## 6. Change it the IaC way: `helm upgrade` (3 min)

```bash
helm upgrade hello-lab charts/hello-lab -n demo \
  --set replicaCount=3 --set-string app.message="Hello v2 from a helm upgrade" --wait
for i in 1 2 3; do curl -s http://hello-lab.localtest.me/ | jq -c '{hostname,message}'; done
```

(Same thing via the Makefile: `make upgrade REPLICAS=3 MESSAGE="Hello v2 from a helm upgrade"`.)

**Talking points**
- Watch pane B: a **rolling update** with zero downtime (`maxUnavailable: 0`).
- The message lives in a ConfigMap. A checksum annotation makes config changes roll the pods automatically.
- In real life this change is a one-line edit to a values file in a pull request, and CI runs `helm upgrade`. `--set` is just for the demo.

## 7. History and rollback (2 min)

```bash
helm history hello-lab -n demo
helm rollback hello-lab 1 -n demo --wait
curl -s http://hello-lab.localtest.me/ | jq -c '{hostname,message}'     # back to the original message
helm history hello-lab -n demo                                         # rollback is itself a new revision
```

**Talking points**
- **Rollback:** every release is versioned, so going back is one command, not a midnight rebuild.
- **Auditable:** history records who changed what and when (the rollback appears as revision N+1).

## 8. Self-healing and desired state (2 min)

```bash
kubectl -n demo delete pod -l app.kubernetes.io/name=hello-lab --wait=false   # "kill" the app
kubectl -n demo get pods                                                      # replacements already starting

kubectl -n demo scale deploy/hello-lab --replicas=5    # a manual, out-of-band change ("drift")
helm upgrade hello-lab charts/hello-lab -n demo --wait # re-apply what's in git
kubectl -n demo get deploy hello-lab                   # back to the declared replica count
```

**Talking points**
- Kubernetes continuously reconciles toward the declared state; lost pods are replaced automatically.
- Manual `kubectl` changes are **drift**. Re-applying the chart puts the cluster back to what git says. That's why changes go through the repo, not the terminal.

## 9. One chart, many environments (2 min)

```bash
diff charts/hello-lab/values-dev.yaml charts/hello-lab/values-prod.yaml
make dev            # namespace dev,  1 replica, small limits
make prod           # namespace prod, HPA 3-6 replicas, PodDisruptionBudget
helm list -A
kubectl get deploy,hpa,pdb -n prod
curl -s http://hello-prod.localtest.me/ | jq -c '{hostname,message}'
```

**Talking points**
- **Environment parity:** dev and prod run the *same chart version*; only the values differ. "Works in dev" now means something.
- **Promotion** = deploy the same, already-tested chart version with the next environment's values file.

## 10. Tear it all down (1 min)

```bash
make lab-down       # runs k3s-uninstall.sh via Ansible
kubectl get nodes   # fails: the cluster is gone
```

**Closing line:** "We just destroyed the whole platform. Rebuilding it is `make lab-up`, and it comes back exactly the same. That's the point of infrastructure as code."

---

## IaC benefits map (for the wrap-up slide)

| Benefit | Where we showed it |
|---|---|
| Repeatable | §2 playbook re-run → `changed=0`; §10 rebuild with one command |
| Versioned | §1 pinned versions; §7 `helm history` |
| Reviewable | §4 `helm template` + lint + schema validation |
| Rollback | §7 `helm rollback` |
| Environment parity | §9 same chart, dev/prod values |
| Self-healing / desired state | §8 pod deletion + drift correction |

---

## What if it breaks

| Symptom | Likely cause | Fix |
|---|---|---|
| `make lab-up` fails at "Install base packages" | apt lock held by an update or broken apt source | `sudo apt-get update` and read the error; wait for unattended-upgrades to finish |
| Node stays `NotReady` | k3s still starting, or it crashed | `sudo systemctl status k3s`; `sudo journalctl -u k3s -e --no-pager \| tail -50` |
| `The connection to the server 127.0.0.1:6443 was refused` | k3s not running, or wrong kubeconfig | `sudo systemctl restart k3s`; `export KUBECONFIG=~/.kube/k3s-lab.yaml` |
| `curl http://hello-lab.localtest.me` hangs or refuses | Port 80 already used (Apache/nginx), or DNS blocked | `sudo ss -ltnp \| grep -E ':80 \|:443 '`; use port-forward instead (§5); or `curl -H 'Host: hello-lab.localtest.me' http://127.0.0.1/` |
| `svclb-traefik-*` pod Pending | Host port 80/443 taken | Stop the other web server, or re-run `make lab-up` with `-e k3s_disable_traefik=true` and use port-forward |
| Pods `ImagePullBackOff` | No internet / registry blocked | `kubectl -n demo describe pod <pod>`; pre-pull at home (section 0) |
| Pods `CrashLoopBackOff` | Bad values (e.g. wrong port) | `kubectl -n demo logs deploy/hello-lab`; `helm rollback hello-lab -n demo` |
| `helm upgrade` hangs on `--wait` | New pods never become Ready | Ctrl-C, `kubectl -n demo describe pod`, then `helm rollback hello-lab -n demo` |
| `Error: INSTALLATION FAILED: cannot re-use a name` | Release already exists | `helm upgrade --install …` or `helm uninstall hello-lab -n demo` |
| Pods can't resolve DNS / reach services | Host firewall (ufw/firewalld) blocking pod traffic | `sudo ufw allow from 10.42.0.0/16`; `sudo ufw allow from 10.43.0.0/16` (k3s pod/service CIDRs) |
| Everything is weird | Lab state is out of whack | `make lab-down && make lab-up` (a few minutes; needs internet, because uninstall removes the k3s binary and cached images) |

**Panic button:** `helm rollback hello-lab -n demo` always returns to the last good revision.
