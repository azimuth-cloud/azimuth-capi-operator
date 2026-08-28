import datetime as dt
import logging

from .config import settings
from .models.v1alpha1 import (
    AddonPhase,
    AddonStatus,
    ClusterPhase,
    ControlPlanePhase,
    LeasePhase,
    NetworkingPhase,
    NodePhase,
    NodeRole,
    NodeStatus,
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


def _update_control_plane_certificate_status(cluster):
    """
    Derive cluster level certificate information from control plane machines.
    """
    expiry_dates = [
        node.certificates_expiry_date
        for node in cluster.status.nodes.values()
        if (
            node.role == NodeRole.CONTROL_PLANE
            and node.certificates_expiry_date is not None
        )
    ]
    expiry_date = min(expiry_dates, default=None)
    cluster.status.control_plane_certificate_expiry_date = expiry_date

    rotation_days = cluster.status.control_plane_certificate_rotation_days
    if expiry_date is not None and rotation_days is not None:
        cluster.status.control_plane_certificate_rotation_date = (
            expiry_date - dt.timedelta(days=rotation_days)
        )
    else:
        cluster.status.control_plane_certificate_rotation_date = None


def _reconcile_cluster_phase(cluster):  # noqa: C901
    """
    Sets the overall cluster phase based on the component phases.
    New cluster.status.phase _appears_ to be an enum of Pending,Provisioning,
    Provisioned,Deleting,Failed,Unknown (taken from source NOT API spec)
    """
    temp_phase = ClusterPhase.UNKNOWN
    match cluster.status.new_phase:
        case "Pending":
            temp_phase = ClusterPhase.PENDING
        case "Provisioning":
            temp_phase = ClusterPhase.RECONCILING
        case "Provisioned":
            temp_phase = ClusterPhase.READY
        case "Deleting":
            temp_phase = ClusterPhase.DELETING
        case "Failed":
            temp_phase = ClusterPhase.FAILED
    cluster.status.phase = temp_phase

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


def cluster_status_check(cluster,obj):
    """
    Gathers overall cluster phase and all cluster conditions on a lease event,
    machine event or controlplane event
    """
    if(obj.get("kind","") == "Cluster"):
        obj_status = obj.get("status",{})
        cluster.status.observed_generation =(
            obj_status.get("observedGeneration",0)
        )
        cluster.status.v2beta2_conditions =(
            obj_status.get("v1beta2",{}).get("conditions",{})
        )

        cluster.status.new_phase = obj_status.get("phase","Unknown")

        _reconcile_cluster_phase(cluster)
    else:
        print("splendid, incorrect type passed:", obj.get("kind","Unknown"))

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
        (c for c in conditions if c["type"] == "ControlPlaneComponentsHealthy"), None
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
    # The Kubernetes version in the control plane object has a leading v that we don't
    # want
    # The accurate version is in the status, but we use the spec if that is not set yet
    cluster.status.kubernetes_version = status.get(
        "version", obj["spec"]["version"]
    ).lstrip("v")
    cluster.status.control_plane_certificate_rotation_days = (
        obj.get("spec", {}).get("rolloutBefore", {}).get("certificatesExpiryDays")
    )

def control_plane_deleted(cluster, obj):
    """
    Updates the status when a CAPI control plane is deleted.
    """
    cluster.status.control_plane_phase = ControlPlanePhase.UNKNOWN
    # Also reset the Kubernetes version of the cluster, as it can no longer be
    # determined
    cluster.status.kubernetes_version = None
    cluster.status.control_plane_certificate_rotation_days = None


def control_plane_absent(cluster):
    """
    Called when the control plane is missing on resume.
    """
    cluster.status.control_plane_phase = ControlPlanePhase.UNKNOWN
    # Also reset the Kubernetes version of the cluster, as it can no longer be
    # determined
    cluster.status.kubernetes_version = None
    cluster.status.control_plane_certificate_rotation_days = None


def machine_updated(cluster, obj, infra_machine):
    """
    Updates the status when a CAPI machine is updated.
    """
    labels = obj["metadata"]["labels"]
    status = obj.get("status", {})
    phase = status.get("phase", "Unknown")
    # We want to break the running phase down depending on node health
    # We also want to remove the Provisioned phase as a transition between Provisioning
    # and Ready, and just leave those nodes in the Provisioning phase
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
        role=NodeRole(labels["capi.stackhpc.com/component"]),
        phase=node_phase,
        # This assumes an OpenStackMachine for now
        size=infra_machine.spec.flavor,
        ip=next(
            (
                a["address"]
                for a in status.get("addresses", [])
                if a["type"] == "InternalIP"
            ),
            None,
        ),
        ips=[
            a["address"]
            for a in status.get("addresses", [])
            if a["type"] == "InternalIP"
        ],
        # Take the version from the spec, which should always be set
        kubelet_version=obj["spec"]["version"].lstrip("v"),
        # The node group will be in a label if applicable
        node_group=labels.get("capi.stackhpc.com/node-group"),
        # Use the timestamp from the metadata for the created time
        created=obj["metadata"]["creationTimestamp"],
        # CAPI derives this from the kube-apiserver certificate on control plane
        # machines. It is absent for workers and may be absent on older clusters.
        certificates_expiry_date=status.get("certificatesExpiryDate"),
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
        status = flux_conditions[0].get("status", "False")
        type_status = flux_conditions[0].get("type", "Ready")
        if type_status == "Ready":
            if status == "True":
                addon_phase = AddonPhase.DEPLOYED.value
            else:
                addon_phase = AddonPhase.FAILED.value
        else:
            addon_phase = AddonPhase.PENDING.value

        addon_revision = flux_conditions[0].get("observedGeneration", 0)

    return AddonStatus(phase=addon_phase, revision=addon_revision)


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
        phase=status.get("phase", AddonPhase.UNKNOWN.value),
        revision=status.get("revision", 0),
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
    current = set(
        a["metadata"]["labels"]["capi.stackhpc.com/component"] for a in addons
    )
    known = set(cluster.status.addons.keys())
    for component in known - current:
        cluster.status.addons.pop(component)


def finalise(cluster):
    """
    Apply final derived elements to the status.
    """
    _update_control_plane_certificate_status(cluster)
    _reconcile_cluster_phase(cluster)
    cluster.status.node_count = len(cluster.status.nodes)
    cluster.status.addon_count = len(cluster.status.addons)
