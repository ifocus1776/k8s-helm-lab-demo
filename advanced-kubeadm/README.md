# Advanced Module: Build a Cluster the Hard(er) Way with kubeadm

The main lab uses **k3s**, a single binary that hides most of the moving parts. This optional module builds an upstream **kubeadm** cluster on Debian 12/13 so trainees can see what k3s, and managed services like EKS, AKS and GKE, do for them.

> Use the main k3s lab for the live demo. kubeadm takes longer, touches more of the host (swap, kernel modules, sysctl, apt repositories) and is best run in disposable VMs.

## What kubeadm makes you do (and k3s / managed cloud abstract)

| Concern | kubeadm (this module) | k3s | Managed cloud (EKS/AKS/GKE) |
|---|---|---|---|
| Container runtime | You install and configure containerd 2.x, including the systemd cgroup driver | Bundled containerd | Provider's node image |
| Host prep | Disable swap, load `overlay`/`br_netfilter`, set bridge/forwarding sysctls | Mostly automatic | Provider |
| Package management | Add the pkgs.k8s.io repo per **minor** version, pin and hold kubelet/kubeadm/kubectl | One pinned binary | Provider |
| Control plane | `kubeadm init` generates PKI, static pods for API server, controller-manager, scheduler and **etcd** | Embedded, SQLite or etcd | Fully hidden, with an SLA |
| CNI (pod network) | **You choose and install one** (Calico here); nodes stay `NotReady` until you do | Flannel built in | Provider CNI (VPC-native) |
| Ingress, LB, storage, metrics | Not included; you add them | Traefik, ServiceLB, local-path, metrics-server | Cloud LB, CSI drivers, add-ons |
| Joining nodes | Bootstrap tokens + CA cert hash (`kubeadm token create --print-join-command`) | Node token | Node pools / autoscaling groups |
| Upgrades | `kubeadm upgrade plan/apply`, one minor at a time, unhold/hold packages, drain nodes | Swap binary / system-upgrade-controller | Click or API call |
| Certificates | Expire after 1 year; `kubeadm certs renew` | Auto-rotated | Provider |
| etcd backup / HA | Your job (stacked or external etcd, 3+ control planes) | Your job (embedded etcd) | Provider |

**Teaching point:** your **Helm charts don't change** across all three. That's the payoff of describing apps as code.

## What the playbook does

`site.yml` has three plays:

1. **Prepare all nodes:** bootstrap `python3` (raw), assert Debian 12/13 and cgroup v2, install base packages, turn swap off (runtime, `/etc/fstab`, and mask systemd `.swap` units, including zram), load and persist `overlay` and `br_netfilter` (`community.general.modprobe`), set sysctls (`ansible.posix.sysctl` → `/etc/sysctl.d/99-kubernetes.conf`). Then it installs **containerd.io 2.4.x** from Docker's signed Debian repo (`deb822_repository` with `signed_by`), writes the default config with `SystemdCgroup = true`, adds the **pkgs.k8s.io v1.37** repo (signed-by key, pinned to the minor), installs `kubelet/kubeadm/kubectl 1.37.2-1.1`, and puts them on **hold**.
2. **Initialise the control plane:** `kubeadm init --pod-network-cidr=10.244.0.0/16 …` (idempotent via `creates: /etc/kubernetes/admin.conf`). It copies `admin.conf` to `~/.kube/config` for `k8s_kube_user` with `copy remote_src` (owner + mode 0600, no `cp -i`), installs Calico via the **Tigera operator** (checksum-pinned manifests, server-side apply), renders the Calico `Installation` with the **same 10.244.0.0/16 CIDR**, removes the control-plane `NoSchedule` taint in single-node mode, and waits for the node to be Ready.
3. **Join workers:** only for workers that haven't joined, it creates a **30-minute** token with `kubeadm token create --print-join-command` on the control plane (output hidden with `no_log`) and runs the join. In single-node mode the `workers` group is empty, so this play is a no-op.

`reset.yml` tears it down: `kubeadm reset --force` on workers, then the control plane. Then it removes what kubeadm deliberately leaves behind: `/etc/cni/net.d`, Calico state, iptables and nftables rules, `vxlan.calico`, and the user kubeconfig. Optionally it unholds and purges packages (`-e k8s_reset_purge_packages=true`).

## Pinned versions (verified 2026-11-18 MST)

| Component | Pinned | Source checked |
|---|---|---|
| Kubernetes | `1.37.2` (packages `1.37.2-1.1`) | `https://dl.k8s.io/release/stable.txt` → v1.37.2. The pkgs.k8s.io `v1.37` package index lists 1.37.2-1.1. kubernetes.io/releases lists 1.37 as the newest supported minor. |
| Calico | `v3.33.1` | github.com/projectcalico/calico/releases. It is the latest, released 2026-11-16, and supersedes the v3.33.0 in the reference playbook. docs.tigera.io lists v3.33 as tested with Kubernetes 1.35, 1.36 and 1.37. |
| Tigera operator | `v1.44.1` | Image tag inside Calico v3.33.1 `manifests/tigera-operator.yaml`. It supersedes v1.44.0 in the reference playbook. |
| containerd | `containerd.io 2.4.*` (2.4.2 today) | download.docker.com Debian bookworm/trixie repos |
| Collections | ansible.posix 2.2.2, community.general 14.0.1, kubernetes.core 6.6.0 | Ansible Galaxy |

**Why not Debian's containerd?** Kubernetes 1.36 dropped support for containerd 1.x, and Debian ships 1.6.20 (bookworm) and 1.7.24 (trixie). So containerd comes from Docker's repo. Only `containerd.io` is installed, not Docker Engine.

**Bumping versions:** change `k8s_minor`, `k8s_version` and `k8s_pkg_version`. Upgrade existing clusters with `kubeadm upgrade`, one minor at a time, not by re-running this playbook. For a new Calico version, update `calico_version` and both `sha256` values (`curl -sL <url> | sha256sum`).

## Run it

Requirements: Debian 12 or 13 (amd64 or arm64), systemd, cgroup v2, 2+ vCPU, 4 GB+ RAM (control plane), unique hostnames and MACs, full network connectivity between nodes. Use **disposable VMs**: this module turns swap off, flushes firewall rules on reset, and changes kernel settings.

```bash
cd advanced-kubeadm
../.venv/bin/ansible-galaxy collection install -r requirements.yml -p ./collections

# Single node (control plane runs workloads too). Default inventory, localhost:
../.venv/bin/ansible-playbook site.yml -K

# Control plane + workers over SSH:
cp inventory/multi-node.example.yml inventory/multi-node.yml   # edit IPs / ansible_user
../.venv/bin/ansible-playbook -i inventory/multi-node.yml site.yml -K

kubectl get nodes -o wide
kubectl get pods -A
kubectl get installation default -o jsonpath='{.spec.calicoNetwork.ipPools[0].cidr}'   # 10.244.0.0/16

# Deploy the same chart as the k3s lab. On kubeadm there's no Traefik, so disable the Ingress:
helm upgrade --install hello-lab ../charts/hello-lab -n demo --create-namespace --set ingress.enabled=false --wait

# Tear down
../.venv/bin/ansible-playbook reset.yml -K              # (-i inventory/multi-node.yml for multi-node)
```

Key variables (`group_vars/all.yml`): `k8s_single_node`, `k8s_pod_cidr` (default `10.244.0.0/16`), `k8s_service_cidr`, `k8s_advertise_address` (default: the node's primary IPv4), `k8s_kube_user` (default: `ansible_user` or your login, **not** `ubuntu`. Debian cloud images use `debian`), `calico_encapsulation`, `calico_enable_observability`.

Pod CIDR choice: Calico's sample uses `192.168.0.0/16`, which collides with most home and office LANs (for example nodes on 192.168.1.x). This module uses `10.244.0.0/16` for both kubeadm and the Calico IP pool, and they **must match**. If your LAN or VPN already uses 10.244/16 or 10.96/12, change both CIDRs.

## Firewall ports

If nodes run a host firewall (nftables/ufw/firewalld), open these. Calico recommends disabling firewalld/ufw on nodes in a lab and using Calico host policy instead.

| Node | Port / protocol | Purpose |
|---|---|---|
| Control plane | TCP 6443 | Kubernetes API server (all nodes + admins) |
| Control plane | TCP 2379–2380 | etcd client/peer (control plane only) |
| All nodes | TCP 10250 | kubelet API |
| Control plane | TCP 10257 | kube-controller-manager (localhost by default) |
| Control plane | TCP 10259 | kube-scheduler (localhost by default) |
| All nodes | TCP 10256 | kube-proxy health check |
| All nodes | TCP/UDP 30000–32767 | NodePort Services |
| All nodes | UDP 4789 | Calico VXLAN (this module's `VXLANCrossSubnet`) |
| All nodes | TCP 179 | Calico BGP (only if you switch to BGP/IP-in-IP mode) |
| All nodes | TCP 5473 | Calico Typha (larger clusters) |

The old ports 10251/10252 (insecure scheduler and controller-manager) were removed years ago. Don't open them.

## Web UI: use Headlamp, not Dashboard v2.7.0

Kubernetes Dashboard v2.7.0 dates from 2022 and is outdated. **Headlamp** (a Kubernetes SIG UI project) is the recommended replacement. Chart `0.46.0` / app `0.46.0` was current on 2026-11-18:

```bash
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/
helm upgrade --install headlamp headlamp/headlamp -n headlamp --create-namespace --version 0.46.0
kubectl -n headlamp create token headlamp            # short-lived login token (chart default binds cluster-admin: lab only)
kubectl -n headlamp port-forward svc/headlamp 8080:80 # then open http://localhost:8080
```

Headlamp also ships as a desktop app (Flatpak) that just reads `~/.kube/config`, so no in-cluster install is needed.

## Debian caveats

- **NetworkManager** (Debian desktop) can fight Calico over `cali*` and `vxlan.calico` interfaces. Add `/etc/NetworkManager/conf.d/calico.conf`:
  ```ini
  [keyfile]
  unmanaged-devices=interface-name:cali*;interface-name:tunl*;interface-name:vxlan.calico;interface-name:vxlan-v6.calico;interface-name:wireguard.cali;interface-name:wg-v6.cali
  ```
- **Don't run this on the same desktop as the k3s lab at the same time.** Both want port 6443 and set up competing CNI/iptables state. Run `make lab-down` first, or use a VM.
- **Docker Engine** on the same host: `reset.yml` flushes iptables, which removes Docker's rules. Run `sudo systemctl restart docker` afterwards, or pass `-e k8s_reset_flush_iptables=false`.
- **zram swap** (`systemd-zram-generator` / `zram-tools`) is masked by the swap step. Unmask it after `reset.yml` if you want it back.
- `kubeadm init` downloads control-plane images from `registry.k8s.io`. Pre-pull with `kubeadm config images pull` on slow links.
- Certificates expire after one year: `sudo kubeadm certs check-expiration`.
