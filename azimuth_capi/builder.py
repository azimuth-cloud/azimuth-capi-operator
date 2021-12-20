import logging

from yaml.nodes import Node

from .models.v1alpha1 import (
    ClusterStatus,
    NodeGroupStatus,
    NodeStatus,
    ClusterPhase,
    NetworkingPhase,
    ControlPlanePhase,
    WorkersPhase,
    NodeGroupPhase,
    NodePhase,
    AddonsPhase
)


class ClusterStatusBuilder:
    """
    Builder object for updating a cluster status.
    """
    def __init__(self, status):
        self.status = ClusterStatus.parse_obj(status)
        self.logger = logging.getLogger(__name__)

    def build(self):
        """
        Returns the current status.
        """
        self.reconcile()
        return self.status.dict(by_alias = True)

    def reconcile_control_plane(self):
        """
        Reconcile the control plane status after changes have been made.
        """
        if self.status.control_plane.phase is ControlPlanePhase.READY:
            if any(
                node.phase in { NodePhase.UNHEALTHY, NodePhase.FAILED }
                for node in self.status.control_plane.nodes.values()
            ):
                self.status.control_plane.phase = ControlPlanePhase.DEGRADED
        return self

    def reconcile_node_group(self, node_group: NodeGroupStatus):
        """
        Reconcile the status of a node group after changes have been made.
        """
        node_group.count = len(node_group.nodes)
        # If the target version of the node group is different from any of the nodes,
        # flag the phase as upgrading
        if any(
            node.kubelet_version != node_group.target_version
            for node in node_group.nodes.values()
        ):
            node_group.phase = NodeGroupPhase.UPGRADING
        if node_group.phase is NodeGroupPhase.READY:
            if any(
                node.phase in { NodePhase.UNHEALTHY, NodePhase.FAILED }
                for node in node_group.nodes.values()
            ):
                node_group.phase = NodeGroupPhase.DEGRADED
        return node_group

    def reconcile_workers(self):
        """
        Reconcile the overall status of the workers after changes have been made.
        """
        # Reconcile each of the node groups first
        self.status.workers.node_groups = {
            name: self.reconcile_node_group(ng)
            for name, ng in self.status.workers.node_groups.items()
            # If the node group is in the deleting state and has no workers, remove it
            if ng.phase is not NodeGroupPhase.DELETING or ng.nodes
        }
        self.status.workers.count = sum(
            ng.count
            for ng in self.status.workers.node_groups.values()
        )
        # If there are no node groups, the overall worker phase should be ready because
        # no node groups is a perfectly valid state
        if not self.status.workers.node_groups:
            self.status.workers.phase = WorkersPhase.READY
        elif all(
            ng.phase is NodeGroupPhase.DELETING
            for ng in self.status.workers.node_groups.values()
        ):
            self.status.workers.phase = WorkersPhase.DELETING
        elif all(
            ng.phase is NodeGroupPhase.FAILED
            for ng in self.status.workers.node_groups.values()
        ):
            self.status.workers.phase = WorkersPhase.FAILED
        elif all(
            ng.phase is NodeGroupPhase.UNKNOWN
            for ng in self.status.workers.node_groups.values()
        ):
            self.status.workers.phase = WorkersPhase.UNKNOWN
        elif all(
            ng.phase is NodeGroupPhase.READY
            for ng in self.status.workers.node_groups.values()
        ):
            self.status.workers.phase = WorkersPhase.READY
        elif any(
            ng.phase is NodeGroupPhase.UPGRADING
            for ng in self.status.workers.node_groups.values()
        ):
            self.status.workers.phase = WorkersPhase.UPGRADING
        elif any(
            ng.phase in {
                NodeGroupPhase.SCALING_UP,
                NodeGroupPhase.SCALING_DOWN,
                NodeGroupPhase.DELETING
            }
            for ng in self.status.workers.node_groups.values()
        ):
            self.status.workers.phase = WorkersPhase.SCALING
        else:
            self.status.workers.phase = WorkersPhase.DEGRADED
        return self

    def reconcile(self):
        """
        Reconcile this object after patches have been made.
        """
        self.reconcile_control_plane()
        self.reconcile_workers()
        # Derive the overall phases from the component phases
        # Start with the networking phase, as that is provisioned first
        if self.status.networking.phase in {
            NetworkingPhase.PENDING,
            NetworkingPhase.PROVISIONING
        }:
            self.status.phase = ClusterPhase.INITIALISING
        elif self.status.networking.phase is NetworkingPhase.DELETING:
            self.status.phase = ClusterPhase.DELETING
        elif self.status.networking.phase is NetworkingPhase.FAILED:
            self.status.phase = ClusterPhase.FAILED
        elif self.status.networking.phase is NetworkingPhase.UNKNOWN:
            self.status.phase = ClusterPhase.UNKNOWN
        # We know that the networking is provisioned
        # Consider the control plane next
        elif self.status.control_plane.phase is ControlPlanePhase.PENDING:
            self.status.phase = ClusterPhase.INITIALISING
        elif self.status.control_plane.phase in {
            ControlPlanePhase.SCALING_UP,
            ControlPlanePhase.SCALING_DOWN
        }:
            self.status.phase = ClusterPhase.SCALING
        elif self.status.control_plane.phase is ControlPlanePhase.UPGRADING:
            self.status.phase = ClusterPhase.UPGRADING
        elif self.status.control_plane.phase is ControlPlanePhase.DELETING:
            self.status.phase = ClusterPhase.DELETING
        elif self.status.control_plane.phase is ControlPlanePhase.DEGRADED:
            self.status.phase = ClusterPhase.DEGRADED
        elif self.status.control_plane.phase is ControlPlanePhase.FAILED:
            self.status.phase = ClusterPhase.FAILED
        elif self.status.control_plane.phase is ControlPlanePhase.UNKNOWN:
            self.status.phase = ClusterPhase.UNKNOWN
        # Now we know that the control plane is ready, consider the workers next
        elif self.status.workers.phase is WorkersPhase.SCALING:
            self.status.phase = ClusterPhase.SCALING
        elif self.status.workers.phase is WorkersPhase.UPGRADING:
            self.status.phase = ClusterPhase.UPGRADING
        elif self.status.workers.phase is WorkersPhase.DELETING:
            # If the workers are deleting without the control plane or cluster also being in
            # the deleting phase, the cluster is removing the workers as part of a scaling
            # operation
            self.status.phase = ClusterPhase.SCALING
        elif self.status.workers.phase is WorkersPhase.DEGRADED:
            self.status.phase = ClusterPhase.DEGRADED
        elif self.status.workers.phase is WorkersPhase.FAILED:
            self.status.phase = ClusterPhase.DEGRADED
        elif self.status.workers.phase is ClusterPhase.UNKNOWN:
            self.status.phase = ClusterPhase.UNKNOWN
        # Just the addons left to consider
        elif self.status.addons.phase is AddonsPhase.PENDING:
            # Just leave the cluster in it's previous state for now
            pass
        elif self.status.addons.phase is AddonsPhase.DEPLOYING:
            self.status.phase = ClusterPhase.CONFIGURING_ADDONS
        elif self.status.addons.phase is AddonsPhase.FAILED:
            self.status.phase = ClusterPhase.DEGRADED
        elif self.status.addons.phase is AddonsPhase.UNKNOWN:
            self.status.phase = ClusterPhase.UNKNOWN
        else:
            self.status.phase = ClusterPhase.READY
        return self

    def cluster_updated(self, obj):
        """
        Updates the status when a CAPI cluster is updated.
        """
        # The networking phases correspond to the CAPI cluster phases
        phase = obj.get("status", {}).get("phase", "Unknown")
        self.status.networking.phase = NetworkingPhase(phase)
        return self

    def cluster_deleted(self, obj):
        """
        Updates the status when a CAPI cluster is deleted.
        """
        self.status.networking.phase = ClusterPhase.UNKNOWN
        return self

    def cluster_absent(self):
        """
        Called when the CAPI cluster is missing on resume.
        """
        return self.cluster_deleted(None)

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
                if components_healthy:
                    if components_healthy["status"] == "True":
                        next_phase = ControlPlanePhase.READY
                    else:
                        next_phase = ControlPlanePhase.DEGRADED
                else:
                    # If there is no healthy condition at all, don't change the status
                    next_phase = self.status.control_plane.phase
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
                next_phase = ControlPlanePhase.DEGRADED
        else:
            next_phase = ControlPlanePhase.PENDING
        self.status.control_plane.phase = next_phase
        # The Kubernetes version in the control plane object has a leading v that we don't want
        # The accurate version is in the status, but we use the spec if that is not set yet
        self.status.kubernetes_version = status.get("version", obj["spec"]["version"]).lstrip("v")
        return self

    def control_plane_deleted(self, obj):
        """
        Updates the status when a CAPI control plane is deleted.
        """
        self.status.control_plane.phase = ControlPlanePhase.UNKNOWN
        # Also reset the Kubernetes version of the cluster, as it can no longer be determined
        self.status.kubernetes_version = None
        return self

    def control_plane_absent(self):
        """
        Called when the control plane is missing on resume.
        """
        return self.control_plane_deleted(None)

    def _node_group_for_machine_deployment(self, obj):
        """
        Returns the node group for the given machine deployment.
        """
        name = obj["metadata"]["labels"]["capi.stackhpc.com/node-group"]
        return self.status.workers.node_groups.setdefault(name, NodeGroupStatus())

    def machine_deployment_updated(self, obj):
        """
        Updates the status when a CAPI machine deployment is updated.
        """
        node_group = self._node_group_for_machine_deployment(obj)
        status = obj.get("status", {})
        phase = status.get("phase", "Unknown")
        # We want to break the running phase down depending on the ready condition
        # All other CAPI phases correspond to the node group phases
        if phase == "Running":
            conditions = status.get("conditions", [])
            ready = next((c for c in conditions if c["type"] == "Ready"), None)
            if ready and ready["status"] == "True":
                node_group.phase = NodeGroupPhase.READY
            else:
                node_group.phase = NodeGroupPhase.DEGRADED
        else:
            node_group.phase = NodeGroupPhase(phase)
        # Set the target fields from the spec
        spec = obj.get("spec", {})
        node_group.target_count = spec.get("replicas", 0)
        target_version = spec.get("template", {}).get("spec", {}).get("version")
        if target_version:
            node_group.target_version = target_version.strip("v")
        return self

    def machine_deployment_deleted(self, obj):
        """
        Updates the status when a CAPI machine deployment is deleted.
        """
        # Machine deployments are removed before the machines they manage have finished deleting
        # So instead of removing the node group, we put it into a deleting state, and the
        # status reconciliation removes it when it has no nodes left
        node_group = self._node_group_for_machine_deployment(obj)
        node_group.phase = NodeGroupPhase.DELETING
        return self

    def remove_unknown_node_groups(self, mds):
        """
        Given the current set of machine deployments, remove any unknown node groups from the status.
        """
        known = set(self.status.workers.node_groups.keys())
        current = set(md.metadata.labels["capi.stackhpc.com/node-group"] for md in mds)
        for name in known - current:
            self.status.workers.node_groups.pop(name)

    def _nodes_for_machine(self, obj):
        """
        Returns the nodes dictionary for the given machine.
        """
        labels = obj["metadata"]["labels"]
        component = labels["capi.stackhpc.com/component"]
        if component == "control-plane":
            return self.status.control_plane.nodes
        else:
            node_group_name = labels["capi.stackhpc.com/node-group"]
            node_group = self.status.workers.node_groups.setdefault(
                node_group_name,
                NodeGroupStatus()
            )
            return node_group.nodes

    def machine_updated(self, obj):
        """
        Updates the status when a CAPI machine is updated.
        """
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
        # Replace the node object in the appropriate node set
        self._nodes_for_machine(obj)[obj["metadata"]["name"]] = NodeStatus(
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
            kubelet_version = obj["spec"]["version"].lstrip("v")
        )
        return self

    def machine_deleted(self, obj):
        """
        Updates the status when a CAPI machine is deleted.
        """
        # Just remove the node from the corresponding node set
        self._nodes_for_machine(obj).pop(obj["metadata"]["name"])
        return self

    def remove_unknown_nodes(self, machines):
        """
        Given the current set of machines, remove any unknown nodes from the status.
        """
        current = set(m.metadata.name for m in machines)
        known = set(self.status.control_plane.nodes.keys())
        for name in known - current:
            self.status.control_plane.nodes.pop(name)
        for node_group in self.status.workers.node_groups.values():
            known = set(node_group.nodes.keys())
            for name in known - current:
                node_group.nodes.pop(name)

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
            self.status.addons.phase = AddonsPhase.DEPLOYED
        elif failed and failed["status"] == "True":
            self.status.addons.phase = AddonsPhase.FAILED
        elif status.get("active", 0) > 0:
            self.status.addons.phase = AddonsPhase.DEPLOYING
        else:
            self.status.addons.phase = AddonsPhase.PENDING
        return self

    def addons_job_absent(self):
        """
        Called when the addons job is absent after a resume.
        """
        self.status.addons.phase = AddonsPhase.UNKNOWN
        return self
