import logging

from .models.v1alpha1 import (
    ClusterPhase,
    LeasePhase,
    NetworkingPhase,
    ControlPlanePhase,
    NodePhase,
    AddonPhase,
    NodeRole,
    NodeStatus,
    AddonStatus,
)


logger = logging.getLogger(__name__)


def _any_node_has_phase(cluster, *phases):
    """
    Returns true if any node has one of the given phases.
    """
    return any(node.phase in phases for node in cluster.status.nodes.values())


def _multiple_kubelet_versions(cluster, role):
    """
    Returns true if nodes with the given role have different kubelet versions,
    false otherwise.
    """
    versions = set(
        node.kubelet_version
        for node in cluster.status.nodes.values()
        if node.role == role
    )
    return len(versions) > 1


def _any_addon_has_phase(cluster, *phases):
    """
    Returns true if any addon has one of the given phases.
    """
    return any(addon.phase in phases for addon in cluster.status.addons.values())


def _reconcile_cluster_phase(cluster):
    """
    Sets the overall cluster phase based on the component phases.
    """
    # Only consider the lease when reconciling the cluster phase if one is set
    if cluster.spec.lease_name:
        if cluster.status.lease_phase in {
            LeasePhase.CREATING,
            LeasePhase.PENDING,
            LeasePhase.STARTING
        }:
            cluster.status.phase = ClusterPhase.PENDING
            return
        if cluster.status.lease_phase == LeasePhase.ERROR:
            cluster.status.phase = ClusterPhase.FAILED
            return
        if cluster.status.lease_phase in {
            LeasePhase.TERMINATING,
            LeasePhase.TERMINATED,
            LeasePhase.DELETING
        }:
            cluster.status.phase = ClusterPhase.UNHEALTHY
            return
        if cluster.status.lease_phase == LeasePhase.UNKNOWN:
            cluster.status.phase = ClusterPhase.PENDING
            return
    # At this point, either there is no lease or the lease phase is Active or Updating
    if cluster.status.networking_phase in {
        NetworkingPhase.PENDING,
        NetworkingPhase.PROVISIONING
    }:
        cluster.status.phase = ClusterPhase.RECONCILING
    elif cluster.status.networking_phase == NetworkingPhase.DELETING:
        cluster.status.phase = ClusterPhase.DELETING
    elif cluster.status.networking_phase == NetworkingPhase.FAILED:
        cluster.status.phase = ClusterPhase.FAILED
    elif cluster.status.networking_phase == NetworkingPhase.UNKNOWN:
        cluster.status.phase = ClusterPhase.PENDING
    # The networking phase is Provisioned
    elif cluster.status.control_plane_phase in {
        ControlPlanePhase.PENDING,
        ControlPlanePhase.SCALING_UP,
        ControlPlanePhase.SCALING_DOWN
    }:
        # If the control plane is scaling but there are control plane nodes with
        # different versions, that is still part of an upgrade
        if _multiple_kubelet_versions(cluster, NodeRole.CONTROL_PLANE):
            cluster.status.phase = ClusterPhase.UPGRADING
        else:
            cluster.status.phase = ClusterPhase.RECONCILING
    elif cluster.status.control_plane_phase == ControlPlanePhase.UPGRADING:
        cluster.status.phase = ClusterPhase.UPGRADING
    elif cluster.status.control_plane_phase == ControlPlanePhase.DELETING:
        cluster.status.phase = ClusterPhase.DELETING
    elif cluster.status.control_plane_phase == ControlPlanePhase.FAILED:
        cluster.status.phase = ClusterPhase.FAILED
    elif cluster.status.control_plane_phase == ControlPlanePhase.UNKNOWN:
        cluster.status.phase = ClusterPhase.PENDING
    # The control plane phase is Ready or Unhealthy
    # If there are workers with different versions, assume an upgrade is in progress
    elif _multiple_kubelet_versions(cluster, NodeRole.WORKER):
        cluster.status.phase = ClusterPhase.UPGRADING
    elif _any_node_has_phase(
        cluster,
        NodePhase.PENDING,
        NodePhase.PROVISIONING,
        NodePhase.DELETING,
        NodePhase.DELETED
    ):
        cluster.status.phase = ClusterPhase.RECONCILING
    # All nodes are either Ready, Unhealthy, Failed or Unknown
    elif _any_addon_has_phase(
        cluster,
        AddonPhase.PENDING,
        AddonPhase.PREPARING,
        AddonPhase.INSTALLING,
        AddonPhase.UPGRADING,
        AddonPhase.UNINSTALLING
    ):
        cluster.status.phase = ClusterPhase.RECONCILING
    # All addons are either Ready, Failed or Unknown
    # Now we know that there is no reconciliation happening, consider cluster health
    elif (
        cluster.status.control_plane_phase == ControlPlanePhase.UNHEALTHY or
        _any_node_has_phase(
            cluster,
            NodePhase.UNHEALTHY,
            NodePhase.FAILED,
            NodePhase.UNKNOWN
        ) or
        _any_addon_has_phase(
            cluster,
            AddonPhase.FAILED,
            AddonPhase.UNKNOWN
        )
    ):
        cluster.status.phase = ClusterPhase.UNHEALTHY
    else:
        cluster.status.phase = ClusterPhase.READY


def lease_updated(cluster, obj):
    """
    Updates the status when a lease is updated.
    """
    phase = obj.get("status", {}).get("phase", "Unknown")
    cluster.status.lease_phase = LeasePhase(phase)


def lease_deleted(cluster, obj):
    """
    Updates the status when a lease is deleted.
    """
    cluster.status.lease_phase = LeasePhase.UNKNOWN


def cluster_updated(cluster, obj):
    """
    Updates the status when a CAPI cluster is updated.
    """
    # Just set the networking phase
    phase = obj.get("status", {}).get("phase", "Unknown")
    cluster.status.networking_phase = NetworkingPhase(phase)


def cluster_deleted(cluster, obj):
    """
    Updates the status when a CAPI cluster is deleted.
    """
    cluster.status.networking_phase = NetworkingPhase.UNKNOWN


def cluster_absent(cluster):
    """
    Called when the CAPI cluster is missing on resume.
    """
    cluster.status.networking_phase = NetworkingPhase.UNKNOWN


def control_plane_updated(cluster, obj):
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
            logger.warn("UNKNOWN CONTROL PLANE REASON: %s", ready["reason"])
            next_phase = ControlPlanePhase.UNHEALTHY
    else:
        next_phase = ControlPlanePhase.PENDING
    cluster.status.control_plane_phase = next_phase
    # The Kubernetes version in the control plane object has a leading v that we don't want
    # The accurate version is in the status, but we use the spec if that is not set yet
    cluster.status.kubernetes_version = status.get("version", obj["spec"]["version"]).lstrip("v")


def control_plane_deleted(cluster, obj):
    """
    Updates the status when a CAPI control plane is deleted.
    """
    cluster.status.control_plane_phase = ControlPlanePhase.UNKNOWN
    # Also reset the Kubernetes version of the cluster, as it can no longer be determined
    cluster.status.kubernetes_version = None


def control_plane_absent(cluster):
    """
    Called when the control plane is missing on resume.
    """
    cluster.status.control_plane_phase = ControlPlanePhase.UNKNOWN
    # Also reset the Kubernetes version of the cluster, as it can no longer be determined
    cluster.status.kubernetes_version = None


def machine_updated(cluster, obj, infra_machine):
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
    cluster.status.nodes[obj["metadata"]["name"]] = NodeStatus(
        # The node role should be in the labels
        role = NodeRole(labels["capi.stackhpc.com/component"]),
        phase = node_phase,
        # This assumes an OpenStackMachine for now
        size = infra_machine.spec.flavor,
        ip = next(
            (
                a["address"]
                for a in status.get("addresses", [])
                if a["type"] == "InternalIP"
            ),
            None
        ),
        ips = [
            a["address"]
            for a in status.get("addresses", [])
            if a["type"] == "InternalIP"
        ],
        # Take the version from the spec, which should always be set
        kubelet_version = obj["spec"]["version"].lstrip("v"),
        # The node group will be in a label if applicable
        node_group = labels.get("capi.stackhpc.com/node-group"),
        # Use the timestamp from the metadata for the created time
        created = obj["metadata"]["creationTimestamp"]
    )


def machine_deleted(cluster, obj):
    """
    Updates the status when a CAPI machine is deleted.
    """
    # Just remove the node from the node set
    cluster.status.nodes.pop(obj["metadata"]["name"], None)


def remove_unknown_nodes(cluster, machines):
    """
    Given the current set of machines, remove any unknown nodes from the status.
    """
    current = set(m.metadata.name for m in machines)
    known = set(cluster.status.nodes.keys())
    for name in known - current:
        cluster.status.nodes.pop(name)


def kubeconfig_secret_updated(cluster, obj):
    """
    Updates the status when a kubeconfig secret is updated.
    """
    cluster.status.kubeconfig_secret_name = obj["metadata"]["name"]


def _flux_to_addon_status(flux_conditions):
    addon_phase = AddonPhase.UNKNOWN.value
    addon_revision = 0
    if len(flux_conditions) > 0:
        status = flux_conditions[0].get("status","False")
        type_status = flux_conditions[0].get("type","Ready")
        if type_status == "Ready":
            if status == "True":
                addon_phase = AddonPhase.DEPLOYED.value
            else:
                addon_phase = AddonPhase.FAILED.value
        else:
            addon_phase = AddonPhase.PENDING.value

        addon_revision = flux_conditions[0].get("observedGeneration",0)

    return AddonStatus(phase = addon_phase, revision = addon_revision)


def flux_updated(cluster, obj):
    """
    Updates the status when a Flux addon is updated.
    """
    component = obj["metadata"]["labels"]["capi.stackhpc.com/component"]
    status = obj.get("status", {})
    conditions = status.get("conditions", [])
    cluster.status.addons[component] = _flux_to_addon_status(conditions)


def addon_updated(cluster, obj):
    """
    Updates the status when an addon is updated.
    """
    component = obj["metadata"]["labels"]["capi.stackhpc.com/component"]
    status = obj.get("status", {})
    cluster.status.addons[component] = AddonStatus(
        phase = status.get("phase", AddonPhase.UNKNOWN.value),
        revision = status.get("revision", 0)
    )


def addon_deleted(cluster, obj):
    """
    Updates the status when an addon is deleted.
    """
    component = obj["metadata"]["labels"]["capi.stackhpc.com/component"]
    cluster.status.addons.pop(component, None)


def remove_unknown_addons(cluster, addons):
    """
    Given the current set of addons, remove any unknown addons from the status.
    """
    current = set(a["metadata"]["labels"]["capi.stackhpc.com/component"] for a in addons)
    known = set(cluster.status.addons.keys())
    for component in known - current:
        cluster.status.addons.pop(component)


def finalise(cluster):
    """
    Apply final derived elements to the status.
    """
    _reconcile_cluster_phase(cluster)
    cluster.status.node_count = len(cluster.status.nodes)
    cluster.status.addon_count = len(cluster.status.addons)
