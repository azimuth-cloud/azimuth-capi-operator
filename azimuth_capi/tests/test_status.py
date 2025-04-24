import unittest
from unittest import mock

from azimuth_capi import status
from azimuth_capi.models import v1alpha1 as api


class TestOperator(unittest.TestCase):
    def test_flux_updated_pass(self):
        cluster = mock.Mock()
        cluster.status.addons = {}
        addon = {"metadata": {"labels": {"capi.stackhpc.com/component": "foo"}}}

        status.flux_updated(cluster, addon)

        addon_status = cluster.status.addons["foo"]
        self.assertEqual(addon_status.phase, api.cluster.AddonPhase.UNKNOWN)
        self.assertEqual(addon_status.revision, 0)
