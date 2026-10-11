# Opt-in reliability core

This change adds an independent, fail-closed update engine. The existing `compose_guardian.main` entrypoint is unchanged. Selecting the new entrypoint is an explicit deployment decision.

## Usage

```sh
python -m compose_guardian.core_main \
  --manifest /config/projects.json \
  --state-dir /state
```

Without `--once`, the core runs daily at 10:00 in `Asia/Shanghai`. Add `--once` for one round. **Without `--execute`, it may pull images but does not recreate containers, recover pending transactions, or clean backup tags.** Image pulls change local image cache and are not a completely read-only operation.

Manifest example:

```json
{
  "projects": [
    {
      "name": "example",
      "directory": "/srv/example",
      "files": ["/srv/example/compose.yml"],
      "services": ["web"],
      "independent": false
    }
  ]
}
```

The name, directory and ordered file list must match the original deployment. Mount original host paths at the same absolute paths when running the tool in a container. Paths are not remapped implicitly. The state directory must be persistent, private, writable, and shared by all instances that manage the same projects.

Check mode permits inspection with unconfirmed service associations. Execution requires `independent: true`, meaning an operator has confirmed that listed services can be updated independently. Associated multi-service update groups are not yet implemented; do not claim independence merely to bypass this gate.

## Implemented safeguards

- Baseline and recovery use the actual running container image ID, not the previous value of a mutable tag.
- Prechecks reject stopped, unhealthy or mixed-image replicas. No-healthcheck containers use an ID/restart stability window; reports do not claim business health was verified.
- Commands have timeouts, return-code checks and bounded, rule-based redacted diagnostics.
- Updates pin target image IDs through an additional Compose override, with pull and build disabled during recreation.
- Old images receive separate verified backup tags. Recovery uses an override without rewriting the original Compose files or shared image tag, and separately verifies image and health recovery.
- Pending recovery metadata is atomically persisted before container mutation. Exceptions trigger recovery; later execution rounds prioritize outstanding recovery.
- Retention keeps two distinct historical image versions per service, excluding the current version. Container and pending-recovery references are protected and may temporarily exceed this limit. Cleanup removes owned backup tags only, never force-prunes image IDs.
- Reports use unique round IDs, per-service stage evidence, atomic writes and a round summary.
- A shared `flock` serializes rounds. Exit codes: `0` normal, `1` update/recovery/check failure (including a recovered failed update), `2` paused.

## Analysis and approvals

Production changelog analysis and notification/approval adapters are **not connected**. The default analyzer returns unavailable, so candidate updates remain paused, including when `--execute` is specified. `LocalAnalyzer` is a test fixture, not a production risk assessment.

Approval interfaces bind the actor and target image to the current round, accept a decision only once, and invalidate requests on a new round or restart. There is no fixed two-hour expiry. Production asynchronous approval resumption is not implemented. Do not enable a fixture analyzer or auto-approval to bypass missing production integrations.

## Tests

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

The suite contains 25 offline tests covering actual image baselines, success, target mismatches, update/recovery failures, later-round recovery, pull/backup errors, stopped/unhealthy services, identity and association checks, missing integrations, approval actor/target/round binding, retention protection, reports, locking, scheduling, redaction and command errors/timeouts. Docker behavior is mocked.

A separate isolated deployment experiment also exercised real Compose 5.1.4 updates and fixed-image recovery, using a local tag switch instead of registry pulling and an injected verification failure. It is not proof of every application crash, registry failure, replica layout or filesystem behavior. Site-specific scripts and state from that experiment are intentionally not included.

## Remaining limitations

- Real Pi/changelog source and version mapping, approval channels and asynchronous same-round resumption require adapters and additional validation.
- Local build/mixed-build services are unsupported; locally built image references may not be pullable.
- Database/volume/config migrations are not reversed by image rollback.
- Command timeouts cannot guarantee that a Docker daemon has stopped an already accepted operation.
- Secret redaction is best effort; reports and state must remain private. Do not expose arbitrary logs publicly.
- Project identity, file combinations, host paths and business health probes must be verified for each deployment.
- Daily scheduling does not catch up missed rounds after restart. Lock semantics across network filesystems/multiple hosts are unverified.

Deploy in check mode first, validate the manifest and reports, and obtain a separate operational decision before enabling container mutation. Switching back to the legacy entrypoint can re-enable its automatic updates; it is not a harmless rollback of tool code alone.
