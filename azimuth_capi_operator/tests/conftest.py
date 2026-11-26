"""Shared fixtures for the operator tests."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from azimuth_capi_operator import operator


class FakeResource:
    """Stand in for an easykube resource, backed by the fake client's state."""

    def __init__(self, client: "FakeClient", api_version: str, name: str) -> None:
        """Store the client and the resource this stands in for."""
        self.client = client
        self.key = (api_version, name)

    async def list(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        """Yield the objects of this resource."""
        for obj in self.client.objects.get(self.key, []):
            yield obj

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
        """Return the fake resource with the given name."""
        return FakeResource(self.client, self.api_version, name)


@pytest.fixture
def fake_ekclient(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Replace the operator's Kubernetes client with a fake."""
    client = FakeClient()
    monkeypatch.setattr(operator, "ekclient", client)
    return client
