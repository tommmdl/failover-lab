#!/usr/bin/env python3
import fcntl, hashlib, json, sys, time, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab

REPORT = {
    "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "checks": [],
    "handovers": [],
}


def check(name, condition, detail=None):
    REPORT["checks"].append(
        {
            "name": name,
            "passed": bool(condition),
            **({"detail": detail} if detail is not None else {}),
        }
    )
    print(("PASS " if condition else "FAIL ") + name, flush=True)
    if not condition:
        raise AssertionError(name)


def request(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        "http://127.0.0.1:8023" + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return (
                response.status,
                response.read(),
                response.headers.get("X-Lab-Cluster"),
            )
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("X-Lab-Cluster")


def rows(side):
    return json.loads(
        lab.sql(
            side,
            "SELECT coalesce(json_agg(t ORDER BY id),'[]'::json) FROM (SELECT id,title,created_at FROM notes) t;",
        ).stdout
    )


def sequence(side):
    return lab.sql(
        side, "SELECT last_value,is_called FROM notes_id_seq;"
    ).stdout.strip()


def schema(side):
    return lab.sql(
        side,
        "SELECT column_name||':'||data_type||':'||is_nullable||':'||is_identity FROM information_schema.columns WHERE table_schema='public' AND table_name='notes' ORDER BY ordinal_position; SELECT indexdef FROM pg_indexes WHERE tablename='notes' ORDER BY indexname;",
    ).stdout


def outside():
    result = {}
    for side in lab.SIDES:
        resources = json.loads(
            lab.k(side, "get", "deploy,sts,pvc", "-A", "-o", "json").stdout
        )["items"]
        result[side] = []
        for item in resources:
            if item["metadata"]["namespace"] == lab.NS:
                continue
            entry = {
                "kind": item["kind"],
                "namespace": item["metadata"]["namespace"],
                "name": item["metadata"]["name"],
                "uid": item["metadata"]["uid"],
            }
            if item["kind"] in ["Deployment", "StatefulSet"]:
                entry.update(
                    replicas=item["spec"]["replicas"],
                    images=[
                        c["image"]
                        for c in item["spec"]["template"]["spec"]["containers"]
                    ],
                )
            result[side].append(entry)
        result[side].sort(key=lambda x: (x["namespace"], x["kind"], x["name"]))
    return result


def write(title):
    status, body, side = request("/notes", "POST", {"title": title})
    check(
        "API acknowledges write: " + title,
        status == 201,
        {"status": status, "cluster": side},
    )
    return json.loads(body)


def roundtrip(target, label):
    s = lab.state()
    old = s["active"]
    baseline = rows(old)
    seq = sequence(old)
    shape = schema(old)
    oldpvc = s["volumes"][old]
    original_uid = lab.obj(old, "pvc", oldpvc)["metadata"]["uid"]
    check(label + ": initial replica matches all rows", rows(target) == baseline)
    # A real application outage is introduced before the handover.
    lab.k(old, "scale", "deployment/api", "--replicas=0")
    lab.k(
        old,
        "wait",
        "--for=delete",
        "pod",
        "-l",
        "app=failover-api",
        "--timeout=60s",
        timeout=80,
    )
    status, _, _ = request("/readyz")
    check(
        label + ": application outage is visible",
        status in [502, 503],
        {"http": status},
    )
    start = time.monotonic()
    if label == "failback":
        real_promote = lab.promote

        def interrupted(*args):
            raise RuntimeError("Planned controller interruption before promotion")

        lab.promote = interrupted
        try:
            try:
                lab.handover()
            except RuntimeError as e:
                if "Planned controller interruption" not in str(e):
                    raise
            check(
                "Interrupted controller keeps gateway closed",
                lab.state()["phase"] == "promoting" and request("/readyz")[0] == 503,
            )
            lab.assert_fenced(old)
            nodes = lab.runtime_fence(old)
            check(
                "Fencing is confirmed in every old node runtime",
                bool(nodes),
                {"nodes": nodes},
            )
        finally:
            lab.promote = real_promote
        lab.resume()
    else:
        lab.handover()
    elapsed = round(time.monotonic() - start, 2)
    check(label + ": active side changed", lab.state()["active"] == target)
    check(label + ": rows and timestamps survive", rows(target) == baseline)
    check(
        label + ": identity sequence never reuses existing IDs",
        int(sequence(target).split("|")[0]) >= int(seq.split("|")[0])
        and int(sequence(target).split("|")[0])
        >= max((r["id"] for r in baseline), default=0),
        {"before": seq, "after": sequence(target), "gaps_allowed": True},
    )
    check(label + ": schema and indexes survive", schema(target) == shape)
    check(
        label + ": old volume is retained",
        lab.obj(old, "pvc", oldpvc)["metadata"]["uid"] == original_uid,
    )
    check(
        label + ": old side returns as a replica",
        lab.sql(old, "SELECT pg_is_in_recovery();").stdout.strip() == "t",
    )
    check(
        label + ": only one API accepts traffic",
        lab.obj(old, "deployment", "api")["spec"]["replicas"] == 0
        and lab.obj(target, "deployment", "api")["spec"]["replicas"] == 1,
    )
    check(label + ": gateway health recovers", request("/readyz")[0] == 200)
    check(
        label + ": gateway returns every row",
        json.loads(request("/notes")[1])["notes"]
        == [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": datetime.fromisoformat(r["created_at"]).isoformat(),
            }
            for r in reversed(baseline)
        ],
    )
    REPORT["handovers"].append(
        {
            "name": label,
            "from": old,
            "to": target,
            "seconds_from_switch_call": elapsed,
            "rows_preserved": len(baseline),
            "row_sha256": hashlib.sha256(
                json.dumps(baseline, sort_keys=True).encode()
            ).hexdigest(),
            "sequence": seq,
        }
    )


def verify():
    with (lab.LOCAL / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s = lab.state()
        check(
            "Starts with cluster a active", s["active"] == "a" and s["phase"] == "ready"
        )
        initial = outside()
        original_rows = rows("a")
        original_sequence = sequence("a")
        # Private ledger keeps actual values out of evidence and supports reconciliation.
        (lab.LOCAL / "test-baseline.json").write_text(
            json.dumps({"rows": original_rows, "sequence": original_sequence})
        )
        check(
            "Clusters have different identities",
            lab.obj("a", "namespace", "kube-system")["metadata"]["uid"]
            != lab.obj("b", "namespace", "kube-system")["metadata"]["uid"],
        )
        check(
            "Primary uses remote_apply",
            lab.sql("a", "SHOW synchronous_commit;").stdout.strip() == "remote_apply",
        )
        check(
            "Standby is synchronous",
            lab.sql(
                "a",
                "SELECT sync_state FROM pg_stat_replication WHERE application_name='peer';",
            ).stdout.strip()
            == "sync",
        )
        check("Gateway starts healthy", request("/readyz")[0] == 200)
        try:
            lab.promote("a", "b")
        except RuntimeError as e:
            check(
                "Promotion is refused while old writer exists", "not fenced" in str(e)
            )
        else:
            check("Promotion is refused while old writer exists", False)
        check(
            "Refused promotion leaves standby in recovery",
            lab.sql("b", "SELECT pg_is_in_recovery();").stdout.strip() == "t",
        )
        rejected = lab.sql(
            "b",
            "INSERT INTO notes(title) VALUES ('must not be written on standby');",
            False,
        )
        check(
            "Standby rejects direct writes",
            rejected.returncode != 0 and "read-only" in rejected.stderr,
        )
        check(
            "Invalid request is rejected",
            request("/notes", "POST", {"title": ""})[0] == 400,
        )
        prefix = "failover-" + str(int(time.time()))
        for i in range(3):
            write(f"{prefix}-before-{i}")
        baseline = rows("a")
        check("Every acknowledged row is visible on standby", rows("b") == baseline)
        # Hold a commit waiting for its synchronous standby. PostgreSQL can commit locally
        # before returning a cancellation: this is deliberately treated as UNKNOWN.
        lab.k("b", "scale", "deployment/postgres", "--replicas=0")
        lab.k(
            "b",
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app=failover-db",
            "--timeout=60s",
            timeout=80,
        )
        uncertain = prefix + "-unknown-outcome"
        try:
            status, _, _ = request("/notes", "POST", {"title": uncertain})
            check(
                "No HTTP success is reported without the synchronous standby",
                status == 502,
                {"outcome": "unknown; do not retry", "http": status},
            )
        finally:
            lab.k("b", "scale", "deployment/postgres", "--replicas=1")
            lab.k(
                "b",
                "rollout",
                "status",
                "deployment/postgres",
                "--timeout=100s",
                timeout=120,
            )
            lab.sync("a", "b")
        # Restart uses the existing clone: init container must recognize an intact directory.
        lab.wait_sql(
            "b", "SELECT count(*) FROM notes WHERE title='" + uncertain + "';", "1"
        )
        check(
            "Unknown outcome is reconciled once, without retry",
            sum(r["title"] == uncertain for r in rows("a")) == 1
            and rows("a") == rows("b"),
        )
        REPORT["unknown_outcome"] = {
            "request_retried": False,
            "eventually_found_once_on_both_sides": True,
        }
        roundtrip("b", "failover")
        note = write(prefix + "-on-recovery-cluster")
        check(
            "Write after failover has a new identity",
            note["id"] > max(r["id"] for r in baseline),
        )
        check("New write replicates back to original cluster", rows("a") == rows("b"))
        roundtrip("a", "failback")
        write(prefix + "-after-failback")
        check(
            "All writes survive the complete round trip",
            rows("a") == rows("b")
            and all(
                any(r["id"] == before["id"] and r == before for r in rows("a"))
                for before in original_rows
            ),
        )
        check(
            "Failover-side write survives failback",
            any(
                r["id"] == note["id"] and r["title"] == note["title"] for r in rows("a")
            ),
        )
        check("A primary pod restart keeps the same PVC and rows", restart_primary())
        check(
            "Existing labs keep workloads, images and PVC identities",
            outside() == initial,
        )
        REPORT["fencing_history"] = lab.state()["history"][-2:]
        REPORT["images"] = lab.IMAGES
        REPORT["final"] = {
            "active": "a",
            "standby": "b",
            "synchronous": True,
            "rows": len(rows("a")),
            "retained_volumes": len(lab.state()["history"]),
            "http": request("/readyz")[0],
        }
        check("Final service is healthy", REPORT["final"]["http"] == 200)


def restart_primary():
    before = rows("a")
    pvc = lab.state()["volumes"]["a"]
    uid = lab.obj("a", "pvc", pvc)["metadata"]["uid"]
    lab.k(
        "a",
        "delete",
        "pod",
        "-l",
        "app=failover-db",
        "--wait=true",
        "--timeout=70s",
        timeout=90,
    )
    lab.k(
        "a", "rollout", "status", "deployment/postgres", "--timeout=100s", timeout=120
    )
    lab.sync("a", "b")
    return (
        rows("a") == before
        and lab.obj("a", "pvc", pvc)["metadata"]["uid"] == uid
        and rows("b") == before
    )


if __name__ == "__main__":
    start = time.monotonic()
    try:
        verify()
        REPORT["passed"] = True
    except Exception as e:
        REPORT["passed"] = False
        REPORT["error"] = str(e)
        raise
    finally:
        REPORT["duration_seconds"] = round(time.monotonic() - start, 2)
        (lab.ROOT / "evidence/verification.json").write_text(
            json.dumps(REPORT, indent=2) + "\n"
        )
