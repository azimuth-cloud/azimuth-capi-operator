import asyncio
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
from easykube.resources import Secret

from .config import settings
from .helm import Chart, Release
from .template import default_loader
from .utils import deepmerge


def b64encode(value):
    """
    Wrapper around base64.b64encode that handles the encoding and decoding.
    """
    return base64.b64encode(value.encode()).decode()


async def ensure_zenith_secret(
    client,
    cluster,
    secret_name,
    extra_labels,
    extra_annotations
):
    """
    Ensures that a secret containing an SSH keypair for a Zenith reservation
    exists with the given name and returns it.

    If the secret does not exist, it is created. If the secret already exists, the
    labels and annotations are patched.
    """
    namespace = cluster["metadata"]["namespace"]
    cluster_name = cluster["metadata"]["name"]
    labels = dict(
        {
            "app.kubernetes.io/managed-by": "azimuth-capi-operator",
            "capi.stackhpc.com/cluster": cluster_name,
        },
        **extra_labels
    )
    # Even if the secret already exists, we will patch the labels and annotations to match
    try:
        _ = await Secret(client).fetch(secret_name, namespace = namespace)
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
                "annotations": dict(
                    {
                        "azimuth.stackhpc.com/zenith-subdomain": response_data["subdomain"],
                        "azimuth.stackhpc.com/zenith-fqdn": response_data["fqdn"],
                    },
                    **extra_annotations
                ),
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
        kopf.adopt(secret_data, cluster)
        return await Secret(client).create(secret_data, namespace = namespace)
    else:
        return await Secret(client).patch(
            secret_name,
            {
                "metadata": {
                    "labels": labels,
                    "annotations": extra_annotations,
                },
            },
            namespace = namespace
        )


async def zenith_apiserver_values(client, cluster):
    """
    Returns the Helm values required to use Zenith for the API server.
    """
    name = cluster["metadata"]["name"]
    # First, reserve a subdomain for the API server
    apiserver_secret = await ensure_zenith_secret(
        client,
        cluster,
        f"{name}-zenith-apiserver",
        {},
        {}
    )
    # Get the static pod definition for the Zenith client for the API server
    configmap, pod = await Release("zenith-apiserver", "kube-system").template_resources(
        Chart(
            repository = settings.zenith.chart_repository,
            name = "zenith-apiserver",
            version = settings.zenith.chart_version
        ),
        deepmerge(
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
        )
    )
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


async def zenith_service_values(
    client,
    cluster,
    service_name,
    service_namespace,
    service_enabled,
    service_upstream,
    mitm_auth_inject,
    zenith_auth_params,
    icon_url = None,
    description = None
):
    """
    Returns the Helm values required to launch a Zenith client for the specified service.
    """
    cluster_name = cluster["metadata"]["name"]
    cluster_namespace = cluster["metadata"]["namespace"]
    secret_name = f"{cluster_name}-{service_name}-zenith"
    release_values = settings.zenith.proxy_defaults.copy()
    if service_enabled:
        # Reserve a subdomain for the service and store the associated keypair in a secret
        extra_annotations = {}
        if icon_url:
            extra_annotations["azimuth.stackhpc.com/service-icon-url"] = icon_url
        if description:
            extra_annotations["azimuth.stackhpc.com/service-description"] = description
        zenith_secret = await ensure_zenith_secret(
            client,
            cluster,
            secret_name,
            {
                "azimuth.stackhpc.com/service-name": service_name,
            },
            extra_annotations
        )
        release_values = deepmerge(
            release_values,
            {
                "upstream": service_upstream,
                "zenithClient": {
                    "sshPrivateKeyData": zenith_secret.data["id_ed25519"],
                    "server": {
                        "host": settings.zenith.sshd_host,
                        "port": settings.zenith.sshd_port,
                    },
                    "auth": {
                        "params": zenith_auth_params,
                    },
                },
                "mitmProxy": {
                    "authInject": mitm_auth_inject,
                },
            }
        )
    else:
        await Secret(client).delete(secret_name, namespace = cluster_namespace)
    # Install or uninstall the Zenith client as an extra addon
    return {
        "addons": {
            "extraAddons": {
                f"{service_name}-proxy": {
                    "enabled": service_enabled,
                    "dependsOn": [service_name],
                    "installType": "helm",
                    "helm": {
                        "chart": {
                            "repo": settings.zenith.chart_repository,
                            "name": "zenith-proxy",
                            "version": settings.zenith.chart_version,
                        },
                        "release": {
                            "namespace": service_namespace,
                            "values": release_values,
                        },
                    },
                },
            },
        },
    }


async def zenith_values(client, cluster, cloud_credentials_secret):
    """
    Returns Helm values to apply Zenith to the specified cluster.
    """
    # Extract the project id from the cloud credentials secret
    clouds_b64 = cloud_credentials_secret.data["clouds.yaml"]
    clouds = yaml.safe_load(base64.b64decode(clouds_b64).decode())
    zenith_auth_params = {
        "tenancy-id": clouds["clouds"]["openstack"]["auth"]["project_id"],
    }
    values = await asyncio.gather(
        zenith_apiserver_values(client, cluster),
        zenith_service_values(
            client,
            cluster,
            "kubernetes-dashboard",
            "kubernetes-dashboard",
            cluster["spec"]["addons"]["dashboard"],
            {
                "host": "kubernetes-dashboard",
                "port": 443,
                "scheme": "https",
            },
            {
                "type": "serviceaccount",
            },
            zenith_auth_params,
            settings.zenith.kubernetes_dashboard_icon_url
        ),
        zenith_service_values(
            client,
            cluster,
            "monitoring",
            "monitoring-system",
            cluster["spec"]["addons"]["monitoring"],
            {
                "host": "monitoring-grafana",
                "port": 80,
            },
            {
                "type": "basic",
                "basic": {
                    "secretName": "monitoring-grafana",
                    "usernameKey": "admin-user",
                    "passwordKey": "admin-password",
                },
            },
            zenith_auth_params,
            settings.zenith.monitoring_icon_url
        )
    )
    return deepmerge(*values)
