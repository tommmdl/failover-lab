#!/usr/bin/env python3
"""Explicitly fenced, synchronous PostgreSQL handovers in disposable namespaces."""

import argparse, fcntl, json, os, secrets, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
LOCAL.mkdir(exist_ok=True)
NS = "failover-demo"
OWNER = "failover-lab"
IMAGES = json.loads((ROOT / "config/images.json").read_text())
SIDES = {
    "a": {
        "context": "kind-devops-notes",
        "config": ROOT.parent / "kubernetes-lab/.local/kubeconfig",
        "node": "devops-notes-control-plane",
    },
    "b": {
        "context": "kind-devops-recovery",
        "config": ROOT.parent / "disaster-recovery-lab/.local/kubeconfig",
        "node": "devops-recovery-control-plane",
    },
}
K = os.environ.get("KUBECTL", str(ROOT.parent / "kubernetes-lab/.local/bin/kubectl"))


def run(args, data=None, ok=True, timeout=180):
    p = subprocess.run(
        [str(x) for x in args],
        input=data,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if ok and p.returncode:
        raise RuntimeError(f"{args[0]} failed: {p.stderr[-1800:]}")
    return p


def k(side, *args, data=None, ok=True, timeout=180):
    s = SIDES[side]
    return run(
        [K, "--kubeconfig", s["config"], "--context", s["context"], "-n", NS, *args],
        data,
        ok,
        timeout,
    )


def obj(side, kind, name):
    return json.loads(k(side, "get", kind, name, "-o", "json").stdout)


def apply(side, item):
    k(side, "apply", "-f", "-", data=json.dumps(item))


def state():
    return json.loads((LOCAL / "state.json").read_text())


def save(s):
    tmp = LOCAL / "state.new"
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    tmp.replace(LOCAL / "state.json")


def guard(side):
    ns = obj(side, "namespace", NS)
    if ns["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != OWNER:
        raise RuntimeError("Namespace is not owned by this lab")


def sql(side, query, ok=True):
    return k(
        side,
        "exec",
        "-i",
        "deployment/postgres",
        "--",
        "psql",
        "-X",
        "-U",
        "notes_admin",
        "-d",
        "notes",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        data=query,
        ok=ok,
        timeout=30,
    )


def wait_sql(side, query, expected, seconds=100):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        p = sql(side, query, False)
        if p.returncode == 0 and p.stdout.strip() == expected:
            return
        time.sleep(2)
    raise RuntimeError(f"Database readiness timed out on {side}")


def ip(side):
    value = run(
        [
            "docker",
            "inspect",
            "-f",
            '{{(index .NetworkSettings.Networks "kind").IPAddress}}',
            SIDES[side]["node"],
        ]
    ).stdout.strip()
    if not value:
        raise RuntimeError("Missing kind network address")
    return value


def meta(name, labels=None):
    return {
        "name": name,
        "namespace": NS,
        "labels": labels or {"app.kubernetes.io/part-of": OWNER},
    }


def deploy(side, pvc, upstream=None):
    volumes = [
        {"name": "data", "persistentVolumeClaim": {"claimName": pvc}},
        {
            "name": "credentials",
            "secret": {"secretName": "database", "defaultMode": 256},
        },
        {"name": "socket", "emptyDir": {}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
    ]
    mounts = [
        {"name": "data", "mountPath": "/var/lib/postgresql/data"},
        {"name": "credentials", "mountPath": "/run/secrets", "readOnly": True},
        {"name": "socket", "mountPath": "/var/run/postgresql"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    env = [
        {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
        {"name": "POSTGRES_DB", "value": "notes"},
        {"name": "POSTGRES_USER", "value": "lab"},
        {"name": "POSTGRES_PASSWORD_FILE", "value": "/run/secrets/password"},
        {
            "name": "POSTGRES_INITDB_ARGS",
            "value": "--encoding=UTF8 --locale=C.UTF-8 --locale-provider=icu --icu-locale=en-US",
        },
    ]
    container = {
        "name": "postgres",
        "image": IMAGES["postgres"]["image"],
        "args": [
            "postgres",
            "-c",
            "wal_level=replica",
            "-c",
            "max_wal_senders=6",
            "-c",
            "wal_keep_size=256MB",
            "-c",
            "synchronous_commit=remote_apply",
            "-c",
            "listen_addresses=*",
        ],
        "env": env,
        "volumeMounts": mounts,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": "384Mi"},
        },
        "readinessProbe": {
            "exec": {"command": ["pg_isready", "-U", "lab", "-d", "notes"]},
            "periodSeconds": 3,
            "initialDelaySeconds": 3,
        },
    }
    spec = {
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsUser": 70,
            "runAsGroup": 70,
            "runAsNonRoot": True,
            "fsGroup": 70,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": volumes,
        "terminationGracePeriodSeconds": 30,
    }
    if upstream:
        conn = f"host={ip(upstream)} port=30432 user=replicator application_name=peer passfile=/var/lib/postgresql/data/pgpass"
        spec["initContainers"] = [
            {
                "name": "clone",
                "image": IMAGES["postgres"]["image"],
                "command": [
                    "sh",
                    "-ec",
                    'if test -e "$PGDATA/PG_VERSION"; then test -f /var/lib/postgresql/data/clone-complete; exit 0; fi; cp /run/secrets/pgpass /var/lib/postgresql/data/pgpass; chmod 600 /var/lib/postgresql/data/pgpass; pg_basebackup --dbname="$UPSTREAM" --pgdata="$PGDATA" --write-recovery-conf --wal-method=stream --checkpoint=fast; chmod 700 "$PGDATA"; touch /var/lib/postgresql/data/clone-complete',
                ],
                "env": env + [{"name": "UPSTREAM", "value": conn}],
                "volumeMounts": mounts,
                "securityContext": container["securityContext"],
                "resources": container["resources"],
            }
        ]
    apply(
        side,
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta("postgres"),
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": "failover-db"}},
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "failover-db",
                            "app.kubernetes.io/part-of": OWNER,
                        }
                    },
                    "spec": spec,
                },
            },
        },
    )
    k(side, "rollout", "status", "deployment/postgres", "--timeout=150s", timeout=170)


def new_pvc(side):
    name = f"pgdata-{side}-{secrets.token_hex(4)}"
    apply(
        side,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": meta(name),
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "standard",
                "resources": {"requests": {"storage": "1Gi"}},
            },
        },
    )
    return name


def api(side, replicas):
    apply(
        side,
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta("api"),
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": "failover-api"}},
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "failover-api",
                            "app.kubernetes.io/part-of": OWNER,
                        }
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                            "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "api",
                                "image": IMAGES["api"]["image"],
                                "env": [
                                    {"name": "DB_HOST", "value": "postgres"},
                                    {"name": "DB_NAME", "value": "notes"},
                                    {"name": "DB_USER", "value": "lab"},
                                    {
                                        "name": "DB_PASSWORD_FILE",
                                        "value": "/run/secrets/password",
                                    },
                                ],
                                "ports": [{"containerPort": 8000}],
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"cpu": "500m", "memory": "192Mi"},
                                },
                                "securityContext": {
                                    "readOnlyRootFilesystem": True,
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "credentials",
                                        "mountPath": "/run/secrets",
                                        "readOnly": True,
                                    },
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                                "readinessProbe": {
                                    "httpGet": {"path": "/readyz", "port": 8000},
                                    "periodSeconds": 3,
                                },
                            }
                        ],
                        "volumes": [
                            {
                                "name": "credentials",
                                "secret": {
                                    "secretName": "database",
                                    "defaultMode": 256,
                                    "items": [{"key": "password", "path": "password"}],
                                },
                            },
                            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
                        ],
                    },
                },
            },
        },
    )
    if replicas:
        k(side, "rollout", "status", "deployment/api", "--timeout=100s", timeout=120)


def sync(primary, standby):
    wait_sql(standby, "SELECT pg_is_in_recovery();", "t")
    wait_sql(
        primary,
        "SELECT count(*) FROM pg_stat_replication WHERE application_name='peer' AND state='streaming';",
        "1",
    )
    sql(
        primary,
        "ALTER SYSTEM SET synchronous_standby_names='FIRST 1 (peer)';\nSELECT pg_reload_conf();",
    )
    wait_sql(
        primary,
        "SELECT count(*) FROM pg_stat_replication WHERE application_name='peer' AND sync_state='sync';",
        "1",
    )


def init():
    if (LOCAL / "state.json").exists():
        raise RuntimeError(
            "Already initialized; use status/test. Existing volumes are never replaced by init."
        )
    for side in SIDES:
        p = k(side, "get", "namespace", NS, ok=False)
        if p.returncode == 0:
            raise RuntimeError(
                f"Namespace {NS} already exists on {side}; refusing adoption"
            )
        if "NotFound" not in p.stderr:
            raise RuntimeError(p.stderr)
    credentials = {
        "password": secrets.token_urlsafe(32),
        "replication": secrets.token_urlsafe(32),
    }
    (LOCAL / "credentials.json").write_text(json.dumps(credentials))
    os.chmod(LOCAL / "credentials.json", 0o600)
    for side in SIDES:
        apply(
            side,
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": NS,
                    "labels": {"app.kubernetes.io/part-of": OWNER},
                },
            },
        )
        apply(
            side,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": meta("database"),
                "type": "Opaque",
                "stringData": {
                    "password": credentials["password"],
                    "pgpass": f"*:*:*:replicator:{credentials['replication']}\n",
                },
            },
        )
        # Secret files are 0400 and fsGroup grants group access: copy pgpass into the private data directory.
        for name, selector, port, target, nodeport in [
            ("postgres", "failover-db", 5432, 5432, 30432),
            ("api", "failover-api", 8000, 8000, 30433),
        ]:
            apply(
                side,
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": meta(name),
                    "spec": {
                        "type": "NodePort",
                        "selector": {"app": selector},
                        "ports": [
                            {"port": port, "targetPort": target, "nodePort": nodeport}
                        ],
                    },
                },
            )
        api(side, 0)
    a = new_pvc("a")
    deploy("a", a)
    sql(
        "a",
        "CREATE TABLE notes(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,title VARCHAR(200) NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP); ALTER TABLE notes OWNER TO lab; CREATE ROLE replicator WITH LOGIN REPLICATION PASSWORD '"
        + credentials["replication"]
        + "';",
    )
    k(
        "a",
        "exec",
        "deployment/postgres",
        "--",
        "sh",
        "-ec",
        "printf '\nhost replication replicator 0.0.0.0/0 scram-sha-256\n' >> \"$PGDATA/pg_hba.conf\"; psql -U notes_admin -d notes -c 'SELECT pg_reload_conf()'",
    )
    b = new_pvc("b")
    deploy("b", b, "a")
    sync("a", "b")
    api("a", 1)
    save(
        {
            "active": "a",
            "phase": "ready",
            "volumes": {"a": a, "b": b},
            "history": [],
            "route": {"host": ip("a"), "port": 30433},
        }
    )
    print("Ready: a primary, b synchronous standby")


def fence(old):
    guard(old)
    k(old, "scale", "deployment/api", "deployment/postgres", "--replicas=0")
    k(
        old,
        "wait",
        "--for=delete",
        "pod",
        "-l",
        "app.kubernetes.io/part-of=failover-lab",
        "--timeout=100s",
        timeout=120,
    )
    assert_fenced(old)


def assert_fenced(old):
    for name in ["api", "postgres"]:
        if obj(old, "deployment", name)["spec"]["replicas"] != 0:
            raise RuntimeError("Promotion refused: old writer is not fenced")
    if json.loads(
        k(
            old,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/part-of=failover-lab",
            "-o",
            "json",
        ).stdout
    )["items"]:
        raise RuntimeError("Promotion refused: old workload pods still exist")


def promote(old, new):
    assert_fenced(old)
    runtime_fence(old)
    wait_sql(new, "SELECT pg_is_in_recovery();", "t")
    sql(new, "SELECT pg_promote(true,30);")
    # The copied setting points at itself; clear before creating a new replica. API remains stopped.
    sql(new, "ALTER SYSTEM SET synchronous_standby_names=''; SELECT pg_reload_conf();")
    wait_sql(new, "SELECT pg_is_in_recovery();", "f")


def runtime_fence(old):
    cluster = SIDES[old]["context"].removeprefix("kind-")
    nodes = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=io.x-k8s.kind.cluster=" + cluster,
            "--format",
            "{{.Names}}",
        ]
    ).stdout.split()
    expected = [
        n["metadata"]["name"]
        for n in json.loads(k(old, "get", "nodes", "-o", "json").stdout)["items"]
    ]
    if not nodes or set(nodes) != set(expected):
        raise RuntimeError("Cannot verify every kind node; promotion refused")
    for node in nodes:
        containers = json.loads(
            run(
                ["docker", "exec", node, "crictl", "ps", "--output", "json"], timeout=20
            ).stdout
        )["containers"]
        if any(
            c.get("labels", {}).get("io.kubernetes.pod.namespace") == NS
            for c in containers
        ):
            raise RuntimeError(
                "Old workload is still running in the node runtime; promotion refused"
            )
    return nodes


def handover():
    s = state()
    if s["phase"] != "ready":
        raise RuntimeError(
            "Incomplete handover; inspect state and use resume after reviewing the runbook"
        )
    old = s["active"]
    new = "b" if old == "a" else "a"
    for side in SIDES:
        guard(side)
    sync(old, new)
    s["phase"] = "draining"
    s["route"] = None
    s["pending"] = {
        "from": old,
        "to": new,
        "retained_pvc": s["volumes"][old],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save(s)
    resume()


def resume():
    s = state()
    if s["phase"] == "ready" or "pending" not in s:
        raise RuntimeError("No interrupted handover to resume")
    pending = s["pending"]
    old = pending["from"]
    new = pending["to"]
    for side in SIDES:
        guard(side)
    if s["phase"] == "draining":
        k(old, "scale", "deployment/api", "--replicas=0")
        k(
            old,
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app=failover-api",
            "--timeout=100s",
            timeout=120,
        )
        barrier = sql(old, "SELECT pg_current_wal_flush_lsn();").stdout.strip()
        wait_sql(
            new, "SELECT pg_last_wal_replay_lsn() >= '" + barrier + "'::pg_lsn;", "t"
        )
        pending["wal_barrier"] = barrier
        s["phase"] = "fencing"
        save(s)
    if s["phase"] == "fencing":
        fence(old)
        pending["fenced_nodes"] = runtime_fence(old)
        s["phase"] = "promoting"
        save(s)
    if s["phase"] == "promoting":
        assert_fenced(old)
        runtime_fence(old)
        recovery = sql(new, "SELECT pg_is_in_recovery();").stdout.strip()
        if recovery == "t":
            wait_sql(
                new,
                "SELECT pg_last_wal_replay_lsn() >= '"
                + pending["wal_barrier"]
                + "'::pg_lsn;",
                "t",
            )
            promote(old, new)
        elif recovery != "f":
            raise RuntimeError("Unknown target database state")
        # Safe on resume after promotion but before clearing the inherited synchronous setting.
        sql(
            new,
            "ALTER SYSTEM SET synchronous_standby_names=''; SELECT pg_reload_conf();",
        )
        s["phase"] = "rebuilding"
        s["active"] = new
        save(s)
    if s["phase"] == "rebuilding":
        if "new_replica_pvc" not in pending:
            pending["new_replica_pvc"] = new_pvc(old)
            save(s)
        deploy(old, pending["new_replica_pvc"], new)
        sync(new, old)
        s["phase"] = "opening"
        save(s)
    if s["phase"] == "opening":
        wait_sql(old, "SELECT pg_is_in_recovery();", "t")
        sync(new, old)
        api(new, 1)
        s["volumes"][old] = pending["new_replica_pvc"]
        pending["completed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        s["history"].append(pending)
        del s["pending"]
        s["phase"] = "ready"
        s["route"] = {"host": ip(new), "port": 30433}
        save(s)
        print(
            f'Active {new}; synchronous standby {old}; retained {pending["retained_pvc"]}'
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "status", "switch", "resume"])
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(state(), indent=2))
        for side in SIDES:
            print(side, k(side, "get", "pods,pvc").stdout)
        return
    with (LOCAL / "operation.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                "Another lab operation is running; inspect status and wait."
            )
        if args.action == "init":
            init()
        elif args.action == "switch":
            handover()
        elif args.action == "resume":
            resume()


if __name__ == "__main__":
    main()
