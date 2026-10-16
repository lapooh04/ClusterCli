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

WAITING_REASONS = {
    "CrashLoopBackOff",
    "ErrImagePull",
    "ImagePullBackOff",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
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
        self.container_statuses = raw.status.container_statuses or []
        self.restarts = sum(status.restart_count or 0 for status in self.container_statuses)

    @property
    def problem(self):
        if self.phase == "Failed":
            return self.reason or "Failed"

        for status in self.container_statuses:
            state = status.state
            if state is None:
                continue
            waiting = state.waiting
            if waiting and waiting.reason:
                return waiting.reason
            terminated = state.terminated
            if terminated and terminated.exit_code:
                return terminated.reason or f"ExitCode{terminated.exit_code}"

        if self.phase not in ("Running", "Succeeded"):
            return self.reason or self.phase or "Unknown"
        if self.phase == "Running" and any(not status.ready for status in self.container_statuses):
            return "NotReady"
        return None

    def is_healthy(self):
        return self.phase == "Succeeded" or (self.phase == "Running" and self.problem is None)

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

# fetching pods safely
def fetch_pods(v1, namespace=None):
    """Return a list of PodInfo, or None if something went wrong."""
    try:
        if namespace is None:
            raw = v1.list_pod_for_all_namespaces().items
        else:
            raw = v1.list_namespaced_pod(namespace=namespace).items
    except ApiException as exc:
        if exc.status == 404:
            console.print(f"[red]namespace {namespace!r} does not exist[/red]")
        else:
            console.print(f"[red]api error {exc.status}:[/red] {exc.reason}")
        return None
    except Exception as exc:
        console.print(f"[red]could not reach the cluster:[/red] {exc}")
        return None

    return [PodInfo(p) for p in raw]


# viewing pods
def cmd_status(v1, namespace, all_namespaces):
    """Colour-coded table of every pod in a namespace or across the cluster."""
    pods = fetch_pods(v1, None if all_namespaces else namespace)
    if pods is None:
        return
    if not pods:
        console.print("[yellow]no pods found[/yellow]")
        return

    title = "Pods across all namespaces" if all_namespaces else f"Pods in {namespace}"
    table = Table(title=title, title_style="bold cyan")
    if all_namespaces:
        table.add_column("NAMESPACE")
    table.add_column("NAME")
    table.add_column("STATUS")
    table.add_column("RESTARTS", justify="right")
    table.add_column("IP")
    table.add_column("AGE", justify="right")

    for pod in pods:
        status = pod.problem or pod.phase or "Unknown"
        color = PHASE_COLORS.get(status, "green" if pod.is_healthy() else "yellow")
        row = [pod.name, f"[{color}]{status}[/{color}]", str(pod.restarts), pod.ip, pod.age]
        if all_namespaces:
            row.insert(0, pod.namespace)
        table.add_row(*row)

    console.print(table)
    healthy = sum(pod.is_healthy() for pod in pods)
    console.print(f"[dim]{healthy}/{len(pods)} healthy[/dim]")

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

# dead and evicted pods
def cmd_clean(v1, namespace, confirm):
    """Delete failed and evicted pods. Reports only, unless --confirm is passed."""
    pods = fetch_pods(v1, namespace)
    if pods is None:
        return
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
        for namespace, count in sorted(tally.items(), key=lambda item: item[1], reverse=True)[:5]:
            print(f"{namespace}, [{count} pods]")
    else:
        for i, ns in enumerate(namespaces, start=1):
            print(f"{i}.{ns.metadata.name}")


# showing recent cluster events
def fetch_events(v1, namespace=None):
    try:
        if namespace is None:
            return v1.list_event_for_all_namespaces().items
        return v1.list_namespaced_event(namespace=namespace).items
    except ApiException as exc:
        console.print(f"[red]could not list events ({exc.status}):[/red] {exc.reason}")
    except Exception as exc:
        console.print(f"[red]could not list events:[/red] {exc}")
    return None


def event_time(event):
    timestamp = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
    if timestamp and timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def cmd_events(v1, namespace, all_namespaces, limit, all_types):
    """Show recent warning events, or all event types when requested."""
    events = fetch_events(v1, None if all_namespaces else namespace)
    if events is None:
        return
    if not all_types:
        events = [event for event in events if event.type == "Warning"]
    events.sort(key=lambda event: event_time(event) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    events = events[:limit]
    if not events:
        console.print("[green]no matching events found[/green]")
        return

    table = Table(title="Recent events", title_style="bold cyan")
    if all_namespaces:
        table.add_column("NAMESPACE")
    table.add_column("TYPE")
    table.add_column("REASON")
    table.add_column("OBJECT")
    table.add_column("COUNT", justify="right")
    table.add_column("AGE")
    table.add_column("MESSAGE")
    for event in events:
        involved = event.involved_object
        timestamp = event_time(event)
        age = "unknown"
        if timestamp:
            age = f"{int((datetime.now(timezone.utc) - timestamp).total_seconds())}s ago"
        row = [
            event.type or "?",
            event.reason or "?",
            f"{involved.kind or '?'}:{involved.name or '?'}",
            str(event.count or 1),
            age,
            event.message or "",
        ]
        if all_namespaces:
            row.insert(0, event.metadata.namespace or "?")
        table.add_row(*row)
    console.print(table)


# checking pod and node health for a quick first diagnosis
def cmd_doctor(v1, namespace):
    """Summarize unhealthy pods, nodes, and recent warning events."""
    pods = fetch_pods(v1, namespace)
    check_errors = pods is None
    if pods is None:
        console.print("[yellow]pod check could not complete[/yellow]")
        pods = []
    unhealthy = [pod for pod in pods if not pod.is_healthy()]
    console.print(f"[bold cyan]Cluster check[/bold cyan] ({namespace or 'all namespaces'})")
    console.print(f"Pods: {len(pods)} total, {len(unhealthy)} need attention")

    if unhealthy:
        table = Table(title="Pods needing attention", title_style="bold yellow")
        if namespace is None:
            table.add_column("NAMESPACE")
        table.add_column("NAME")
        table.add_column("STATUS")
        table.add_column("RESTARTS", justify="right")
        for pod in unhealthy:
            row = [pod.name, pod.problem or pod.phase or "Unknown", str(pod.restarts)]
            if namespace is None:
                row.insert(0, pod.namespace)
            table.add_row(*row)
        console.print(table)

    not_ready = []
    try:
        nodes = v1.list_node().items
        for node in nodes:
            conditions = node.status.conditions or []
            ready = next((condition for condition in conditions if condition.type == "Ready"), None)
            if ready is None or ready.status != "True":
                not_ready.append((node.metadata.name, ready.reason if ready else "Ready condition missing"))
        console.print(f"Nodes: {len(nodes)} total, {len(not_ready)} not Ready")
        for name, reason in not_ready:
            console.print(f"  [red]{name}[/red]: {reason}")
    except Exception as exc:
        check_errors = True
        console.print(f"[yellow]could not check nodes (cluster permission may be required):[/yellow] {exc}")

    events = fetch_events(v1, namespace)
    if events is None:
        check_errors = True
    else:
        warnings = [event for event in events if event.type == "Warning"]
        warnings.sort(key=lambda event: event_time(event) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        console.print(f"Warning events: {len(warnings)}")
        for event in warnings[:5]:
            involved = event.involved_object
            console.print(f"  {involved.kind}/{involved.name}: {event.reason or 'Warning'} - {event.message or ''}")

    if unhealthy:
        console.print("\n[dim]Next: inspect events and pod logs, then check the owning workload.[/dim]")
    elif not_ready:
        console.print("\n[dim]Next: inspect node conditions and recent events.[/dim]")
    elif check_errors:
        console.print("[yellow]Some checks were unavailable; review the errors above and verify your RBAC permissions.[/yellow]")
    else:
        console.print("[green]No unhealthy pods or not-ready nodes detected.[/green]")


def parse_duration(value):
    """Parse a duration such as 30s, 15m, 2h, or 1d into seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        amount = int(value[:-1])
        seconds = amount * units[value[-1].lower()]
        if amount <= 0:
            raise ValueError
        return seconds
    except (ValueError, KeyError, IndexError):
        raise argparse.ArgumentTypeError("duration must look like 30s, 15m, 2h, or 1d")


# viewing pod logs
def cmd_logs(v1, pod_name, namespace, container, tail, since, follow, previous):
    try:
        response = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            tail_lines=tail,
            since_seconds=since,
            previous=previous,
            follow=follow,
            _preload_content=not follow,
        )
        if follow:
            for chunk in response.stream(amt=1024, decode_content=True):
                print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
        else:
            print(response, end="" if response.endswith("\n") else "\n")
    except ApiException as exc:
        console.print(f"[red]could not read logs ({exc.status}):[/red] {exc.reason}")
        if exc.status == 403:
            console.print("[dim]the current account needs permission to get pods/log[/dim]")
    except Exception as exc:
        console.print(f"[red]could not read logs:[/red] {exc}")
        
        
# also adding the parsers here itself 
# creating these subparsers: status (for pods), nodes (for nodes), clean (to view and remove dead pods), namespace (to view namespace), events (warning events), logs (for a pod), doctor (summarize any unhealthy pods, or warning events)
def main():
    parser = argparse.ArgumentParser(prog="ocmac", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="pod dashboard for a namespace or overall")
    status.add_argument("-n", "--namespace", default="openshift-console")
    status.add_argument("--all", action="store_true", help="view pods for all namespaces")

    subparsers.add_parser("nodes", help="list cluster nodes")

    doctor = subparsers.add_parser("doctor", help="summarize unhealthy pods, nodes, and warning events")
    doctor.add_argument("-n", "--namespace", help="check one namespace (default: all namespaces)")

    events = subparsers.add_parser("events", help="show recent warning events")
    events.add_argument("-n", "--namespace", default="openshift-console")
    events.add_argument("--all", action="store_true", help="include all namespaces")
    events.add_argument("--limit", type=int, default=30, help="maximum events to show")
    events.add_argument("--all-types", action="store_true", help="include Normal events too")

    logs = subparsers.add_parser("logs", help="read or follow a pod's logs")
    logs.add_argument("pod", help="pod name")
    logs.add_argument("-n", "--namespace", default="openshift-console")
    logs.add_argument("-c", "--container", help="container name when the pod has multiple containers")
    logs.add_argument("--tail", type=int, default=100, help="number of recent lines")
    logs.add_argument("--since", type=parse_duration, help="only show logs from e.g. 30m, 2h, or 1d ago")
    logs.add_argument("-f", "--follow", action="store_true", help="continue streaming new log lines")
    logs.add_argument("--previous", action="store_true", help="read the previous terminated container")

    clean = subparsers.add_parser("clean", help="delete failed/evicted pods")
    clean.add_argument("-n", "--namespace", required=True)
    clean.add_argument("--confirm", action="store_true", help="actually delete")

    ns = subparsers.add_parser("namespace", help="view all namespaces")
    ns.add_argument("--short", action="store_true", help="view only top 5 busiest namespaces")

    args = parser.parse_args()

    if getattr(args, "limit", 1) <= 0:
        parser.error("--limit must be greater than zero")
    if getattr(args, "tail", 1) <= 0:
        parser.error("--tail must be greater than zero")

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
    elif args.command == "doctor":
        cmd_doctor(v1, args.namespace)
    elif args.command == "events":
        cmd_events(v1, args.namespace, args.all, args.limit, args.all_types)
    elif args.command == "logs":
        cmd_logs(v1, args.pod, args.namespace, args.container, args.tail, args.since, args.follow, args.previous)


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
