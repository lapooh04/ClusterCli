"""OC-Peek -- a little OpenShift health dashboard."""

import argparse
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from rich.console import Console
from rich.table import Table

console = Console()

PHASE_COLORS = {
    "Running": "green",
    "Succeeded": "blue",
    "Pending": "yellow",
    "Failed": "red",
    "Evicted": "red",
    "CrashLoopBackOff": "red",
    "ImagePullBackOff": "red",
}

# the pod class
class PodInfo:
    """Information of the pod that we want to retrieve."""

    def __init__(self, raw):
        self.name = raw.metadata.name
        self.namespace = raw.metadata.namespace
        self.phase = raw.status.phase
        self.ip = raw.status.pod_ip or "-"
        self.started = raw.status.start_time
        self.reason = raw.status.reason       

    def is_healthy(self):
        return self.phase == "Running"

    def is_dead(self):
        """Finished and safe to delete -- failed or evicted."""
        return self.phase == "Failed" or self.reason == "Evicted"

    @property
    def age(self):
        if self.started is None:
            return "unknown"
        seconds = int((datetime.now(timezone.utc) - self.started).total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def __repr__(self):
        return f"<PodInfo {self.name} {self.phase}>"

#the node class
class NodeInfo:
    """creating a class for storing all the important information we need for nodes"""
    def __init__(self, raw):
        self.node_name = raw.metadata.name
        self.node_capacity = raw.status.capacity

    def __repr__(self):
        return f"<NodeInfo {self.node_name} {self.node_capacity}>\n"


# connecting to the cluster
def connect():
    """Return (core_api, apps_api), or (None, None) if we can't log in."""
    try:
        config.load_kube_config()
    except ConfigException as exc:
        console.print(f"[red]no usable kubeconfig:[/red] {exc}")
        console.print("run [yellow]oc login[/yellow] first")
        return None, None
    return client.CoreV1Api(), client.AppsV1Api()

# fetching all pods for a namespace safely
def fetch_pods(v1, namespace):
    """Return a list of PodInfo, or [] if something went wrong."""
    try:
        raw = v1.list_namespaced_pod(namespace=namespace).items
    except ApiException as exc:
        if exc.status == 404:
            console.print(f"[red]namespace {namespace!r} does not exist[/red]")
        else:
            console.print(f"[red]api error {exc.status}:[/red] {exc.reason}")
        return []
    except Exception as exc:
        console.print(f"[red]could not reach the cluster:[/red] {exc}")
        return []

    return [PodInfo(p) for p in raw]


# viewing pods
def cmd_status(v1, namespace, all):
    """Colour-coded table of every pod in a namespace."""
    pods = fetch_pods(v1, namespace)
    if not pods:                     
        return

    if all:
        pods = v1.list_pod_for_all_namespaces().items
        print(f"{len(pods)} pods in the cluster\n")
        healthy = [p for p in pods if p.status.phase == "Running" or p.status.phase == "Succeeded"]
        console.print(f"[dim]{len(healthy)}/{len(pods)} healthy[/dim]")
    else:    
        table = Table(title=f"Pods in {namespace}", title_style="bold cyan")
        table.add_column("NAME")
        table.add_column("STATUS")
        table.add_column("IP")
        table.add_column("AGE", justify="right")
        for pod in pods:
            color = PHASE_COLORS.get(pod.phase, "white")
            table.add_row(pod.name, f"[{color}]{pod.phase}[/{color}]", pod.ip, pod.age)
            console.print(table)
            healthy = [p for p in pods if p.is_healthy()]
            console.print(f"[dim]{len(healthy)}/{len(pods)} healthy[/dim]")

# viewing nodes
def cmd_nodes(v1):
    """List the cluster's nodes and whether they're ready."""
    try:
        nodes = v1.list_node().items       
    except Exception as exc:
        console.print(f"[red]could not list nodes:[/red] {exc}")
        return

    table = Table(title="Nodes", title_style="bold cyan")
    table.add_column("NAME")
    table.add_column("READY")
    table.add_column("CPU", justify="right")
    table.add_column("MEMORY", justify="right")

    for node in nodes:
        ready = "Unknown"
        for condition in node.status.conditions:
            if condition.type == "Ready":
                ready = condition.status        # the string "True" or "False"

        color = "green" if ready == "True" else "red"
        table.add_row(
            node.metadata.name,
            f"[{color}]{ready}[/{color}]",
            node.status.capacity.get("cpu", "?"),
            node.status.capacity.get("memory", "?"),
        )

    console.print(table)

# dead and evicted nodes
def cmd_clean(v1, namespace, confirm):
    """Delete failed and evicted pods. Reports only, unless --confirm is passed."""
    pods = fetch_pods(v1, namespace)
    dead = [p for p in pods if p.is_dead()]

    if not dead:
        console.print(f"[green]nothing to clean in {namespace}[/green]")
        return

    console.print(f"[yellow]{len(dead)} dead pods in {namespace}:[/yellow]")
    for pod in dead:
        console.print(f"  {pod.name}  ({pod.phase})")

    # does nothing if the user doesnt explicitly asks
    if not confirm:
        console.print("\n[dim]dry run. pass --confirm to actually delete.[/dim]")
        return

    for pod in dead:
        try:
            v1.delete_namespaced_pod(name=pod.name, namespace=namespace)
            console.print(f"[red]deleted[/red] {pod.name}")
        except ApiException as exc:
            if exc.status == 404:
                console.print(f"[dim]{pod.name} was already gone[/dim]")
            else:
                console.print(f"[red]could not delete {pod.name}:[/red] {exc.reason}")

# viewing namespace
def cmd_ns(v1, short):
    """View all the available namespaces"""
    namespaces = v1.list_namespace().items
    print(f"found {len(namespaces)} namespaces \n")

    if short:
        #only show the top five busiest namespaces with the highest number of pods
        print("Top 5 busiest namespaces are:\n")
        pods = v1.list_pod_for_all_namespaces().items
        tally = {}
        for pod in pods:
            ns = pod.metadata.namespace
            tally[ns] = tally.get(ns, 0) + 1
        for i in range(5):
            busiest_namespace = max(tally, key=tally.get)
            print(f"{busiest_namespace}, [{tally.get(busiest_namespace, 0)} pods]")
            tally.pop(busiest_namespace)
    else:
        for i, ns in enumerate(namespaces, start=1):
            print(f"{i}.{ns.metadata.name}")
        
        
# also adding the parsers here itself 
# creating three subparsers: status (for pods), nodes (for nodes), clean (to view and remove dead pods), namespaces (to view namespace)
def main():
    parser = argparse.ArgumentParser(prog="ocmac", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="pod dashboard for a namespace or overall")
    status.add_argument("-n", "--namespace", default="openshift-console")
    status.add_argument("--all", action="store_true", help="view pods for all namespaces")

    subparsers.add_parser("nodes", help="list cluster nodes")

    clean = subparsers.add_parser("clean", help="delete failed/evicted pods")
    clean.add_argument("-n", "--namespace", required=True)
    clean.add_argument("--confirm", action="store_true", help="actually delete")

    ns = subparsers.add_parser("namespace", help="view all namespaces")
    ns.add_argument("--short", action="store_true", help="view only top 5 busiest namespaces")

    args = parser.parse_args()

    v1, apps = connect()
    if v1 is None:
        return 1

    if args.command == "status":
        cmd_status(v1, args.namespace, args.all)
    elif args.command == "nodes":
        cmd_nodes(v1)
    elif args.command == "clean":
        cmd_clean(v1, args.namespace, args.confirm)
    elif args.command == "namespace":
        cmd_ns(v1, args.short)


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
