import logging

from .models.v1alpha1 import (
    ClusterPhase,
    NetworkingPhase,
    ControlPlanePhase,
    NodePhase,
    AddonPhase,
    ClusterStatus,
    NodeRole,
    NodeStatus,
    AddonStatus,
    ServiceStatus,
)


class ClusterStatusBuilder:
    """
    Builder object for updating a cluster status.
    """
    def __init__(self, status):
        self.status: ClusterStatus = ClusterStatus.parse_obj(status)
        # Store the current status so we can compare at the end
        self._previous_status = self.status.dict(by_alias = True)
        self.logger = logging.getLogger(__name__)

    def build(self):
        """
        Returns a tuple of (changed, status)
        """
        self._reconcile_cluster_phase()
        self.status.node_count = len(self.status.nodes)
        self.status.addon_count = len(self.status.addons)
        next_status = self.status.dict(by_alias = True)
        return next_status != self._previous_status, next_status

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

    def _any_addon_has_phase(self, *phases):
        """
        Returns true if any addon has one of the given phases.
        """
        return any(addon.phase in phases for addon in self.status.addons.values())

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
            NodePhase.DELETING,
            NodePhase.DELETED
        ):
            self.status.phase = ClusterPhase.RECONCILING
        # All nodes are either Ready, Unhealthy, Failed or Unknown
        elif self._any_addon_has_phase(
            AddonPhase.PENDING,
            AddonPhase.INSTALLING,
            AddonPhase.UNINSTALLING
        ):
            self.status.phase = ClusterPhase.RECONCILING
        # All addons are either Ready, Failed or Unknown
        # Now we know that there is no reconciliation happening, consider cluster health
        elif (
            self.status.control_plane_phase is ControlPlanePhase.UNHEALTHY or
            self._any_node_has_phase(
                NodePhase.UNHEALTHY,
                NodePhase.FAILED,
                NodePhase.UNKNOWN
            ) or
            self._any_addon_has_phase(
                AddonPhase.FAILED,
                AddonPhase.UNKNOWN
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
        # We also want to remove the Provisioned phase as a transition between Provisioning
        # and Ready, and just leave those nodes in the Provisioning phase
        # All other CAPI machine phases correspond to the node phases
        if phase == "Running":
            conditions = status.get("conditions", [])
            healthy = next((c for c in conditions if c["type"] == "NodeHealthy"), None)
            if healthy and healthy["status"] == "True":
                node_phase = NodePhase.READY
            else:
                node_phase = NodePhase.UNHEALTHY
        elif phase == "Provisioned":
            node_phase = NodePhase.PROVISIONING
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
            node_group = labels.get("capi.stackhpc.com/node-group"),
            # Use the timestamp from the metadata for the created time
            created = obj["metadata"]["creationTimestamp"]
        )
        return self

    def machine_deleted(self, obj):
        """
        Updates the status when a CAPI machine is deleted.
        """
        # Just remove the node from the node set
        self.status.nodes.pop(obj["metadata"]["name"], None)
        return self

    def remove_unknown_nodes(self, machines):
        """
        Given the current set of machines, remove any unknown nodes from the status.
        """
        current = set(m.metadata.name for m in machines)
        known = set(self.status.nodes.keys())
        for name in known - current:
            self.status.nodes.pop(name)
        return self

    def kubeconfig_secret_updated(self, obj):
        """
        Updates the status when a kubeconfig secret is updated.
        """
        self.status.kubeconfig_secret_name = obj["metadata"]["name"]
        return self

    def _addon_is_latest_revision(self, component, revision):
        """
        Returns true if the specified revision is the latest for the given component.
        """
        # If the revision is >= the previous largest revision we have seen, it is the latest
        latest = getattr(self.status.addons.get(component), "revision", 0)
        return revision >= latest

    def _addon_previous_phase(self, component):
        """
        Returns the previous phase for the addon.
        """
        addon = self.status.addons.get(component)
        return AddonPhase(addon.phase) if addon else AddonPhase.UNKNOWN

    def addon_job_updated(self, obj):
        """
        Updates the status when an addon job is updated.
        """
        labels = obj["metadata"]["labels"]
        component = labels["app.kubernetes.io/component"]
        operation = labels["capi.stackhpc.com/operation"]
        revision = int(labels["capi.stackhpc.com/revision"])
        # Suspended jobs are not considered
        if obj.get("spec", {}).get("suspend", False):
            return self
        # We only process jobs for the latest revision
        if self._addon_is_latest_revision(component, revision):
            previous_phase = self._addon_previous_phase(component)
            # Then calculate the next phase for the addon
            status = obj.get("status", {})
            conditions = status.get("conditions", [])
            complete = next((c for c in conditions if c["type"] == "Complete"), None)
            failed = next((c for c in conditions if c["type"] == "Failed"), None)
            is_complete = complete and complete["status"] == "True"
            is_failed = failed and failed["status"] == "True"
            is_active = status.get("active", 0) > 0
            if operation == "install":
                if is_complete:
                    addon_phase = AddonPhase.READY
                elif is_failed:
                    addon_phase = AddonPhase.FAILED
                elif is_active:
                    addon_phase = AddonPhase.INSTALLING
                else:
                    addon_phase = AddonPhase.PENDING
            else:
                if is_complete:
                    self.status.addons.pop(component, None)
                    return self
                if is_failed:
                    addon_phase = AddonPhase.FAILED
                elif is_active:
                    addon_phase = AddonPhase.UNINSTALLING
                else:
                    # The job has just launched - the phase stays the same
                    addon_phase = previous_phase
            self.status.addons[component] = AddonStatus(phase = addon_phase, revision = revision)
        return self

    def addon_job_deleted(self, obj):
        """
        Updates the status when an addon job is deleted.
        """
        # Uninstall jobs are completed at the same time that they are deleted
        # So we need to handle that case here
        labels = obj["metadata"]["labels"]
        component = labels["app.kubernetes.io/component"]
        operation = labels["capi.stackhpc.com/operation"]
        revision = int(labels["capi.stackhpc.com/revision"])
        if self._addon_is_latest_revision(component, revision) and operation == "uninstall":
            status = obj.get("status", {})
            conditions = status.get("conditions", [])
            complete = next((c for c in conditions if c["type"] == "Complete"), None)
            if complete and complete["status"] == "True":
                self.status.addons.pop(component, None)
        return self

    def remove_unknown_addons(self, install_jobs):
        """
        Given the current set of addon install jobs, remove any unknown addons from the status.
        """
        current = set(j.metadata.labels["app.kubernetes.io/component"] for j in install_jobs)
        known = set(self.status.addons.keys())
        for component in known - current:
            self.status.addons.pop(component)
        return self

    def _service_status(self, obj):
        """
        Returns the service status object for the given secret object.
        """
        annotations = obj["metadata"]["annotations"]
        # If no label is specified, derive one from the name
        if "azimuth.stackhpc.com/service-label" in annotations:
            label = annotations["azimuth.stackhpc.com/service-label"]
        else:
            name = obj["metadata"]["labels"]["azimuth.stackhpc.com/service-name"]
            label = " ".join(
                word.capitalize()
                for word in name.removesuffix("-proxy").split("-")
            )
        return ServiceStatus(
            fqdn = annotations["azimuth.stackhpc.com/zenith-fqdn"],
            label = label.strip(),
            icon_url = annotations.get("azimuth.stackhpc.com/service-icon-url"),
            description = annotations.get("azimuth.stackhpc.com/service-description")
        )

    def service_secret_updated(self, obj):
        """
        Updates the status when a service secret is updated.
        """
        name = obj["metadata"]["labels"]["azimuth.stackhpc.com/service-name"]
        self.status.services[name] = self._service_status(obj)
        return self

    def service_secret_deleted(self, obj):
        """
        Updates the status when a service secret is deleted.
        """
        name = obj["metadata"]["labels"]["azimuth.stackhpc.com/service-name"]
        self.status.services.pop(name, None)
        return self

    def remove_unknown_services(self, secrets):
        """
        Given the current set of service secrets, remove any unknown services from the status.
        """
        services = {}
        for obj in secrets:
            name = obj["metadata"]["labels"]["azimuth.stackhpc.com/service-name"]
            services[name] = self._service_status(obj)
        self.status.services = services
        return self
