# cka-mock

LLM-generated CKA mock exams on Minikube. The LLM (OpenCode Go via an
OpenAI-compatible gateway) proposes challenges; a deterministic engine renders,
sets up, preflights, and grades them. The LLM never executes anything.

## Development

```sh
nix develop          # python env + cka-mock + kubectl + minikube + helm + kustomize
cd src && pytest     # run tests (also available as `python -m pytest`)
```

The packaged CLI (`packages.default`) is built with poetry-core. At runtime the
tool shells out to `kubectl`, `minikube`, `helm`, and `kustomize`, which must be
on `PATH` — the devShell provides all of them.

## CLI

```
cka-mock new  [--topics RBAC Helm ...] [--questions N] [--duration M] [--difficulty easy|medium|hard]
cka-mock grade
cka-mock status
cka-mock reset
cka-mock replay <exam-id>     # repeat a past exam as a fresh attempt (no LLM):
                              #   resets the cluster, re-applies the broken states,
                              #   serves the same questions again
cka-mock list  [--json]
```

## Configuration

`cka-mock.toml` is discovered from the current directory upward. See
`src/cka_mock/config.py` for fields. Provider settings:

- `OPENCODE_API_KEY` (required) — key from https://opencode.ai/auth
- `OPENCODE_BASE_URL` — default `https://opencode.ai/zen/go/v1` (Go gateway)
- `OPENCODE_REASONING_EFFORT` — `off|low|medium|high|max`

## Status

Implemented and verified end-to-end on a real minikube cluster
(`python scripts/e2e.py`: 13 archetypes, 59/59 checks, 100%):

- **Scaffold**: nix flake + package, OpenCode Go provider, config, CLI, test harness.
- **Generation contract**: JSON-Schema validation, image allowlist, fail-closed retry
  loop, archetype registry.
- **Deterministic core**: renderer (task text, setup, reference, assertions),
  assertion engine, grader, report.
- **Minikube loop**: dedicated `cka-exam` profile (Calico CNI), per-exam workdir +
  kubeconfig, setup apply, preflight (proves each challenge unsolved and solvable,
  restores the broken state), grade/report.
- **File layer (tool-rendered)**: broken manifest/CNI-style configs, Helm chart
  skeletons, Kustomize bases, ConfigMap-served web content — all delivered as files
  in the exam workdir.
- **Fingerprints + journal**: generated exams avoid repeating parameter sets;
  every grade is recorded for progress tracking.

Archetypes currently supported: deployment, service, pvc, networkpolicy (behavioral
probes), rbac, scheduling (nodeSelector + limits), troubleshooting_crashloop,
configmap_secret, fix_served_file, cni_config, autoscaling (HPA), helm, kustomize.

Planned:

- Gateway API, CRDs/operators, CoreDNS archetypes.
- True taint/toleration and kubeadm/node-admin tasks need a multi-node environment
  (roadmap: pluggable `Environment` with a kubeadm-on-VM profile).

## Notes

- NetworkPolicy challenges require a policy-enforcing CNI; the env defaults to Calico
  (`minikube_cni = "calico"` in config).
- Default addons are `metrics-server` only. The `ingress` addon is slow and, unlabeled,
  leaves controller pods Pending — if you opt in via config, the tool auto-labels the
  node `ingress-ready=true` first.
- If any question fails preflight (reference not satisfiable, e.g. an unschedulable
  setup), `cka-mock new` retries up to `exam_attempts` (default 3) times: it regenerates
  a different challenge set and marks the failed questions as rejected in the next prompt.
- `cka-mock new` needs an OpenCode key (`OPENCODE_API_KEY`). Use `scripts/e2e.py` for an
  offline full-loop test (no LLM).
- Set `CKA_MOCK_DEBUG=1` for verbose preflight polling.
