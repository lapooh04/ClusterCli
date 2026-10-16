# OC-Peek

OC-Peek is a small command-line dashboard for checking pods, nodes, namespaces, events, and container logs in an OpenShift cluster. It uses your Kubernetes kubeconfig, including the context and project selected by `oc login`.

## Requirements

- Python 3.10 or newer
- Access to an OpenShift cluster and a working `oc` login
- Permissions for the resources used by the command you run

## Setup

From the project directory:

```bash
python3 -m venv myenv
source myenv/bin/activate
python -m pip install -r requirements.txt
oc login <cluster-url>
```

Check that the CLI starts:

```bash
python main.py --help
```

## Commands

`status` shows pod phase, container problems, restart counts, IPs, and age. It defaults to the `openshift-console` namespace.

```bash
python main.py status
python main.py status -n my-project
python main.py status --all
```

`doctor` gives a quick first look at unhealthy pods, nodes that are not Ready, and recent Warning events. By default it checks pods and events across namespaces; use `-n` to focus on one namespace.

```bash
python main.py doctor
python main.py doctor -n my-project
```

`events` shows recent Warning events in `openshift-console` by default. Use `--all` to query every namespace, `--limit` to change the number shown, and `--all-types` to include Normal events.

```bash
python main.py events -n my-project
python main.py events --all --limit 50
python main.py events -n my-project --all-types
```

`logs` displays recent logs for a pod. Use `--previous` to inspect the last terminated container, `--container` for a specific container, and `--follow` to stream new output. The default namespace is `openshift-console`.

```bash
python main.py logs my-pod -n my-project
python main.py logs my-pod -n my-project --tail 300 --since 2h
python main.py logs my-pod -n my-project --previous
python main.py logs my-pod -n my-project -c web --follow
```

`nodes` lists node readiness and capacity. `namespace` lists namespaces; add `--short` to show the five busiest namespaces.

```bash
python main.py nodes
python main.py namespace
python main.py namespace --short
```

`clean` first reports failed or evicted pods without changing anything. Deletion only happens when `--confirm` is explicitly supplied.

```bash
python main.py clean -n my-project
python main.py clean -n my-project --confirm
```

## Troubleshooting

- If the CLI cannot load a kubeconfig, run `oc login` and verify the active context with `oc whoami` and `oc project`.
- A `403` response means the logged-in account lacks permission for that API resource. Pod status needs `get` and `list` on pods; `doctor` also needs `list` on events and cluster-scoped nodes; logs need `get` on `pods/log`; `clean --confirm` needs `delete` on pods.
- Use `doctor` to identify the affected resource, `events` for scheduling and image-pull clues, then `logs --previous` for a crashed container. `oc describe pod <pod> -n <namespace>` can provide additional Kubernetes condition detail.
- The health summary is a practical signal, not a complete diagnosis. It does not inspect application-specific health or resource metrics.

Run `python main.py <command> --help` for command-specific options.