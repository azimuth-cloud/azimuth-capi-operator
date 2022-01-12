import logging

from yaml.nodes import Node

from azimuth_capi.capi_resources import Cluster

from .models.v1alpha1 import (
    ClusterPhase,
    NetworkingPhase,
    ControlPlanePhase,
    NodePhase,
    AddonsPhase,
    ClusterStatus,
    NodeRole,
    NodeStatus,
)


class ClusterStatusBuilder:
    """
    Builder object for updating a cluster status.
    """
    def __init__(self, status):
        self.status: ClusterStatus = ClusterStatus.parse_obj(status)
        self.logger = logging.getLogger(__name__)

    def build(self):
        """
        Returns the current status.
        """
        self._reconcile_cluster_phase()
        return self.status.dict(by_alias = True)

    def _any_node_has_phase(self, *phases):
        """
        Returns true if any node has one of the given phases.
        """
        return any(node.phase in phases for node in self.status.nodes.values())

    def _multiple_kubelet_versions(self, role):
        """
        Returns true if nodes with the given role have different kubelet versions,
        false otherwise.
        """
        versions = set(
            node.kubelet_version
            for node in self.status.nodes.values()
            if node.role is role
        )
        return len(versions) > 1

    def _reconcile_cluster_phase(self):
        """
        Sets the overall cluster phase based on the component phases.
        """
        if self.status.networking_phase in {
            NetworkingPhase.PENDING,
            NetworkingPhase.PROVISIONING
        }:
            self.status.phase = ClusterPhase.RECONCILING
        elif self.status.networking_phase is NetworkingPhase.DELETING:
            self.status.phase = ClusterPhase.DELETING
        elif self.status.networking_phase is NetworkingPhase.FAILED:
            self.status.phase = ClusterPhase.FAILED
        elif self.status.networking_phase is NetworkingPhase.UNKNOWN:
            self.status.phase = ClusterPhase.UNKNOWN
        # The networking phase is Provisioned
        elif self.status.control_plane_phase in {
            ControlPlanePhase.PENDING,
            ControlPlanePhase.SCALING_UP,
            ControlPlanePhase.SCALING_DOWN
        }:
            # If the control plane is scaling but there are control plane nodes with
            # different versions, that is still part of an upgrade
            if self._multiple_kubelet_versions(NodeRole.CONTROL_PLANE):
                self.status.phase = ClusterPhase.UPGRADING
            else:
                self.status.phase = ClusterPhase.RECONCILING
        elif self.status.control_plane_phase is ControlPlanePhase.UPGRADING:
            self.status.phase = ClusterPhase.UPGRADING
        elif self.status.control_plane_phase is ControlPlanePhase.DELETING:
            self.status.phase = ClusterPhase.DELETING
        elif self.status.control_plane_phase is ControlPlanePhase.FAILED:
            self.status.phase = ClusterPhase.FAILED
        elif self.status.control_plane_phase is ControlPlanePhase.UNKNOWN:
            self.status.phase = ClusterPhase.UNKNOWN
        # The control plane phase is Ready or Unhealthy
        # If there are workers with different versions, assume an upgrade is in progress
        elif self._multiple_kubelet_versions(NodeRole.WORKER):
            self.status.phase = ClusterPhase.UPGRADING
        elif self._any_node_has_phase(
            NodePhase.PENDING,
            NodePhase.PROVISIONING,
            NodePhase.PROVISIONED,
            NodePhase.DELETING,
            NodePhase.DELETED
        ):
            self.status.phase = ClusterPhase.RECONCILING
        # All nodes are either Ready, Unhealthy, Failed or Unknown
        elif self.status.addons_phase in {
            AddonsPhase.PENDING,
            AddonsPhase.DEPLOYING
        }:
            self.status.phase = ClusterPhase.RECONCILING
        elif self.status.addons_phase is AddonsPhase.FAILED:
            self.status.phase = ClusterPhase.FAILED
        elif self.status.addons_phase is AddonsPhase.UNKNOWN:
            self.status.phase = ClusterPhase.UNKNOWN
        # The addons are Deployed
        # Now we know that there is no reconciliation happening, consider cluster health
        elif (
            self.status.control_plane_phase is ControlPlanePhase.UNHEALTHY or
            self._any_node_has_phase(
                NodePhase.UNHEALTHY,
                NodePhase.FAILED,
                NodePhase.UNKNOWN
            )
        ):
            self.status.phase = ClusterPhase.UNHEALTHY
        else:
            self.status.phase = ClusterPhase.READY


    def cluster_updated(self, obj):
        """
        Updates the status when a CAPI cluster is updated.
        """
        # Just set the networking phase
        phase = obj.get("status", {}).get("phase", "Unknown")
        self.status.networking_phase = NetworkingPhase(phase)
        return self

    def cluster_deleted(self, obj):
        """
        Updates the status when a CAPI cluster is deleted.
        """
        self.status.networking_phase = NetworkingPhase.UNKNOWN
        return self

    def cluster_absent(self):
        """
        Called when the CAPI cluster is missing on resume.
        """
        self.status.networking_phase = NetworkingPhase.UNKNOWN
        return self

    def control_plane_updated(self, obj):
        """
        Updates the status when a CAPI control plane is updated.
        """
        status = obj.get("status", {})
        # The control plane object has no phase property
        # Instead, we must derive it from the conditions
        conditions = status.get("conditions", [])
        ready = next((c for c in conditions if c["type"] == "Ready"), None)
        components_healthy = next(
            (c for c in conditions if c["type"] == "ControlPlaneComponentsHealthy"),
            None
        )
        if ready:
            if ready["status"] == "True":
                if components_healthy and components_healthy["status"] == "True":
                    next_phase = ControlPlanePhase.READY
                else:
                    next_phase = ControlPlanePhase.UNHEALTHY
            elif ready["reason"] == "ScalingUp":
                next_phase = ControlPlanePhase.SCALING_UP
            elif ready["reason"] == "ScalingDown":
                next_phase = ControlPlanePhase.SCALING_DOWN
            elif ready["reason"].startswith("Waiting"):
                next_phase = ControlPlanePhase.SCALING_UP
            elif ready["reason"] == "RollingUpdateInProgress":
                next_phase = ControlPlanePhase.UPGRADING
            elif ready["reason"] == "Deleting":
                next_phase = ControlPlanePhase.DELETING
            else:
                self.logger.warn("UNKNOWN CONTROL PLANE REASON: %s", ready["reason"])
                next_phase = ControlPlanePhase.UNHEALTHY
        else:
            next_phase = ControlPlanePhase.PENDING
        self.status.control_plane_phase = next_phase
        # The Kubernetes version in the control plane object has a leading v that we don't want
        # The accurate version is in the status, but we use the spec if that is not set yet
        self.status.kubernetes_version = status.get("version", obj["spec"]["version"]).lstrip("v")
        return self

    def control_plane_deleted(self, obj):
        """
        Updates the status when a CAPI control plane is deleted.
        """
        self.status.control_plane_phase = ControlPlanePhase.UNKNOWN
        # Also reset the Kubernetes version of the cluster, as it can no longer be determined
        self.status.kubernetes_version = None
        return self

    def control_plane_absent(self):
        """
        Called when the control plane is missing on resume.
        """
        self.status.control_plane_phase = ControlPlanePhase.UNKNOWN
        # Also reset the Kubernetes version of the cluster, as it can no longer be determined
        self.status.kubernetes_version = None
        return self

    def machine_updated(self, obj):
        """
        Updates the status when a CAPI machine is updated.
        """
        labels = obj["metadata"]["labels"]
        status = obj.get("status", {})
        phase = status.get("phase", "Unknown")
        # We want to break the running phase down depending on node health
        # All other CAPI machine phases correspond to the node phases
        if phase == "Running":
            conditions = status.get("conditions", [])
            healthy = next((c for c in conditions if c["type"] == "NodeHealthy"), None)
            if healthy and healthy["status"] == "True":
                node_phase = NodePhase.READY
            else:
                node_phase = NodePhase.UNHEALTHY
        else:
            node_phase = NodePhase(phase)
        # Replace the node object in the node set
        self.status.nodes[obj["metadata"]["name"]] = NodeStatus(
            # The node role should be in the labels
            role = NodeRole(labels["capi.stackhpc.com/component"]),
            phase = node_phase,
            ip = next(
                (
                    a["address"]
                    for a in status.get("addresses", [])
                    if a["type"] == "InternalIP"
                ),
                None
            ),
            # Take the version from the spec, which should always be set
            kubelet_version = obj["spec"]["version"].lstrip("v"),
            # The node group will be in a label if applicable
            node_group = labels.get("capi.stackhpc.com/node-group")
        )
        # Also update the node count
        self.status.node_count = len(self.status.nodes)
        return self

    def machine_deleted(self, obj):
        """
        Updates the status when a CAPI machine is deleted.
        """
        # Just remove the node from the node set
        self.status.nodes.pop(obj["metadata"]["name"], None)
        self.status.node_count = len(self.status.nodes)
        return self

    def remove_unknown_nodes(self, machines):
        """
        Given the current set of machines, remove any unknown nodes from the status.
        """
        current = set(m.metadata.name for m in machines)
        known = set(self.status.nodes.keys())
        for name in known - current:
            self.status.nodes.pop(name)
        self.status.node_count = len(self.status.nodes)

    def kubeconfig_secret_updated(self, obj):
        """
        Updates the status when a kubeconfig secret is updated.
        """
        self.status.kubeconfig_secret_name = obj["metadata"]["name"]
        return self

    def addons_job_updated(self, obj):
        """
        Updates the status when an addons job is updated.
        """
        status = obj.get("status", {})
        conditions = status.get("conditions", [])
        complete = next((c for c in conditions if c["type"] == "Complete"), None)
        failed = next((c for c in conditions if c["type"] == "Failed"), None)
        if complete and complete["status"] == "True":
            self.status.addons_phase = AddonsPhase.DEPLOYED
        elif failed and failed["status"] == "True":
            self.status.addons_phase = AddonsPhase.FAILED
        elif status.get("active", 0) > 0:
            self.status.addons_phase = AddonsPhase.DEPLOYING
        else:
            self.status.addons_phase = AddonsPhase.PENDING
        return self

    def addons_job_absent(self):
        """
        Called when the addons job is absent after a resume.
        """
        self.status.addons_phase = AddonsPhase.UNKNOWN
        return self
