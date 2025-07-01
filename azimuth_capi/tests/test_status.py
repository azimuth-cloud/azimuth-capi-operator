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
