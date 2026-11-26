"""Shared fixtures for the operator tests."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from easykube import ApiError
from easykube.rest.util import PropertyDict

from azimuth_capi_operator import operator


def not_found(path: str) -> ApiError:
    """Return the error the real client raises for a 404."""
    request = httpx.Request("GET", f"https://kubernetes/{path}")
    response = httpx.Response(404, request=request)
    return ApiError(httpx.HTTPStatusError("", request=request, response=response))


class FakeResource:
    """Stand in for an easykube resource, backed by the fake client's state."""

    def __init__(self, client: "FakeClient", api_version: str, name: str) -> None:
        """Store the client and the resource this stands in for."""
        self.client = client
        self.key = (api_version, name)

    async def list(self, **params: Any) -> AsyncIterator[PropertyDict]:
        """Yield the objects of this resource."""
        for obj in self.client.objects.get(self.key, []):
            yield PropertyDict(obj)

    async def fetch(self, name: str, **params: Any) -> PropertyDict:
        """Return the named object, or raise a 404 like the real API."""
        for obj in self.client.objects.get(self.key, []):
            if obj["metadata"]["name"] == name:
                return PropertyDict(obj)
        raise not_found(name)

    async def replace(
        self, name: str, data: dict[str, Any], **params: Any
    ) -> dict[str, Any]:
        """Record the replaced object and return it."""
        self.client.replaced.append((self.key, data))
        return data


class FakeClient:
    """Stand in for the operator's easykube client, recording what it is sent."""

    def __init__(self) -> None:
        """Start with no objects and nothing replaced."""
        self.objects: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.missing_apis: set[str] = set()
        self.replaced: list[tuple[tuple[str, str], dict[str, Any]]] = []

    def api(self, api_version: str) -> "FakeApi":
        """Return the fake API for the given version."""
        return FakeApi(self, api_version)


class FakeApi:
    """Stand in for an easykube API group."""

    def __init__(self, client: FakeClient, api_version: str) -> None:
        """Store the client and API version."""
        self.client = client
        self.api_version = api_version

    async def resource(self, name: str) -> FakeResource:
        """Return the fake resource, or raise a 404 if the API is not installed."""
        if self.api_version in self.client.missing_apis:
            raise not_found(self.api_version)
        return FakeResource(self.client, self.api_version, name)


@pytest.fixture
def fake_ekclient(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Replace the operator's Kubernetes client with a fake."""
    client = FakeClient()
    monkeypatch.setattr(operator, "ekclient", client)
    return client
