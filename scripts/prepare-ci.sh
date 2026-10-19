#!/usr/bin/env bash
set -euo pipefail
# This script creates disposable CI clusters only; never run against the local labs.
[[ ${CI:-} == true ]] || { echo 'For disposable GitHub Actions runners only' >&2; exit 1; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$ROOT/.local/bin" "$ROOT/../kubernetes-lab/.local" "$ROOT/../disaster-recovery-lab/.local"
cd "$ROOT/.local/bin"
curl -fsSLo kind https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64
curl -fsSLo kind.sha256 https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64.sha256sum
printf '%s  kind\n' "$(awk '{print $1}' kind.sha256)" | sha256sum --check
curl -fsSLo kubectl https://dl.k8s.io/release/v1.37.1/bin/linux/amd64/kubectl
curl -fsSLo kubectl.sha256 https://dl.k8s.io/release/v1.37.1/bin/linux/amd64/kubectl.sha256
printf '%s  kubectl\n' "$(cat kubectl.sha256)" | sha256sum --check
chmod +x kind kubectl
sudo sysctl -w fs.inotify.max_user_instances=512
NODE='kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5'
./kind create cluster --name devops-notes --image "$NODE" --kubeconfig "$ROOT/../kubernetes-lab/.local/kubeconfig" --wait 120s
./kind create cluster --name devops-recovery --image "$NODE" --kubeconfig "$ROOT/../disaster-recovery-lab/.local/kubeconfig" --wait 120s
