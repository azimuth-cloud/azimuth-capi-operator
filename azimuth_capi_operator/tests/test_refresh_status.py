"""Tests for refreshing the cluster status from its related objects."""

import asyncio
import datetime as dt
import logging
from typing import Any

import pytest
from easykube import ApiError

from azimuth_capi_operator import operator
from azimuth_capi_operator.config import settings
from azimuth_capi_operator.models import v1alpha1 as api
from azimuth_capi_operator.tests.conftest import FakeClient

INFRA_MACHINES = ("infrastructure.cluster.x-k8s.io/v1beta1", "openstackmachines")


def healthy_capi_objects() -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Return a healthy CAPI cluster and control plane."""
    return {
        (operator.CLUSTER_API_VERSION, "clusters"): [
            {"status": {"phase": "Provisioned", "v1beta2": {"conditions": []}}},
        ],
        (operator.CLUSTER_API_CONTROLPLANE_VERSION, "kubeadmcontrolplanes"): [
            {
                "spec": {"version": "v1.37.1"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "ControlPlaneComponentsHealthy", "status": "True"},
                    ],
                },
            },
        ],
    }


def worker_machine(name: str) -> dict[str, Any]:
    """Return a healthy worker machine with an OpenStack machine of the same name."""
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "labels": {"capi.stackhpc.com/component": "worker"},
        },
        "spec": {
            "version": "v1.37.1",
            "infrastructureRef": {
                "apiVersion": INFRA_MACHINES[0],
                "kind": INFRA_MACHINES[1],
                "name": name,
                "namespace": "tenant1",
            },
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "NodeHealthy", "status": "True"}],
        },
    }


def addon(component: str, status: dict[str, Any]) -> dict[str, Any]:
    """Return an addon object for the given component."""
    return {
        "metadata": {"labels": {"capi.stackhpc.com/component": component}},
        "status": status,
    }


def pending_cluster(**spec: str) -> api.Cluster:
    """Return a cluster whose status has not been filled in."""
    return api.Cluster.model_validate(
        {
            "apiVersion": f"{settings.api_group}/v1alpha1",
            "kind": "Cluster",
            "metadata": {"name": "test", "namespace": "tenant1"},
            "spec": {
                "label": "test",
                "templateName": "template1",
                "cloudCredentialsSecretName": "secret1",
                "controlPlaneMachineSize": "vm.small",
                **spec,
            },
            "status": {"phase": "Pending"},
        }
    )


def run_timer(cluster: api.Cluster, client: FakeClient) -> api.ClusterStatus:
    """Run the cluster timer once and return the status it saved."""
    asyncio.run(
        operator.check_cluster_timeout(
            instance=cluster, logger=logging.getLogger(__name__), patch={}
        )
    )
    [(_, saved)] = client.replaced
    return api.ClusterStatus.model_validate(saved["status"])


def test_timer_rebuilds_lost_cluster_status(fake_ekclient: FakeClient) -> None:
    """Check that the timer rebuilds a cluster status that was never filled in.

    Event handlers are not retried, so after a lost event (e.g. following a Velero
    restore) the cluster would otherwise never become Ready.
    """
    fake_ekclient.objects = healthy_capi_objects()

    saved_status = run_timer(pending_cluster(), fake_ekclient)

    assert saved_status.phase == api.ClusterPhase.READY
    assert saved_status.kubernetes_version == "1.37.1"


def test_timer_rebuilds_lease_phase(fake_ekclient: FakeClient) -> None:
    """Check that the timer fills in the phase of the cluster's lease.

    An unknown lease phase keeps the cluster Pending, whatever state CAPI is in.
    """
    fake_ekclient.objects = {
        **healthy_capi_objects(),
        (operator.AZIMUTH_SCHEDULING_VERSION, "leases"): [
            {"metadata": {"name": "test-lease"}, "status": {"phase": "Active"}},
        ],
    }

    saved_status = run_timer(pending_cluster(leaseName="test-lease"), fake_ekclient)

    assert saved_status.lease_phase == api.LeasePhase.ACTIVE
    assert saved_status.phase == api.ClusterPhase.READY


def test_timer_rebuilds_nodes_from_machines(fake_ekclient: FakeClient) -> None:
    """Check that the timer rebuilds the nodes from the machines that still exist.

    A machine whose OpenStack machine has gone is being deleted, and must not stop
    the rest of the status from being repaired.
    """
    fake_ekclient.objects = {
        **healthy_capi_objects(),
        (operator.CLUSTER_API_VERSION, "machines"): [
            worker_machine("worker-0"),
            worker_machine("worker-deleting"),
        ],
        INFRA_MACHINES: [
            {"metadata": {"name": "worker-0"}, "spec": {"flavor": "m1.small"}},
        ],
    }
    cluster = pending_cluster()
    cluster.status.nodes["worker-gone"] = api.NodeStatus(
        role=api.NodeRole.WORKER, phase=api.NodePhase.READY
    )

    saved_status = run_timer(cluster, fake_ekclient)

    assert set(saved_status.nodes) == {"worker-0"}


def test_timer_handles_new_capi_cluster(fake_ekclient: FakeClient) -> None:
    """Check that the timer handles a CAPI cluster that has no status yet.

    A newly created CAPI cluster has no status, and the refresh should report the
    cluster as Pending.
    """
    fake_ekclient.objects = {(operator.CLUSTER_API_VERSION, "clusters"): [{}]}

    saved_status = run_timer(pending_cluster(), fake_ekclient)

    assert saved_status.phase == api.ClusterPhase.PENDING


def test_timer_rebuilds_status_without_flux(fake_ekclient: FakeClient) -> None:
    """Check that the timer rebuilds the status when Flux is not installed.

    Flux is optional, and its missing API must not stop the status being repaired.
    """
    fake_ekclient.objects = healthy_capi_objects()
    fake_ekclient.missing_apis = {"kustomize.toolkit.fluxcd.io/v1"}

    saved_status = run_timer(pending_cluster(), fake_ekclient)

    assert saved_status.phase == api.ClusterPhase.READY


def test_timer_times_out_stuck_cluster(fake_ekclient: FakeClient) -> None:
    """Check that the timer still marks a cluster stuck mid-change as Unhealthy."""
    fake_ekclient.objects = healthy_capi_objects()
    [control_plane] = fake_ekclient.objects[
        (operator.CLUSTER_API_CONTROLPLANE_VERSION, "kubeadmcontrolplanes")
    ]
    control_plane["status"]["conditions"] = [
        {"type": "Ready", "status": "False", "reason": "ScalingUp"},
    ]
    cluster = pending_cluster()
    cluster.status.last_updated = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)

    saved_status = run_timer(cluster, fake_ekclient)

    assert saved_status.phase == api.ClusterPhase.UNHEALTHY


def test_timer_rebuilds_addons(fake_ekclient: FakeClient) -> None:
    """Check that the timer rebuilds the addons from the cluster's addon objects."""
    fake_ekclient.objects = {
        **healthy_capi_objects(),
        ("addons.stackhpc.com/v1alpha1", "helmreleases"): [
            addon("cni", {"phase": "Deployed", "revision": 1}),
        ],
        ("addons.stackhpc.com/v1alpha1", "manifests"): [
            addon("extra", {"phase": "Deployed", "revision": 1}),
        ],
        ("kustomize.toolkit.fluxcd.io/v1", "kustomizations"): [
            addon("flux", {"conditions": [{"type": "Ready", "status": "True"}]}),
        ],
    }
    cluster = pending_cluster()
    cluster.status.addons["gone"] = api.AddonStatus(
        phase=api.AddonPhase.DEPLOYED, revision=1
    )

    saved_status = run_timer(cluster, fake_ekclient)

    assert set(saved_status.addons) == {"cni", "extra", "flux"}


def test_timer_keeps_status_without_required_api(fake_ekclient: FakeClient) -> None:
    """Check that the timer leaves the status alone when a required API is missing.

    Treating it as having no objects would wipe the nodes and addons from the status.
    """
    fake_ekclient.objects = healthy_capi_objects()
    fake_ekclient.missing_apis = {operator.CLUSTER_API_VERSION}

    with pytest.raises(ApiError):
        run_timer(pending_cluster(), fake_ekclient)

    assert fake_ekclient.replaced == []
