# What happened during testing

[Português](validation.md) · [Procedure](runbook.en.md)

The local run used the existing kind clusters with a new `failover-demo` namespace in each. Applications and databases from previous labs were not replication targets. The test compares their workloads, images and PVC UIDs before and after; it does not need to change or merge their data.

The exercise writes notes through HTTP, checks synchronous replication and causes an API outage. It then closes the route, drains the application, waits for a WAL barrier, stops the previous database and inspects node runtimes before promotion. Another note is written on the recovery cluster. Failback carries that note home and rebuilds the peer replica. Finally, the primary Pod is replaced and the data is checked again.

A planned controller interruption also happens before promotion during failback: an exception stops the procedure after fencing the writer. The test requires a 503 response from the gateway and no old workload containers on any node. `resume` finishes from the persisted phase. This tests a control process failure, not a machine crash.

## A write with an uncertain outcome

The test stops the standby and sends a real POST. The gateway receives no upstream response within ten seconds and returns 502. PostgreSQL may already have committed locally and be waiting for replication. The test never retries that POST: it restarts the standby and finds the note exactly once on both sides. The outage remains visible in logs and the report; it is not converted into HTTP success.

My first attempt used `statement_timeout` to interrupt the commit. The synchronous replication wait did not end as expected, and the external command reached its 30-second limit. The standby was restarted and the write reconciled. I replaced that assumption with the HTTP exercise above, which represents the client's experience without promising rollback.

## A sequence does not have to be gapless

The first handover preserved rows and timestamps, but one check required identical sequence `last_value` before and after promotion. It failed. PostgreSQL may advance a sequence during recovery; a gap does not mean a lost note. The [report of this incorrect assumption](../evidence/sequence-assumption-failure.json) is retained.

The current check requires unchanged existing rows and IDs, no backwards sequence movement and a new write with an ID beyond previous values. It does not reset the sequence to make the numbers look tidy. The [PostgreSQL sequence documentation](https://www.postgresql.org/docs/17/functions-sequence.html) explains why gapless values are not guaranteed.

## Local result on 19 October 2026

**48 checks passed** in **106.98 seconds**. Failover preserved 12 notes and took 28.63 seconds from the handover call; failback preserved 13 and took 31.04 seconds. The final write left 14 notes in both databases. These totals include notes retained from earlier attempts.

Side `a` is active, `b` is its synchronous replica and the application returns HTTP 200. Four old volumes were retained across the attempts and final run. No workload, image or PVC UID from the other labs changed.

## Reading the evidence

[verification.json](../evidence/verification.json) includes each check, row counts at each handover, hashes of complete rows, WAL barriers, nodes inspected for fencing and retained volumes. Actual note values and credentials remain in private files. Timings start at the handover procedure call; they exclude initial cluster creation and are not continuous availability measurements.

GitHub Actions repeats initialization and the same exercise in two fresh clusters. The [successful CI run](https://github.com/tommmdl/failover-lab/actions/runs/39310992536) passed the same **48 checks** in 75.13 seconds of testing. Failover took 20.20 seconds and preserved 4 notes; failback took 18.48 seconds and preserved 5. The final write left 6 notes in each database. The [report downloaded from its artifact](../evidence/ci-verification.json) and [run metadata](../evidence/ci-run.json) are retained alongside local evidence. The first CI run stopped before creating clusters because the kind checksum URL was incorrect; it was corrected to the `.sha256sum` file published with the release. Diagnostic logs do not include Secrets or dumps.

These results demonstrate the controlled procedure in this environment. They do not measure host failure, lost disks, complete cluster isolation, production throughput or automatic elections. There is no production RTO or general promise of zero RPO for failure scenarios that were not tested.
