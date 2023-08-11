import asyncio
import base64
import contextlib
import httpx
import urllib.parse
import yaml

from easykube import rest


class UnsupportedAuthenticationError(Exception):
    """
    Raised when an unsupported authentication method is used.
    """
    def __init__(self, auth_type):
        super().__init__(f"unsupported authentication type: {auth_type}")


class Auth(httpx.Auth):
    """
    Authenticator class for OpenStack connections.
    """
    def __init__(self, auth_url, application_credential_id, application_credential_secret):
        self.url = auth_url
        self._application_credential_id = application_credential_id
        self._application_credential_secret = application_credential_secret
        self._token = None
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def _refresh_token(self):
        """
        Context manager to ensure only one request at a time triggers a token refresh.
        """
        token = self._token
        async with self._lock:
            # Only yield to the wrapped block if the token has not changed
            # in the time it took to acquire the lock
            if token == self._token:
                yield

    def _build_token_request(self):
        return httpx.Request(
            "POST",
            f"{self.url}/v3/auth/tokens",
            json = {
                "auth": {
                    "identity": {
                        "methods": ["application_credential"],
                        "application_credential": {
                            "id": self._application_credential_id,
                            "secret": self._application_credential_secret,
                        },
                    },
                },
            }
        )
    
    def _handle_token_response(self, response):
        response.raise_for_status()
        self._token = response.headers["X-Subject-Token"]

    async def async_auth_flow(self, request):
        if self._token is None:
            async with self._refresh_token():
                response = yield self._build_token_request()
                self._handle_token_response(response)
        request.headers['X-Auth-Token'] = self._token
        response = yield request


class Resource(rest.Resource):
    """
    Base resource for OpenStack APIs.
    """
    def __init__(self, client, name, prefix = None, singular_name = None):
        super().__init__(client, name, prefix)
        # If no singular name is given, assume the name ends in 's'
        self._singular_name = singular_name or self._name[:-1]

    def _extract_list(self, response):
        return response.json()[self._name]
    
    def _extract_next_page(self, response):
        next_url = next(
            (
                link["href"]
                for link in response.json().get(f"{self._name}_links", [])
                if link["rel"] == "next"
            ),
            None
        )
        # Sometimes, the returned URLs have http where they should have https
        # To mitigate this, we split the URL and return the path and params separately
        url = urllib.parse.urlsplit(next_url)
        params = urllib.parse.parse_qs(url.query)
        return url.path, params

    def _extract_one(self, response):
        content_type = response.headers.get("content-type")
        if content_type == "application/json":
            return response.json()[self._singular_name]
        else:
            return super()._extract_one(response)


class Client(rest.AsyncClient):
    """
    Client for OpenStack APIs.
    """
    def __aenter__(self):
        # Prevent individual clients from being used in a context manager
        raise RuntimeError("clients must be used via a cloud object")
    
    def resource(self, name, prefix = None):
        return Resource(self, name, prefix)


class Cloud:
    """
    Object for interacting with OpenStack clouds.
    """
    def __init__(self, auth, transport, interface):
        self._auth = auth
        self._transport = transport
        self._interface = interface
        self._endpoints = {}
        # A map of api name to client
        self._clients = {}

    async def __aenter__(self):
        await self._transport.__aenter__()
        # Once the transport has been initialised, we can initialise the endpoints
        client = Client(base_url = self._auth.url, auth = self._auth, transport = self._transport)
        response = await client.get("/v3/auth/catalog")
        self._endpoints = {
            entry["type"]: next(
                ep["url"]
                for ep in entry["endpoints"]
                if ep["interface"] == self._interface
            )
            for entry in response.json()["catalog"]
        }
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._transport.__aexit__(exc_type, exc_value, traceback)

    def api_client(self, name):
        """
        Returns a client for the named API.
        """
        if name not in self._clients:
            self._clients[name] = Client(
                base_url = self._endpoints[name],
                auth = self._auth,
                transport = self._transport
            )
        return self._clients[name]

    @classmethod
    def from_clouds(cls, clouds, cloud = "openstack"):
        config = clouds["clouds"][cloud]
        if config["auth_type"] != "v3applicationcredential":
            raise UnsupportedAuthenticationError(config["auth_type"])
        auth = Auth(
            config["auth"]["auth_url"],
            config["auth"]["application_credential_id"],
            config["auth"]["application_credential_secret"]
        )
        transport = httpx.AsyncHTTPTransport(verify = config.get("verify", True))
        return cls(auth, transport, config.get("interface", "public"))
    

async def delete_resources(cluster, cloud_credentials_secret):
    """
    Ensures that any external resources for the cluster are deleted, e.g. those created
    by CCMs and CSI drivers.
    """
    # For now, we just delete loadbalancers created by the OCCM for LoadBalancer services
    # When the FIP is also created by the OCCM, we release that as well
    clouds_b64 = cloud_credentials_secret.data["clouds.yaml"]
    clouds = yaml.safe_load(base64.b64decode(clouds_b64).decode())

    async with Cloud.from_clouds(clouds) as cloud:
        # Release any floating IPs associated with loadbalancer services for the cluster
        networkapi = cloud.api_client("network")
        fips = networkapi.resource("floatingips", "/v2.0/")
        async for fip in fips.list():
            if not fip.description.startswith("Floating IP for Kubernetes external service"):
                continue
            if not fip.description.endswith(f"from cluster {cluster.metadata.name}"):
                continue
            await fips.delete(fip.id)

        # Delete any loadbalancers associated with loadbalancer services for the cluster
        lbapi = cloud.api_client("load-balancer")
        loadbalancers = lbapi.resource("loadbalancers", "/v2/lbaas/")
        async for lb in loadbalancers.list():
            if not lb.name.startswith(f"kube_service_{cluster.metadata.name}_"):
                continue
            if lb.provisioning_status not in {"PENDING_DELETE", "DELETED"}:
                await loadbalancers.delete(lb.id, cascade = "true")
