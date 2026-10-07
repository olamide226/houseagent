"""The Helm chart, rendered with `helm template` and checked: two Deployments, the rollout
settings, the worker's heartbeat probe, the ingress path allowlist, and no secret embedded.
Skipped where helm is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

CHART = str(Path(__file__).parents[2] / "deploy" / "helm" / "household-agent")
PUBLIC = ["/webhooks", "/presence", "/ics", "/healthz"]
DASHBOARD = ["/setup", "/login", "/logout", "/dashboard", "/static"]


def render(*sets: str) -> dict[tuple[str, str], dict]:
    args = [arg for item in sets for arg in ("--set", item)]
    out = subprocess.run(["helm", "template", "ha", CHART, *args], check=True, capture_output=True, text=True).stdout
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in yaml.safe_load_all(out) if doc}


def paths(ingress: dict) -> list[str]:
    return [p["path"] for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"]]


def test_the_default_render_is_two_deployments_with_referenced_secrets_and_an_allowlisted_ingress():
    docs = render()
    assert sorted(docs) == [("ConfigMap", "ha"), ("Deployment", "ha-api"), ("Deployment", "ha-worker"),
                            ("Ingress", "ha"), ("Service", "ha-api")]
    api, worker = docs["Deployment", "ha-api"]["spec"], docs["Deployment", "ha-worker"]["spec"]
    assert api["replicas"] == 1 and worker["replicas"] == 1
    assert api["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
    api_pod, worker_pod = api["template"]["spec"], worker["template"]["spec"]
    (api_c,), (worker_c,) = api_pod["containers"], worker_pod["containers"]
    assert api_c["command"] == ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    assert worker_c["command"] == ["python", "-m", "app.worker.main"]
    assert api_c["livenessProbe"]["httpGet"]["path"] == "/healthz" and api_c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    probe = worker_c["livenessProbe"]["exec"]["command"]
    assert probe[:2] == ["python", "-c"] and "/tmp/worker-heartbeat" in probe[2] and "> 180" in probe[2], probe
    compile(probe[2], "<probe>", "exec")                                   # the probe is valid Python
    assert "ports" not in worker_c and ("Service", "ha-worker") not in docs  # the worker listens on nothing by default
    assert [c["command"] for c in api_pod["initContainers"]] == [["alembic", "upgrade", "head"]]
    for container in (api_c, worker_c, *api_pod["initContainers"]):
        assert container["image"] == "ghcr.io/olamide226/houseagent:0.1.0"
        assert container["envFrom"] == [{"configMapRef": {"name": "ha"}}, {"secretRef": {"name": "household-agent"}}]
        assert "env" not in container                                       # no value is embedded in a pod spec
        security = container["securityContext"]
        assert security["runAsNonRoot"] and security["readOnlyRootFilesystem"] and not security["allowPrivilegeEscalation"]
        assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]
    config = docs["ConfigMap", "ha"]["data"]
    assert config["WORKER_HEARTBEAT_FILE"] == "/tmp/worker-heartbeat" and config["AGENT_RUNTIME"] == "loop"
    assert "LLM_MODEL" not in config                                        # an empty value is left to the Secret or the default
    secretish = [k for k in config if any(w in k for w in ("KEY", "TOKEN", "SECRET", "PASSWORD", "DATABASE_URL"))]
    assert not secretish, secretish
    assert not any(kind == "Secret" for kind, _ in docs)                    # the chart creates no Secret at all
    ingress = docs["Ingress", "ha"]
    assert paths(ingress) == PUBLIC + DASHBOARD and not any(p.startswith(("/internal", "/readyz")) for p in paths(ingress))
    assert all(p["pathType"] == "Prefix" and p["backend"]["service"] == {"name": "ha-api", "port": {"name": "http"}}
               for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"])
    assert ingress["spec"]["tls"] == [{"hosts": ["home.example.com"], "secretName": "household-agent-tls"}]
    assert ingress["metadata"]["annotations"] == {"cert-manager.io/cluster-issuer": "letsencrypt"}


def test_the_dashboard_can_be_kept_off_the_public_ingress():
    docs = render("dashboard.public=false")
    assert paths(docs["Ingress", "ha"]) == PUBLIC and ("Ingress", "ha-dashboard") not in docs
    docs = render("dashboard.public=false", "dashboard.internalIngress.enabled=true")
    assert paths(docs["Ingress", "ha"]) == PUBLIC and paths(docs["Ingress", "ha-dashboard"]) == DASHBOARD
    assert docs["Ingress", "ha-dashboard"]["spec"]["ingressClassName"] == "tailscale"


def test_the_letta_runtime_gives_the_worker_a_port_inside_the_cluster_only():
    docs = render("config.AGENT_RUNTIME=letta", "config.LETTA_BASE_URL=http://letta:8283")
    config = docs["ConfigMap", "ha"]["data"]
    assert config["INTERNAL_BASE_URL"] == "http://ha-api:8000" and config["WORKER_INTERNAL_URL"] == "http://ha-worker:8001"
    assert docs["Service", "ha-worker"]["spec"]["ports"] == [{"name": "internal", "port": 8001, "targetPort": "internal"}]
    (worker_c,) = docs["Deployment", "ha-worker"]["spec"]["template"]["spec"]["containers"]
    assert worker_c["ports"] == [{"name": "internal", "containerPort": 8001}]
    assert not any(p.startswith("/internal") for p in paths(docs["Ingress", "ha"]))


def test_codex_sign_in_is_one_claim_shared_by_both_processes_and_absent_by_default():
    docs = render()
    for name in ("ha-api", "ha-worker"):
        pod = docs["Deployment", name]["spec"]["template"]["spec"]
        assert pod["volumes"] == [{"name": "tmp", "emptyDir": {}}] and "securityContext" not in pod
    assert "CODEX_HOME" not in docs["ConfigMap", "ha"]["data"]

    docs = render("codexHome.existingClaim=codex-sign-in", "config.LLM_PROVIDER=codex_cli")
    for name in ("ha-api", "ha-worker"):
        pod = docs["Deployment", name]["spec"]["template"]["spec"]
        assert {"name": "codex-home", "persistentVolumeClaim": {"claimName": "codex-sign-in"}} in pod["volumes"]
        assert {"name": "codex-home", "mountPath": "/codex"} in pod["containers"][0]["volumeMounts"]
        assert pod["securityContext"] == {"fsGroup": 65534}                 # nobody can write the sign-in file
        assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
    assert docs["ConfigMap", "ha"]["data"]["CODEX_HOME"] == "/codex"
    assert not any(kind in ("Secret", "PersistentVolumeClaim") for kind, _ in docs)     # the claim is yours, as the Secret is


def test_autoscaling_migration_ingress_and_image_tag_are_values():
    docs = render("api.autoscaling.enabled=true")
    hpa = docs["HorizontalPodAutoscaler", "ha-api"]["spec"]
    assert (hpa["minReplicas"], hpa["maxReplicas"]) == (1, 3) and "replicas" not in docs["Deployment", "ha-api"]["spec"]
    assert hpa["metrics"][0]["resource"]["target"] == {"type": "Utilization", "averageUtilization": 70}
    docs = render("api.migrate=false", "ingress.enabled=false", "image.tag=abc123")
    assert "initContainers" not in docs["Deployment", "ha-api"]["spec"]["template"]["spec"] and ("Ingress", "ha") not in docs
    assert docs["Deployment", "ha-worker"]["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":abc123")


def test_a_render_that_names_no_secret_is_refused():
    refused = subprocess.run(["helm", "template", "ha", CHART, "--set", "existingSecret="], capture_output=True, text=True)
    assert refused.returncode != 0 and "existingSecret names the Secret" in refused.stderr
