import datetime as dt
import unittest
from unittest import mock

from azimuth_capi import status
from azimuth_capi.models import v1alpha1 as api


def get_flux_test_object(condition, clustername="foo"):
    return {
        "metadata": {"labels": {"capi.stackhpc.com/component": clustername}},
        "status": {"conditions": [condition]},
    }


class TestStatus(unittest.TestCase):
    def test_machine_updated_records_certificate_expiry_date(self):
        cluster = mock.Mock()
        cluster.status.nodes = {}
        infra_machine = mock.Mock()
        infra_machine.spec.flavor = "m1.medium"
        machine = {
            "metadata": {
                "name": "control-plane-0",
                "creationTimestamp": "2026-01-01T00:00:00Z",
                "labels": {"capi.stackhpc.com/component": "control-plane"},
            },
            "spec": {"version": "v1.32.1"},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "NodeHealthy", "status": "True"}],
                "certificatesExpiryDate": "2027-01-01T12:00:00Z",
            },
        }

        status.machine_updated(cluster, machine, infra_machine)

        node = cluster.status.nodes["control-plane-0"]
        self.assertEqual(
            node.certificates_expiry_date,
            dt.datetime(2027, 1, 1, 12, tzinfo=dt.timezone.utc),
        )

    def test_machine_updated_records_no_certificate_expiry_date_for_worker(self):
        cluster = mock.Mock()
        cluster.status.nodes = {}
        infra_machine = mock.Mock()
        infra_machine.spec.flavor = "m1.medium"
        machine = {
            "metadata": {
                "name": "worker-0",
                "creationTimestamp": "2026-01-01T00:00:00Z",
                "labels": {"capi.stackhpc.com/component": "worker"},
            },
            "spec": {"version": "v1.32.1"},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "NodeHealthy", "status": "True"}],
            },
        }

        status.machine_updated(cluster, machine, infra_machine)

        node = cluster.status.nodes["worker-0"]
        self.assertEqual(node.role, api.NodeRole.WORKER)
        self.assertIsNone(node.certificates_expiry_date)
        self.assertNotIn(
            "certificatesExpiryDate", node.model_dump(exclude_defaults=True)
        )

    def test_control_plane_updated_records_certificate_rotation_days(self):
        cluster = mock.Mock()
        control_plane = {
            "spec": {
                "version": "v1.32.1",
                "rolloutBefore": {"certificatesExpiryDays": 21},
            },
            "status": {
                "version": "v1.32.1",
                "conditions": [
                    {"type": "Ready", "status": "True"},
                    {
                        "type": "ControlPlaneComponentsHealthy",
                        "status": "True",
                    },
                ],
            },
        }

        status.control_plane_updated(cluster, control_plane)

        self.assertEqual(
            cluster.status.control_plane_certificate_rotation_days,
            21,
        )

    def test_control_plane_updated_clears_missing_certificate_rotation_days(self):
        cluster = mock.Mock()
        cluster.status.control_plane_certificate_rotation_days = 21
        control_plane = {
            "spec": {"version": "v1.32.1"},
            "status": {
                "version": "v1.32.1",
                "conditions": [
                    {"type": "Ready", "status": "True"},
                    {
                        "type": "ControlPlaneComponentsHealthy",
                        "status": "True",
                    },
                ],
            },
        }

        status.control_plane_updated(cluster, control_plane)

        self.assertIsNone(
            cluster.status.control_plane_certificate_rotation_days,
        )

    def test_control_plane_certificate_status_uses_earliest_expiry(self):
        cluster = mock.Mock()
        cluster.status = api.ClusterStatus(
            control_plane_certificate_rotation_days=21,
            nodes={
                "control-plane-0": api.NodeStatus(
                    role=api.NodeRole.CONTROL_PLANE,
                    certificates_expiry_date="2027-04-01T12:00:00Z",
                ),
                "control-plane-1": api.NodeStatus(
                    role=api.NodeRole.CONTROL_PLANE,
                    certificates_expiry_date="2027-03-20T12:00:00Z",
                ),
                # when one control plane machine has no expiry field on its status
                "control-plane-2": api.NodeStatus(
                    role=api.NodeRole.CONTROL_PLANE,
                ),
                "worker-0": api.NodeStatus(
                    role=api.NodeRole.WORKER,
                ),
            },
        )

        status._update_control_plane_certificate_status(cluster)

        self.assertEqual(
            cluster.status.control_plane_certificate_expiry_date,
            dt.datetime(2027, 3, 20, 12, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            cluster.status.control_plane_certificate_rotation_date,
            dt.datetime(2027, 2, 27, 12, tzinfo=dt.timezone.utc),
        )

    def test_control_plane_certificate_status_ignores_unexpected_worker_expiry(self):
        cluster = mock.Mock()
        cluster.status = api.ClusterStatus(
            nodes={
                "control-plane-0": api.NodeStatus(
                    role=api.NodeRole.CONTROL_PLANE,
                    certificates_expiry_date="2027-03-20T12:00:00Z",
                ),
                # upstream CAPI does not normally set certificate expiry on workers.
                # This verifies an unexpected value cannot affect
                # the control plane summary.
                "worker-0": api.NodeStatus(
                    role=api.NodeRole.WORKER,
                    certificates_expiry_date="2027-01-01T12:00:00Z",
                ),
            },
        )

        status._update_control_plane_certificate_status(cluster)

        self.assertEqual(
            cluster.status.control_plane_certificate_expiry_date,
            dt.datetime(2027, 3, 20, 12, tzinfo=dt.timezone.utc),
        )

    def test_control_plane_certificate_status_clears_unknown_values(self):
        cluster = mock.Mock()
        cluster.status = api.ClusterStatus(
            control_plane_certificate_expiry_date="2027-03-20T12:00:00Z",
            control_plane_certificate_rotation_days=21,
            control_plane_certificate_rotation_date="2027-02-27T12:00:00Z",
            nodes={
                "control-plane-0": api.NodeStatus(
                    role=api.NodeRole.CONTROL_PLANE,
                ),
                "worker-0": api.NodeStatus(
                    role=api.NodeRole.WORKER,
                ),
            },
        )

        status._update_control_plane_certificate_status(cluster)

        self.assertIsNone(
            cluster.status.control_plane_certificate_expiry_date,
        )
        self.assertIsNone(
            cluster.status.control_plane_certificate_rotation_date,
        )

    def test_flux_updated_empty(self):
        cluster = mock.Mock()
        cluster.status.addons = {}
        flux_kustomization_body = {
            "metadata": {"labels": {"capi.stackhpc.com/component": "foo"}}
        }

        status.flux_updated(cluster, flux_kustomization_body)

        addon_status = cluster.status.addons["foo"]
        self.assertEqual(addon_status.phase, api.cluster.AddonPhase.UNKNOWN)
        self.assertEqual(addon_status.revision, 0)

    def test_flux_updated_ready(self):
        cluster = mock.Mock()
        cluster.status.addons = {}
        flux_kustomization_body = get_flux_test_object(
            {"status": "True", "type": "Ready"}
        )

        status.flux_updated(cluster, flux_kustomization_body)

        addon_status = cluster.status.addons["foo"]
        self.assertEqual(addon_status.phase, api.cluster.AddonPhase.DEPLOYED)

    def test_flux_updated_failed(self):
        cluster = mock.Mock()
        cluster.status.addons = {}
        flux_kustomization_body = get_flux_test_object(
            {"status": "False", "type": "Ready"}
        )

        status.flux_updated(cluster, flux_kustomization_body)

        addon_status = cluster.status.addons["foo"]
        self.assertEqual(addon_status.phase, api.cluster.AddonPhase.FAILED)

    def test_flux_updated_installing(self):
        cluster = mock.Mock()
        cluster.status.addons = {}
        flux_kustomization_body = get_flux_test_object(
            {"status": "False", "type": "Reconciling"}
        )

        status.flux_updated(cluster, flux_kustomization_body)

        addon_status = cluster.status.addons["foo"]
        self.assertEqual(addon_status.phase, api.cluster.AddonPhase.PENDING)

    def test_flux_updated_revision_updates(self):
        cluster = mock.Mock()
        cluster.status.addons = {}
        flux_kustomization_body = get_flux_test_object({"observedGeneration": 1})

        status.flux_updated(cluster, flux_kustomization_body)

        addon_status = cluster.status.addons["foo"]
        self.assertEqual(addon_status.revision, 1)
