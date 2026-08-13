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
configmap_secret, fix_served_file, cni_config, autoscaling (HPA), helm, kustomize,
ingress, ingress_multi (host-based routing, behaviorally verified through the
ingress-nginx controller).

Workload archetypes whose Deployments must become Ready only allow long-running
images (nginx/redis/httpd/memcached); crash-loop troubleshooting is restricted to
HTTP-serving images, so the generated reference solutions can never CrashLoop from
an image that exits immediately (e.g. busybox/alpine/python).

Planned:

- Gateway API, CRDs/operators, CoreDNS archetypes.
- True taint/toleration and kubeadm/node-admin tasks need a multi-node environment
  (roadmap: pluggable `Environment` with a kubeadm-on-VM profile).

## Notes

- During an exam, remember to check which minikube profile is currently active.
- NetworkPolicy challenges require a policy-enforcing CNI; the env defaults to Calico
  (`minikube_cni = "calico"` in config).
- Default addons are `metrics-server` only. When an exam contains an `ingress`/`ingress_multi`
  question the tool auto-enables the minikube `ingress` addon: it labels the node
  (`minikube.k8s.io/primary=true`, `ingress-ready=true`), then waits until the
  ingress-nginx controller, its admission certgen jobs, and the validating webhook are all
  working before serving — so Ingress creation never fails on a not-ready webhook.
- `cka-mock new --topics ingress` focuses an exam on the Ingress category.
- If any question fails preflight (reference not satisfiable, e.g. an unschedulable
  setup), `cka-mock new` first tries to **regenerate just that one challenge in place**
  (`repair_attempts`, default 3): the LLM is asked for a single replacement that still
  fits the rest of the exam (no name/host collisions, family caps honored), the old
  question's cluster artifacts are cleaned up, and it is retried. Only if repair keeps
  failing does the whole exam regenerate (`exam_attempts`).
- At most `max_per_family` questions may come from the same archetype family (default 3;
  the `ingress` family = `ingress` + `ingress_multi`), and all Ingress host names must be
  unique within an exam — otherwise the ingress controller would route one host to the
  wrong backend. Set e.g. `max_per_family = 2` in `cka-mock.toml` for tighter limits.
- `cka-mock new` needs an OpenCode key (`OPENCODE_API_KEY`). Use `scripts/e2e.py` for an
  offline full-loop test (no LLM).
- Set `CKA_MOCK_DEBUG=1` for verbose preflight polling.
