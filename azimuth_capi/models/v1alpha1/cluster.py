import datetime as dt
import ipaddress

from kube_custom_resource import CustomResource, schema
from pydantic import Field, ValidationInfo, field_validator


class NodeGroupSpec(schema.BaseModel):
    """
    The spec for a group of homogeneous nodes in the cluster.
    """

    name: schema.constr(min_length=1) = Field(
        ..., description="The name of the node group."
    )
    machine_size: schema.constr(min_length=1) = Field(
        ...,
        description="The name of the size to use for the machines in the node group.",
    )
    autoscale: bool = Field(
        False, description="Whether the node group should autoscale or not."
    )
    count: schema.Optional[schema.conint(ge=0)] = Field(
        None,
        description=(
            "The fixed number of nodes in the node group when not autoscaling."
        ),
        validate_default=True,
    )
    min_count: schema.Optional[schema.conint(ge=1)] = Field(
        None,
        description="The minimum number of nodes in the node group when autoscaling.",
        validate_default=True,
    )
    max_count: schema.Optional[schema.conint(ge=1)] = Field(
        None,
        description="The maximum number of nodes in the node group when autoscaling.",
        validate_default=True,
    )

    @field_validator("count")
    @classmethod
    def check_count_required(cls, v, info: ValidationInfo):
        if not info.data["autoscale"] and v is None:
            raise ValueError("must be given for a non-autoscaled node group")
        return v

    @field_validator("min_count")
    @classmethod
    def check_min_count_required(cls, v, info: ValidationInfo):
        if info.data["autoscale"] and v is None:
            raise ValueError("must be given for an autoscaled node group")
        return v

    @field_validator("max_count")
    @classmethod
    def check_max_count_required(cls, v, info: ValidationInfo):
        if info.data["autoscale"]:
            if v is None:
                raise ValueError("must be given for an autoscaled node group")
            min_count = info.data.get("min_count")
            if min_count and v < min_count:
                raise ValueError("must be greater than or equal to the minimum count")
        return v


class AddonsSpec(schema.BaseModel):
    """
    The spec for the addons of the cluster.
    """

    dashboard: bool = Field(
        False, description="Indicates if the Kubernetes dashboard should be enabled."
    )
    ingress: bool = Field(False, description="Indicates if ingress should be enabled.")
    ingress_controller_load_balancer_ip: schema.Optional[ipaddress.IPv4Address] = Field(
        None,
        description="The IP address to use for the ingress controller load balancer.",
    )
    monitoring: bool = Field(
        False, description="Indicates if monitoring should be enabled."
    )
    monitoring_alertmanager_volume_size: schema.conint(gt=0) = Field(
        10, description="The size of the Alertmanager volume size in GB."
    )
    monitoring_prometheus_volume_size: schema.conint(gt=0) = Field(
        10, description="The size of the Prometheus volume size in GB."
    )
    monitoring_loki_volume_size: schema.conint(gt=0) = Field(
        10, description="The size of the Loki volume size in GB."
    )


class ClusterSpec(schema.BaseModel):
    """
    The spec for an Azimuth cluster.
    """

    label: schema.constr(min_length=1) = Field(
        ..., description="The human-readable name of the cluster."
    )
    template_name: schema.constr(min_length=1) = Field(
        ..., description="The name of the template to use."
    )
    cloud_credentials_secret_name: schema.constr(min_length=1) = Field(
        ..., description="The name of the secret containing the cloud credentials."
    )
    zenith_identity_realm_name: schema.Optional[schema.constr(min_length=1)] = Field(
        None,
        description=(
            "The name of the Azimuth identity realm to use for Zenith services. "
            "If not given then the first discovered realm in the namespace is used."
        ),
    )
    lease_name: schema.Optional[schema.constr(min_length=1)] = Field(
        None, description="The name of the lease to use for the cluster, if required."
    )
    paused: bool = Field(
        False, description="Indicates if reconciliation should be paused."
    )
    autohealing: bool = Field(
        True, description="Indicates if auto-healing should be enabled."
    )
    control_plane_machine_size: schema.constr(min_length=1) = Field(
        ..., description="The name of the size to use for control plane machines."
    )
    node_groups: list[NodeGroupSpec] = Field(
        default_factory=list, description="The node groups for the cluster."
    )
    addons: AddonsSpec = Field(
        default_factory=AddonsSpec,
        description=(
            "Describes the optional addons that should be enabled for the cluster."
        ),
    )
    created_by_username: schema.Optional[str] = Field(
        None,
        description="Username of user that created the cluster.",
    )
    created_by_user_id: schema.Optional[str] = Field(
        None,
        description="User id of user that created the cluster.",
    )
    updated_by_username: schema.Optional[str] = Field(
        None,
        description="Username of user that updated the cluster.",
    )
    updated_by_user_id: schema.Optional[str] = Field(
        None,
        description="User id of user that updated the cluster.",
    )


class ClusterPhase(str, schema.Enum):
    """
    The overall phase of the cluster.
    """

    PENDING = "Pending"
    RECONCILING = "Reconciling"
    UPGRADING = "Upgrading"
    READY = "Ready"
    DELETING = "Deleting"
    UNHEALTHY = "Unhealthy"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class LeasePhase(str, schema.Enum):
    """
    The phase of the lease for a cluster.
    """

    CREATING = "Creating"
    PENDING = "Pending"
    STARTING = "Starting"
    ACTIVE = "Active"
    UPDATING = "Updating"
    TERMINATING = "Terminating"
    TERMINATED = "Terminated"
    DELETING = "Deleting"
    ERROR = "Error"
    UNKNOWN = "Unknown"


class NetworkingPhase(str, schema.Enum):
    """
    The phase of the networking for the cluster.
    """

    PENDING = "Pending"
    PROVISIONING = "Provisioning"
    PROVISIONED = "Provisioned"
    DELETING = "Deleting"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class ControlPlanePhase(str, schema.Enum):
    """
    The phase of the control plane of the cluster.
    """

    PENDING = "Pending"
    SCALING_UP = "ScalingUp"
    SCALING_DOWN = "ScalingDown"
    UPGRADING = "Upgrading"
    READY = "Ready"
    DELETING = "Deleting"
    UNHEALTHY = "Unhealthy"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class NodePhase(str, schema.Enum):
    """
    The phase of a node in the cluster.
    """

    PENDING = "Pending"
    PROVISIONING = "Provisioning"
    READY = "Ready"
    DELETING = "Deleting"
    DELETED = "Deleted"
    UNHEALTHY = "Unhealthy"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class AddonPhase(str, schema.Enum):
    """
    The phase of an addon in the cluster.
    """

    UNKNOWN = "Unknown"
    PENDING = "Pending"
    PREPARING = "Preparing"
    DEPLOYED = "Deployed"
    FAILED = "Failed"
    INSTALLING = "Installing"
    UPGRADING = "Upgrading"
    UNINSTALLING = "Uninstalling"


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
        NodeRole.UNKNOWN.value, description="The role of the node in the cluster."
    )
    phase: NodePhase = Field(
        NodePhase.UNKNOWN.value, description="The phase of the node."
    )
    size: schema.Optional[schema.constr(min_length=1)] = Field(
        None, description="The name of the size of the machine."
    )
    ip: schema.Optional[str] = Field(
        None, description="The internal IP address of the node."
    )
    ips: list[str] = Field(
        default_factory=list, description="The IP addresses of the node."
    )
    kubelet_version: schema.Optional[str] = Field(
        None, description="The kubelet version of the node."
    )
    node_group: schema.Optional[str] = Field(
        None,
        description=(
            "The node group that the node belongs to. Only used for worker nodes."
        ),
    )
    created: schema.Optional[dt.datetime] = Field(
        None, description="The datetime at which the node was created."
    )


class AddonStatus(schema.BaseModel):
    """
    The status of an addon in the cluster.
    """

    phase: AddonPhase = Field(
        AddonPhase.UNKNOWN.value, description="The phase of the addon."
    )
    revision: schema.conint(ge=0) = Field(0, description="The revision of the addon.")


class ServiceStatus(schema.BaseModel):
    """
    The status of a service in the cluster.
    """

    subdomain: schema.constr(min_length=1) = Field(
        ..., description="The subdomain of the service."
    )
    fqdn: schema.constr(min_length=1) = Field(
        ..., description="The FQDN of the service."
    )
    label: schema.constr(min_length=1) = Field(
        ..., description="A human-readable label for the service."
    )
    icon_url: schema.Optional[schema.AnyHttpUrl] = Field(
        None, description="A URL to an icon for the service."
    )
    description: schema.Optional[str] = Field(
        None, description="A brief description of the service."
    )


class ClusterStatus(schema.BaseModel, extra="allow"):
    """
    The status of the cluster.
    """

    kubernetes_version: schema.Optional[str] = Field(
        None, description="The Kubernetes version of the cluster, if known."
    )
    kubeconfig_secret_name: schema.Optional[str] = Field(
        None,
        description="The name of the secret containing the kubeconfig file, if known.",
    )
    phase: ClusterPhase = Field(
        ClusterPhase.UNKNOWN.value, description="The overall phase of the cluster."
    )
    lease_phase: LeasePhase = Field(
        LeasePhase.UNKNOWN.value, description="The phase of the lease for the cluster."
    )
    networking_phase: NetworkingPhase = Field(
        NetworkingPhase.UNKNOWN.value, description="The phase of the networking."
    )
    control_plane_phase: ControlPlanePhase = Field(
        ControlPlanePhase.UNKNOWN.value, description="The phase of the control plane."
    )
    node_count: int = Field(0, description="The number of nodes in the cluster.")
    nodes: schema.Dict[str, NodeStatus] = Field(
        default_factory=dict,
        description="The status of the nodes in cluster, indexed by node name.",
    )
    addon_count: int = Field(0, description="The number of addons for the cluster.")
    addons: schema.Dict[str, AddonStatus] = Field(
        default_factory=dict,
        description="The status of the addons for the cluster, indexed by addon name.",
    )
    services: schema.Dict[str, ServiceStatus] = Field(
        default_factory=dict,
        description="The services for the cluster, indexed by service name.",
    )
    last_updated: schema.Optional[dt.datetime] = Field(
        default=None, description="Used to trigger the timeout of pending states"
    )


class Cluster(
    CustomResource,
    subresources={"status": {}},
    printer_columns=[
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
            "name": "Paused",
            "type": "boolean",
            "jsonPath": ".spec.paused",
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
    ],
):
    """
    An Azimuth cluster.
    """

    spec: ClusterSpec
    status: ClusterStatus = Field(default_factory=ClusterStatus)
