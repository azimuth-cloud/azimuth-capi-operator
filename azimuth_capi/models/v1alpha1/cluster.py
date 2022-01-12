import typing as t

from pydantic import Extra, Field, AnyHttpUrl, constr, conint

from ..util import BaseModel, Enum, Dict


class NodeGroupSpec(BaseModel):
    """
    The spec for a group of homogeneous nodes in the cluster.
    """
    name: constr(min_length = 1) = Field(
        ...,
        description = "The name of the node group."
    )
    machine_size: constr(min_length = 1) = Field(
        ...,
        description = "The name of the size to use for the machines in the node group."
    )
    count: conint(ge = 0) = Field(
        ...,
        description = "The number of nodes in the node group."
    )


class AddonsSpec(BaseModel):
    """
    The spec for the addons of the cluster.
    """
    cert_manager: bool = Field(
        False,
        description = "Indicates if cert-manager should be enabled."
    )
    ingress: bool = Field(
        False,
        description = "Indicates if ingress should be enabled."
    )
    monitoring: bool = Field(
        False,
        description = "Indicates if monitoring should be enabled."
    )


class ClusterSpec(BaseModel):
    """
    The spec for an Azimuth cluster.
    """
    label: constr(min_length = 1) = Field(
        ...,
        description = "The human-readable name of the cluster."
    )
    template_name: constr(min_length = 1) = Field(
        ...,
        description = "The name of the template to use."
    )
    cloud_credentials_secret_name: constr(min_length = 1) = Field(
        ...,
        description = "The name of the secret containing the cloud credentials."
    )
    autohealing: bool = Field(
        True,
        description = "Indicates if auto-healing should be enabled."
    )
    machine_root_volume_size: conint(ge = 0) = Field(
        0,
        description = (
            "The size of the root volume for machines in the cluster. "
            "If greater than zero a volume is provisioned for the root volume, "
            "otherwise the ephemeral volume of the machine size is used."
        )
    )
    control_plane_machine_size: constr(min_length = 1) = Field(
        ...,
        description = "The name of the size to use for control plane machines."
    )
    node_groups: t.List[NodeGroupSpec] = Field(
        default_factory = list,
        description = "The node groups for the cluster."
    )
    addons: AddonsSpec = Field(
        default_factory = AddonsSpec,
        description = "Describes the optional addons that should be enabled for the cluster."
    )


class ClusterPhase(str, Enum):
    """
    The overall phase of the cluster.
    """
    RECONCILING  = "Reconciling"
    UPGRADING    = "Upgrading"
    READY        = "Ready"
    DELETING     = "Deleting"
    UNHEALTHY    = "Unhealthy"
    FAILED       = "Failed"
    UNKNOWN      = "Unknown"


class NetworkingPhase(str, Enum):
    """
    The phase of the networking for the cluster.
    """
    PENDING      = "Pending"
    PROVISIONING = "Provisioning"
    PROVISIONED  = "Provisioned"
    DELETING     = "Deleting"
    FAILED       = "Failed"
    UNKNOWN      = "Unknown"


class ControlPlanePhase(str, Enum):
    """
    The phase of the control plane of the cluster.
    """
    PENDING      = "Pending"
    SCALING_UP   = "ScalingUp"
    SCALING_DOWN = "ScalingDown"
    UPGRADING    = "Upgrading"
    READY        = "Ready"
    DELETING     = "Deleting"
    UNHEALTHY    = "Unhealthy"
    FAILED       = "Failed"
    UNKNOWN      = "Unknown"


class NodePhase(str, Enum):
    """
    The phase of a node in the cluster.
    """
    PENDING      = "Pending"
    PROVISIONING = "Provisioning"
    PROVISIONED  = "Provisioned"
    READY        = "Ready"
    DELETING     = "Deleting"
    DELETED      = "Deleted"
    UNHEALTHY    = "Unhealthy"
    FAILED       = "Failed"
    UNKNOWN      = "Unknown"


class AddonsPhase(str, Enum):
    """
    The phase of the addons for the cluster.
    """
    PENDING   = "Pending"
    DEPLOYING = "Deploying"
    DEPLOYED  = "Deployed"
    FAILED    = "Failed"
    UNKNOWN   = "Unknown"


class NodeRole(str, Enum):
    """
    The role of a node.
    """
    CONTROL_PLANE = "control-plane"
    WORKER = "worker"
    UNKNOWN = "Unknown"


class NodeStatus(BaseModel):
    """
    The status of a node in the cluster.
    """
    role: NodeRole = Field(
        NodeRole.UNKNOWN.value,
        description = "The role of the node in the cluster."
    )
    phase: NodePhase = Field(
        NodePhase.UNKNOWN.value,
        description = "The phase of the node."
    )
    ip: t.Optional[str] = Field(
        None,
        description = "The internal IP address of the node."
    )
    kubelet_version: t.Optional[str] = Field(
        None,
        description = "The kubelet version of the node."
    )
    node_group: t.Optional[str] = Field(
        None,
        description = (
            "The node group that the node belongs to. "
            "Only used for worker nodes."
        )
    )


class ClusterStatus(BaseModel):
    """
    The status of the cluster.
    """
    class Config:
        extra = Extra.allow

    kubernetes_version: t.Optional[str] = Field(
        None,
        description = "The Kubernetes version of the cluster, if known."
    )
    kubeconfig_secret_name: t.Optional[str] = Field(
        None,
        description = "The name of the secret containing the kubeconfig file, if known."
    )
    phase: ClusterPhase = Field(
        ClusterPhase.UNKNOWN.value,
        description = "The overall phase of the cluster."
    )
    networking_phase: NetworkingPhase = Field(
        NetworkingPhase.UNKNOWN.value,
        description = "The phase of the networking."
    )
    control_plane_phase: ControlPlanePhase = Field(
        ControlPlanePhase.UNKNOWN.value,
        description = "The phase of the control plane."
    )
    addons_phase: AddonsPhase = Field(
        AddonsPhase.UNKNOWN.value,
        description = "The phase of the addons."
    )
    node_count: int = Field(
        0,
        description = "The number of nodes in the cluster."
    )
    nodes: Dict[str, NodeStatus] = Field(
        default_factory = dict,
        description = "The status of the nodes in cluster, indexed by node name."
    )


class Cluster(BaseModel):
    """
    An Azimuth cluster.
    """
    spec: ClusterSpec
    status: ClusterStatus = Field(default_factory = ClusterStatus)
