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
from easykube import resources as k8s

from .template import default_loader


async def ensure_zenith_secret(client, owner, name, zenith_config):
    """
    Ensures that a secret containing an SSH keypair for a Zenith reservation
    exists with the given name and returns it.
    """
    namespace = owner["metadata"]["namespace"]
    # First, see if the secret already exists, as we can save a lot of time!
    try:
        return await k8s.Secret(client).fetch(name, namespace = namespace)
    except ApiError as exc:
        if exc.status_code != 404:
            raise
    # Generate an SSH keypair
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_key_text = public_key.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    # Reserve a domain with the Zenith registrar, associating the keys in the process
    async with httpx.AsyncClient(base_url = zenith_config.registrar_admin_url) as zclient:
        response = await zclient.post("/admin/reserve", json = {
            "public_keys": [public_key_text],
        })
        response.raise_for_status()
        response_data = response.json()
    # Put the public and private keys into the named secret
    secret_data = {
        "metadata": {
            "name": name,
            "namespace": namespace,
            # Annotate the secret with information about the subdomain
            "annotations": {
                "azimuth.stackhpc.com/zenith-subdomain": response_data["subdomain"],
                "azimuth.stackhpc.com/zenith-fqdn": response_data["fqdn"],
            },
        },
        "stringData": {
            "id_rsa": private_key
                .private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
                .decode(),
            "id_rsa.pub": public_key_text,
        }
    }
    kopf.adopt(secret_data, owner)
    return await k8s.Secret(client).create(secret_data)


async def zenith_apiserver_values(client, cluster, zenith_config):
    """
    Returns the Helm values required to use Zenith for the API server.
    """
    name = cluster["metadata"]["name"]
    # First, reserve a subdomain for the API server
    apiserver_secret = await ensure_zenith_secret(
        client,
        cluster,
        f"{name}-zenith-apiserver",
        zenith_config
    )
    # Get the static pod definition for the Zenith client for the API server
    zenith_client_apiserver_pod = default_loader.loads("pod-zenith-client-apiserver.yaml")
    apiserver_client_config = yaml.safe_dump({
        # Use the SSH private key from the secret
        # The secret data is already base64-encoded
        "ssh_private_key_data": apiserver_secret.data["id_rsa"],
        # Specify the SSHD host and port from our configuration
        "server_address": zenith_config.sshd_host,
        "server_port": zenith_config.sshd_port,
        # Proxy to the MITM proxy that is running in the same pod
        "forward_to_host": "127.0.0.1",
        "forward_to_port": 8080,
        # Use a long read timeout so that watches work
        "read_timeout": 31536000,
        # Instruct Zenith to use the API server certificate
        "tls_cert_file": "/etc/kubernetes/pki/apiserver.crt",
        "tls_key_file": "/etc/kubernetes/pki/apiserver.key",
        "tls_client_ca_file": "/etc/kubernetes/pki/ca.crt",
    })
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
                        "path": "/etc/kubernetes/manifests/zenith-client-apiserver.yaml",
                        "content": base64.b64encode(zenith_client_apiserver_pod.encode()).decode(),
                        "encoding": "base64",
                        "owner": "root:root",
                        "permissions": "0600",
                    },
                    {
                        "path": "/etc/zenith/client-apiserver.yaml",
                        "content": base64.b64encode(apiserver_client_config.encode()).decode(),
                        "encoding": "base64",
                        "owner": "root:root",
                        "permissions": "0600",
                    },
                ]
            },
        },
    }


async def zenith_values(client, cluster, zenith_config):
    """
    Returns Helm values to apply Zenith to the specified cluster.
    """
    return zenith_apiserver_values(client, cluster, zenith_config)
