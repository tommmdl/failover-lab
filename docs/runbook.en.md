# Moving over and coming back

[Português](runbook.md) · [Home](../README.en.md)

Side `a` is `kind-devops-notes`; side `b` is `kind-devops-recovery`. Only namespace `failover-demo` is managed. Previous labs' namespaces are outside the handover. `scripts/lab.py` generates the Kubernetes objects; relevant configuration and image digests live in Git, while passwords remain in `.local` and Secrets.

## Before starting

Check `python3 scripts/lab.py status`, available Docker disk space and access to both clusters. The active database must not be in recovery; its peer must be in recovery and appear as `sync` in `pg_stat_replication`. Do not use `init` to recover an existing setup: it refuses that operation to avoid replacing data or credentials.

Keep private copies of `.local/credentials.json` and `.local/state.json` in appropriate storage. These files do not replace a database backup. Data lives on local PVCs inside kind nodes; deleting a cluster deletes that data too. The local procedure never destroys a cluster.

## Both handovers

1. Open localhost:8023, create a note and check the replica.
2. Run `python3 scripts/lab.py switch`. The same command handles failover and failback.
3. The script closes the route, stops the previous API and waits for its Pods to terminate. It reads a WAL barrier and waits for the standby to replay it.
4. It scales the old database to zero, waits for Pods to disappear and queries the runtime on **every** kind node in that cluster. If a container from this namespace still exists, or a node cannot be inspected, promotion is refused.
5. It promotes the replica while the API stays stopped. It creates a fresh PVC on the original side and runs `pg_basebackup` from the promoted database without mounting or deleting the old volume.
6. It restores synchronous replication, starts the API on the new side and opens the route.
7. Create a note on that side, then repeat the handover to return. Compare data, IDs and timestamps. Sequences can have gaps after promotion/restart; the test requires that they do not move backwards or reuse existing IDs.

The gateway returns 503 while the state records a handover; an unavailable upstream outside that interval produces 502. An in-flight request may lose its response. Check its result before retrying a write. NodePorts and administrative access are lab tools; do not write directly to the database during promotion.

## If the process stops midway

`.local/state.json` records phases and the new volume before attempting to use it. The route stays closed until the phase becomes `ready`. A lock prevents concurrent lab commands. There is no external supervisor to resume automatically.

Inspect Pods, database logs and `.local/state.json`, fix the access, disk space or authentication problem, then run:

```bash
python3 scripts/lab.py resume
```

`resume` continues the recorded phase: draining, verifying fencing, promoting if still necessary, or finishing replica rebuilding. It does not promote an already promoted database again. An incomplete clone is refused: retain the failed PVC for inspection and use a fresh empty PVC, updating the pending reference only after confirming which database is primary. Never delete `PGDATA` or force an old copy into service.

If state is lost or reality differs from the record, stop the gateway and keep both APIs at zero. Identify the primary and timeline through administrative access to both sides, then recover the private state before automating another handover. There is no blind promotion command for an unreachable cluster.

The test also uses a lock. If it fails, read `evidence/verification.json` before retrying: side `b` may be active. Return to `a` through a normal handover or resume the interrupted operation. The test block that stops the standby attempts to restart it even when the request fails.

## Retaining and removing resources

Each handover retains the previous PVC and allocates another 1 GiB volume. This consumes disk space. Compare the PVC list with `volumes` and `history` in state. Never remove either current volume. An old copy must not return as primary after new writes on its peer: it must be rebuilt as a replica.

There is no automatic destructive cleanup on the local machine. To stop the demo while retaining volumes, stop the gateway and scale both APIs to zero. Coordinate any pause of the active database with its standby; pending commits may wait. Before permanently removing the namespace, export and test a backup. Never run CI cleanup commands against the local clusters.

## Network and access limits

The databases use SCRAM and the application keeps a limited role. Replication uses a separate role; the API receives only its own password file. The pgpass copy has mode 0600 on the database volume. Containers run without root or a ServiceAccount token; the local controller uses your kubeconfigs.

This exercise does not encrypt replication with TLS, and its authentication rule accepts the replication role from any reachable address. NodePorts are reachable on the node network. Use this only in the local Docker study environment. Production would need restricted networking, TLS, secret management, external fencing/quorum, backups and exercises across independent failure domains.
