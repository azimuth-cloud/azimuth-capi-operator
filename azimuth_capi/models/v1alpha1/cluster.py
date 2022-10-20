import datetime as dt
import typing as t

from pydantic import Extra, Field, AnyHttpUrl, constr, validator

from kube_custom_resource import CustomResource, schema


class NodeGroupSpec(schema.BaseModel):
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
    autoscale: bool = Field(
        False,
        description = "Whether the node group should autoscale or not."
    )
    count: t.Optional[schema.conint(ge = 0)] = Field(
        None,
        description = "The fixed number of nodes in the node group when not autoscaling."
    )
    min_count: t.Optional[schema.conint(ge = 1)] = Field(
        None,
        description = "The minimum number of nodes in the node group when autoscaling."
    )
    max_count: t.Optional[schema.conint(ge = 1)] = Field(
        None,
        description = "The maximum number of nodes in the node group when autoscaling."
    )

    @validator("count", always = True)
    def check_count_required(cls, v, values, **kwargs):
        if not values["autoscale"] and v is None:
            raise ValueError("must be given for a non-autoscaled node group")
        return v

    @validator("min_count", always = True)
    def check_min_count_required(cls, v, values, **kwargs):
        if values["autoscale"] and v is None:
            raise ValueError("must be given for an autoscaled node group")
        return v

    @validator("max_count", always = True)
    def check_max_count_required(cls, v, values, **kwargs):
        if values["autoscale"]:
            if v is None:
                raise ValueError("must be given for an autoscaled node group")
            min_count = values["min_count"]
            if min_count and v < min_count:
                raise ValueError("must be greater than or equal to the minimum count")
        return v


class AddonsSpec(schema.BaseModel):
    """
    The spec for the addons of the cluster.
    """
    dashboard: bool = Field(
        False,
        description = "Indicates if the Kubernetes dashboard should be enabled."
    )
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
    apps: bool = Field(
        False,
        description = "Indicates if Kubeapps should be enabled."
    )


class ClusterSpec(schema.BaseModel):
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


class ClusterPhase(str, schema.Enum):
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


class NetworkingPhase(str, schema.Enum):
    """
    The phase of the networking for the cluster.
    """
    PENDING      = "Pending"
    PROVISIONING = "Provisioning"
    PROVISIONED  = "Provisioned"
    DELETING     = "Deleting"
    FAILED       = "Failed"
    UNKNOWN      = "Unknown"


class ControlPlanePhase(str, schema.Enum):
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


class NodePhase(str, schema.Enum):
    """
    The phase of a node in the cluster.
    """
    PENDING      = "Pending"
    PROVISIONING = "Provisioning"
    READY        = "Ready"
    DELETING     = "Deleting"
    DELETED      = "Deleted"
    UNHEALTHY    = "Unhealthy"
    FAILED       = "Failed"
    UNKNOWN      = "Unknown"


class AddonPhase(str, schema.Enum):
    """
    The phase of an addon in the cluster.
    """
    PENDING      = "Pending"
    INSTALLING   = "Installing"
    READY        = "Ready"
    UNINSTALLING = "Uninstalling"
    FAILED       = "Failed"
    UNKNOWN      = "Unknown"


class NodeRole(str, schema.Enum):
    """
    The role of a node.
    """
    CONTROL_PLANE = "control-plane"
    WORKER = "worker"
    UNKNOWN = "Unknown"


class NodeStatus(schema.BaseModel):
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
    size: t.Optional[constr(min_length = 1)] = Field(
        None,
        description = "The name of the size of the machine."
    )
    ip: t.Optional[str] = Field(
        None,
        description = "The internal IP address of the node."
    )
    ips: t.List[str] = Field(
        default_factory = list,
        description = "The IP addresses of the node."
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
    created: t.Optional[dt.datetime] = Field(
        None,
        description = "The datetime at which the node was created."
    )


class AddonStatus(schema.BaseModel):
    """
    The status of an addon in the cluster.
    """
    phase: AddonPhase = Field(
        AddonPhase.UNKNOWN.value,
        description = "The phase of the addon."
    )
    revision: schema.conint(ge = 0) = Field(
        0,
        description = "The revision of the addon."
    )


class ServiceStatus(schema.BaseModel):
    """
    The status of a service in the cluster.
    """
    fqdn: constr(min_length = 1) = Field(
        ...,
        description = "The FQDN of the service."
    )
    label: constr(min_length = 1) = Field(
        ...,
        description = "A human-readable label for the service."
    )
    icon_url: t.Optional[AnyHttpUrl] = Field(
        None,
        description = "A URL to an icon for the service."
    )
    description: t.Optional[str] = Field(
        None,
        description = "A brief description of the service."
    )


class ClusterStatus(schema.BaseModel):
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
    node_count: int = Field(
        0,
        description = "The number of nodes in the cluster."
    )
    nodes: schema.Dict[str, NodeStatus] = Field(
        default_factory = dict,
        description = "The status of the nodes in cluster, indexed by node name."
    )
    addon_count: int = Field(
        0,
        description = "The number of addons for the cluster."
    )
    addons: schema.Dict[str, AddonStatus] = Field(
        default_factory = dict,
        description = "The status of the addons for the cluster, indexed by addon name."
    )
    services: schema.Dict[str, ServiceStatus] = Field(
        default_factory = dict,
        description = "The services for the cluster, indexed by service name."
    )


class Cluster(
    CustomResource,
    subresources = {"status": {}},
    printer_columns = [
        {
            "name": "Label",
            "type": "string",
            "jsonPath": ".spec.label",
        },
        {
            "name": "Template",
            "type": "string",
            "jsonPath": ".spec.templateName",
        },
        {
            "name": "Kubernetes Version",
            "type": "string",
            "jsonPath": ".status.kubernetesVersion",
        },
        {
            "name": "Phase",
            "type": "string",
            "jsonPath": ".status.phase",
        },
        {
            "name": "Control Plane",
            "type": "string",
            "jsonPath": ".status.controlPlanePhase",
            "priority": 1,
        },
        {
            "name": "Node Count",
            "type": "integer",
            "jsonPath": ".status.nodeCount",
        },
        {
            "name": "Addon Count",
            "type": "integer",
            "jsonPath": ".status.addonCount",
            "priority": 1,
        },
    ]
):
    """
    An Azimuth cluster.
    """
    spec: ClusterSpec
    status: ClusterStatus = Field(default_factory = ClusterStatus)
