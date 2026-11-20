# Pods in CrashLoopBackOff

A pod in CrashLoopBackOff starts, crashes, and is restarted with an increasing delay.

## Find out why it exits

    kubectl describe pod <pod> -n <namespace>
    kubectl logs <pod> -n <namespace> --previous

`--previous` shows the logs of the container that crashed, not the one starting now.

## Exit code 137: OOMKilled

The container used more memory than its limit and the kernel killed it. Check the memory
graph: a steady climb means a leak, a sudden jump means one large request. Raise the memory
limit as a stopgap and open a ticket for the leak.

## Failing liveness probe

If the logs look healthy but the pod restarts anyway, the liveness probe may be failing:
for example a probe that calls the database, so a slow database restarts every pod.
Liveness should only check that the process is alive; dependencies belong in readiness.

## Bad configuration

A missing environment variable or secret usually fails within a second of starting. Compare
the deployment's env with the last working revision: `kubectl rollout history`.
