# Is `compose-e2e` stable? — 2026-08-16

## 1. Why this was asked

On `fix/lease-ownership`, the **same commit** produced a failing and a passing
`compose-e2e` within minutes of each other (runs `31925011245` and
`31925013343`). A job that answers differently on the same tree is not
evidence about the code, so "CI is green" could not be used until the
question was settled.

The failing run's Scheduler was repeating:

```
scheduler-1 | sandbox cleanup failed
scheduler-1 | ValueError: 'DockerUnavailable' is not a valid RefusalReason
```

That is fixed in #14: the socket adapter now keeps the transport error when
the Controller reports something that is not a named refusal, instead of
raising the enum lookup's own `ValueError`. The cause was pre-existing (3B),
not introduced by the phase-4 merge.

## 2. What was run

`workflow_dispatch` was added (#15) so one tree can be run repeatedly without
inventing commits. Eight `compose-e2e` executions on the fixed tree:

| Run | Trigger | `compose-e2e` |
|---|---|---|
| `31927078302` | `fix/unknown-refusal` push | pass, 7m02s |
| `31927080775` | `fix/unknown-refusal` PR | pass, 7m09s |
| `31927959590` | `ci/workflow-dispatch` push | pass |
| `31928183833` | `ci/workflow-dispatch` push | pass, 7m00s |
| `31928194803` | `ci/workflow-dispatch` PR | pass, 6m56s |
| `31929036447` | `main` dispatch | pass, 7m50s |
| `31929040983` | `main` dispatch | pass, 7m17s |
| `31929045048` | `main` dispatch | pass, 7m11s |
| `31929049362` | `main` dispatch | pass, 6m50s |

The last four ran **concurrently** on `main`, which is the load shape most
likely to starve a Docker daemon — the condition the original failure needed.

Every other job passed in all of those runs: `backend-unit`,
`backend-integration` (18–20m), `web`.

## 3. Result

**Nine consecutive passes, zero failures, on the tree that carries the fix.**
Duration is tight: 6m50s to 7m50s. The one failure this record exists for
took **10m07s** — it was slower before it failed, consistent with a daemon
under pressure rather than a race in the test walk.

`compose-e2e` is stable enough that a green run now means something. "CI is
green" no longer needs the discount this record was opened to measure.

## 3.1 A tenth run went red, for a different reason — 2026-08-16, later

The claim in §3 survived four hours. On `feat/failure-reason`, `compose-e2e`
failed in the **Restart drill** step: a Run stayed `queued` for 120 s. The
container logs are not the §2 failure at all:

```
worker-1 | wake-up wait failed; falling back to polling
worker-1 | redis.exceptions.ConnectionError: Error 111 connecting to redis:6379
worker-1 | socket.gaierror: [Errno -3] Temporary failure in name resolution
```

Name resolution inside the Compose network failed while the drill was
restarting containers. The platform did what it is designed to do — fell back
to polling — and the drill's 120 s window still ran out.

Three facts place it outside the code:

- The **same commit** passed `compose-e2e` on its push-triggered run and
  failed on its pull-request run, minutes apart.
- `scripts/restart_drill.py` on the 16 GB host, on that same branch, passed:
  all four scenarios, 123.6 s.
- The four concurrent runs of `main` in §2 had passed the same step.

So `compose-e2e` has **two** distinct flake sources, not one. #14 removed the
`DockerUnavailable` crash and nothing has shown it since; this is the runner's
own networking during container restarts, and nothing in this repository
fixes it. The honest reading of §3 is now: a green run means something, a red
run needs its logs read before it means anything, and "nine in a row" was a
measurement of one failure mode rather than of the job.

## 4. What this does not say

- It is not proof that no flake remains. Nine passes bound the failure rate
  loosely, nothing more. A future red run deserves a diagnosis, not a rerun.
- The two `backend-unit` failures on `fix/unknown-refusal`
  (`31925934960`, `31925947301`) were not flakes: the first version of the
  new adapter test named a class that does not exist, and pyright refused it.
  Fixed in the same PR.
- Nothing here is about `0.1 Technical Preview`, the reference host, or the
  fresh-install walk, which is still not done.
