import unittest
from unittest import mock

import kopf

from azimuth_capi import operator
from azimuth_capi.models import v1alpha1 as api

class TestOperator(unittest.IsolatedAsyncioTestCase):
    def get_fake_cluster(self) -> api.Cluster:
        fake_cluster = dict(
            apiVersion="capi.azimuth.stackhpc.com/v1alpha1",
            kind="Cluster",
            metadata=dict(
                name="test",
                namespace="tenant1",
            ),
            spec=dict(
                label="test",
                templateName="template1.31",
                cloudCredentialsSecretName="secret1",
                controlPlaneMachineSize="vm.small",
                nodePools=[
                    dict(
                        name="nodepool1",
                        machineSize="vm.small",
                        count=2,
                    )
                ]
            )
        )
        return api.Cluster(**fake_cluster)

    def get_fake_cluster_template(self) -> api.ClusterTemplate:
        fake_cluster_template = dict(
            apiVersion="capi.azimuth.stackhpc.com/v1alpha1",
            kind="ClusterTemplate",
            metadata=dict(
                name="test",
                namespace="tenant1",
            ),
            spec=dict(
                label="test",
                description="My test template",
                values=dict(
                    kubernetesVersion="v1.31.0",
                    machineImageId="12456789",
                )
            )
        )
        return api.ClusterTemplate(**fake_cluster_template)

    def test_generate_helm_values_for_release(self):
        cluster = self.get_fake_cluster()
        template = self.get_fake_cluster_template()

        result = operator.generate_helm_values_for_release(template, cluster)

        self.assertDictEqual(result, {
            'addons': {'ingress': {'enabled': False},
            'kubernetesDashboard': {'enabled': False},
            'monitoring': {'enabled': False}},
            'cloudCredentialsSecretName': 'secret1',
            'controlPlane': {'healthCheck': {'enabled': True},
                            'machineFlavor': 'vm.small'},
            'kubernetesVersion': 'v1.31.0',
            'machineImageId': '12456789',
            'nodeGroupDefaults': {'healthCheck': {'enabled': True}},
            'nodeGroups': None}
        )
