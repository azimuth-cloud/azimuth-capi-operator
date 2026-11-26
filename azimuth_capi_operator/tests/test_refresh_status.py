"""Tests for refreshing the cluster status from its related objects."""

import asyncio
import logging

from azimuth_capi_operator import operator
from azimuth_capi_operator.config import settings
from azimuth_capi_operator.models import v1alpha1 as api
from azimuth_capi_operator.tests.conftest import FakeClient


def test_timer_rebuilds_lost_cluster_status(fake_ekclient: FakeClient) -> None:
    """Check that the timer rebuilds a cluster status that was never filled in.

    Event handlers are not retried, so after a lost event (e.g. following a Velero
    restore) the cluster would otherwise never become Ready.
    """
    fake_ekclient.objects = {
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
    cluster = api.Cluster.model_validate(
        {
            "apiVersion": f"{settings.api_group}/v1alpha1",
            "kind": "Cluster",
            "metadata": {"name": "test", "namespace": "tenant1"},
            "spec": {
                "label": "test",
                "templateName": "template1",
                "cloudCredentialsSecretName": "secret1",
                "controlPlaneMachineSize": "vm.small",
            },
            "status": {"phase": "Pending"},
        }
    )

    asyncio.run(
        operator.check_cluster_timeout(
            instance=cluster, logger=logging.getLogger(__name__), patch={}
        )
    )

    [(_, saved)] = fake_ekclient.replaced
    saved_status = api.ClusterStatus.model_validate(saved["status"])
    assert saved_status.phase == api.ClusterPhase.READY
    assert saved_status.kubernetes_version == "1.37.1"
