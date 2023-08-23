import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat
)
import httpx
import kopf
import yaml

from easykube import ApiError

from pyhelm3 import Client as HelmClient

from .config import settings
from .template import default_loader
from .utils import mergeconcat


def b64encode(value):
    """
    Wrapper around base64.b64encode that handles the encoding and decoding.
    """
    return base64.b64encode(value.encode()).decode()


async def ensure_zenith_secret(client, cluster, secret_name):
    """
    Ensures that a secret containing an SSH keypair for a Zenith reservation
    exists with the given name and returns it.

    If the secret does not exist, it is created. If the secret already exists, the
    labels and annotations are patched.
    """
    namespace = cluster.metadata.namespace
    cluster_name = cluster.metadata.name
    labels = {
        "app.kubernetes.io/managed-by": "azimuth-capi-operator",
        "capi.stackhpc.com/cluster": cluster_name,
    }
    eksecrets = await client.api("v1").resource("secrets")
    # Even if the secret already exists, we will patch the labels and annotations to match
    try:
        _ = await eksecrets.fetch(secret_name, namespace = namespace)
    except ApiError as exc:
        if exc.status_code != 404:
            raise
        # Generate an SSH keypair
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        public_key_text = public_key.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
        # Reserve a domain with the Zenith registrar, associating the keys in the process
        async with httpx.AsyncClient(base_url = settings.zenith.registrar_admin_url) as zclient:
            response = await zclient.post("/admin/reserve", json = {
                "public_keys": [public_key_text],
            })
            response.raise_for_status()
            response_data = response.json()
        # Put the public and private keys into the named secret
        secret_data = {
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    "azimuth.stackhpc.com/zenith-subdomain": response_data["subdomain"],
                    "azimuth.stackhpc.com/zenith-fqdn": response_data["fqdn"],
                },
            },
            "stringData": {
                "id_ed25519": (
                    private_key
                        .private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
                        .decode()
                ),
                "id_ed25519.pub": public_key_text,
            },
        }
        kopf.adopt(secret_data, cluster.dict(by_alias = True))
        return await eksecrets.create(secret_data, namespace = namespace)
    else:
        return await eksecrets.patch(
            secret_name,
            { "metadata": { "labels": labels } },
            namespace = namespace
        )


async def zenith_apiserver_values(client, cluster):
    """
    Returns the Helm values required to use Zenith for the API server.
    """
    name = cluster.metadata.name
    # First, reserve a subdomain for the API server
    apiserver_secret = await ensure_zenith_secret(client, cluster, f"{name}-zenith-apiserver")
    # Get the static pod definition for the Zenith client for the API server
    client = HelmClient(
        default_timeout = settings.helm_client.default_timeout,
        executable = settings.helm_client.executable,
        history_max_revisions = settings.helm_client.history_max_revisions,
        insecure_skip_tls_verify = settings.helm_client.insecure_skip_tls_verify,
        unpack_directory = settings.helm_client.unpack_directory
    )
    resources = list(
        await client.template_resources(
            await client.get_chart(
                "zenith-apiserver",
                repo = settings.zenith.chart_repository,
                version = settings.zenith.chart_version
            ),
            "zenith-apiserver",
            mergeconcat(
                settings.zenith.apiserver_defaults,
                {
                    "zenithClient": {
                        # Use the SSH private key from the secret
                        # The secret data is already base64-encoded
                        "sshPrivateKeyData": apiserver_secret.data["id_ed25519"],
                        # Specify the SSHD host and port from our configuration
                        "server": {
                            "host": settings.zenith.sshd_host,
                            "port": settings.zenith.sshd_port,
                        },
                    },
                }
            ),
            namespace = "kube-system"
        )
    )
    configmap = next(r for r in resources if r["kind"] == "ConfigMap")
    pod = next(r for r in resources if r["kind"] == "Pod")
    # Return the values required to enable Zenith support
    return {
        # The control plane will use the allocated Zenith FQDN on port 443
        "controlPlaneEndpoint": {
            "host": apiserver_secret.metadata.annotations["azimuth.stackhpc.com/zenith-fqdn"],
            "port": 443,
        },
        # Disable the load balancer and floating IP for the API server as Zenith is providing
        # that functionality
        "apiServer": {
            "enableLoadBalancer": False,
            "associateFloatingIP": False,
        },
        # Inject an additional static pod manifest onto the control plane nodes
        # This static pod will run a Zenith client that connects to the API server on that node
        "controlPlane": {
            "kubeadmConfigSpec": {
                "useExperimentalRetryJoin": True,
                "files": [
                    {
                        "path": "/etc/kubernetes/manifests/zenith-apiserver.yaml",
                        "content": b64encode(yaml.safe_dump(pod)),
                        "encoding": "base64",
                        "owner": "root:root",
                        "permissions": "0600",
                    },
                ] + [
                    {
                        "path": f"/etc/zenith/{name}",
                        "content": b64encode(data),
                        "encoding": "base64",
                        "owner": "root:root",
                        "permissions": "0600",
                    }
                    for name, data in configmap["data"].items()
                ],
            },
        },
    }


def zenith_client_values(enabled, name, namespace, **kwargs):
    """
    Returns the Helm values required to launch a Zenith client for the specified service.
    """
    if enabled:
        full_name = f"{name}-client"
        return {
            "addons": {
                "custom": {
                    full_name: {
                        "kind": "Manifests",
                        "spec": {
                            "namespace": namespace,
                            "manifests": {
                                "zenith-client.yaml": default_loader.loads(
                                    "zenith-client.yaml",
                                    name = full_name,
                                    **kwargs
                                ),
                            },
                        },
                    },
                },
            },
        }
    else:
        return {}


async def zenith_values(client, cluster, addons):
    """
    Returns Helm values to apply Zenith to the specified cluster.
    """
    return mergeconcat(
        await zenith_apiserver_values(client, cluster),
        # Produce the values required to install Zenith clients for the cluster services
        zenith_client_values(
            addons.dashboard,
            "kubernetes-dashboard",
            "kubernetes-dashboard",
            upstream_service_name = "kubernetes-dashboard",
            upstream_port = 443,
            upstream_scheme = "https",
            mitm_proxy_enabled = True,
            mitm_proxy_auth_inject_type = "ServiceAccount",
            mitm_proxy_auth_inject_service_account = {
                "clusterRoleName": "cluster-admin",
            },
            label = "Kubernetes Dashboard",
            icon_url = settings.zenith.kubernetes_dashboard_icon_url
        ),
        zenith_client_values(
            addons.monitoring,
            "kube-prometheus-stack",
            "monitoring-system",
            upstream_service_name = "kube-prometheus-stack-grafana",
            upstream_port = 80,
            mitm_proxy_enabled = True,
            mitm_proxy_auth_inject_type = "Basic",
            mitm_proxy_auth_inject_basic = {
                "secretName": "kube-prometheus-stack-grafana",
                "usernameKey": "admin-user",
                "passwordKey": "admin-password",
            },
            label = "Monitoring",
            icon_url = settings.zenith.monitoring_icon_url
        )
    )


async def zenith_operator_resources(instance, cloud_credentials_secret):
    """
    Returns the resources required to enable the Zenith operator for the given cluster.
    """
    # The project ID for the cluster is automatically injected as a Zenith auth param
    # for all clients created by the operator unless auth is explicitly skipped
    clouds_b64 = cloud_credentials_secret.data["clouds.yaml"]
    clouds = yaml.safe_load(base64.b64decode(clouds_b64).decode())
    project_id = clouds["clouds"]["openstack"]["auth"]["project_id"]
    client = HelmClient(
        default_timeout = settings.helm_client.default_timeout,
        executable = settings.helm_client.executable,
        history_max_revisions = settings.helm_client.history_max_revisions,
        insecure_skip_tls_verify = settings.helm_client.insecure_skip_tls_verify,
        unpack_directory = settings.helm_client.unpack_directory
    )
    return list(
        await client.template_resources(
            await client.get_chart(
                "zenith-operator",
                repo = settings.zenith.chart_repository,
                version = settings.zenith.chart_version
            ),
            instance.metadata.name,
            mergeconcat(
                settings.zenith.operator_defaults,
                {
                    "kubeconfigSecret": {
                        "name": (
                            instance.status.kubeconfig_secret_name or
                            f"{instance.status.helm_release_name}-kubeconfig"
                        ),
                        "key": "value",
                    },
                    "config": {
                        "registrarAdminUrl": settings.zenith.registrar_admin_url,
                        "sshdHost": settings.zenith.sshd_host,
                        "sshdPort": settings.zenith.sshd_port,
                        "defaultAuthParams": {
                            "tenancy-id": project_id,
                        },
                    },
                }
            ),
            namespace = instance.metadata.namespace
        )
    )
