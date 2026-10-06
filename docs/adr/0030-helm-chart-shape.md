# 0030 The shape of the Helm chart

## Context

Spec section 17 asks for one chart with two Deployments, the api's rollout settings, the worker's
heartbeat liveness, an ingress path allowlist and secrets that are referenced. It leaves open how
migrations run, how configuration is passed, and what `DASHBOARD_PUBLIC` does. No cluster was
available, so the chart could be linted and rendered but not installed.

## Decision

- **Secrets come from one existing Secret**, named by `existingSecret`, loaded with `envFrom`.
  The chart defines no Secret and has no value that holds a credential. Rendering fails if no
  Secret is named. Everything that is not a secret is a ConfigMap built from `config`.
- **Migrations run in an init container of the api** (`alembic upgrade head`), not in a Helm
  hook. A pre-install hook runs before the chart's own ConfigMap exists, and ArgoCD treats hooks
  differently from Helm. The command is idempotent, so a second api replica starting is harmless.
- **`/static` is on the ingress allowlist** beside the spec's paths. The dashboard's stylesheet
  and htmx are served from there; without it every page would load unstyled and no button would
  work.
- **`DASHBOARD_PUBLIC` is a chart value, `dashboard.public`**, not an application setting. The
  application serves the same routes either way; what "public" means is which ingress carries
  them. With `false`, `/setup`, `/login`, `/logout`, `/dashboard` and `/static` leave the public
  ingress and an optional second ingress serves them.
- **Paths are `Prefix` matches without a trailing slash**, so `/dashboard` (the Today page) and
  `/dashboard/shopping` both match, as do `/setup` and `/logout`.
- **The worker's liveness probe is a Python one-liner** on the age of the heartbeat file. The
  image has no shell tool that is certain to be there; it certainly has Python.
- **Pods run as `nobody` on a read-only root file system**, with `/tmp` as an `emptyDir`.
- **Postgres, the object store, cert-manager, the tailnet route and backups stay outside** the
  chart: they are the cluster's, as on the existing k3s stack.

## Consequences

- A first install shows the worker logging database errors for the few seconds until the api's
  init container has migrated. Its jobs retry every 5 seconds.
- The api cannot start if migration fails, which is the right failure for a schema mismatch.
- The image name in `values.yaml` is a placeholder: CI does not build or push an image.
- The checks are `helm lint` and a test that renders the chart several ways and asserts on the
  result (`tests/unit/test_helm.py`), skipped where helm is not installed. Nothing has been
  applied to a cluster, so admission, the probes and the ingress class are untested in practice.
