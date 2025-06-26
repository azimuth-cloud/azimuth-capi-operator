import typing as t

from configomatic import (
    Configuration as BaseConfiguration,
)
from configomatic import (
    LoggingConfiguration,
    Section,
)
from easysemver import SEMVER_VERSION_REGEX
from pydantic import (
    AfterValidator,
    Field,
    FilePath,
    StringConstraints,
    TypeAdapter,
    ValidationInfo,
    conint,
    constr,
    field_validator,
    model_validator,
)
from pydantic import (
    AnyHttpUrl as PyAnyHttpUrl,
)

#: Type for a string that validates as a SemVer version
SemVerVersion = t.Annotated[str, StringConstraints(pattern=SEMVER_VERSION_REGEX)]

#: Type for a string that validates as a URL
AnyHttpUrl = t.Annotated[
    str, AfterValidator(lambda v: str(TypeAdapter(PyAnyHttpUrl).validate_python(v)))
]


class HelmClientConfiguration(Section):
    """
    Configuration for the Helm client.
    """

    #: The default timeout to use with Helm releases
    #: Can be an integer number of seconds or a duration string like 5m, 5h
    default_timeout: int | constr(min_length=1) = "1h"
    #: The executable to use
    #: By default, we assume Helm is on the PATH
    executable: constr(min_length=1) = "helm"
    #: The maximum number of revisions to retain in the history of releases
    history_max_revisions: int = 10
    #: Indicates whether to verify TLS when pulling charts
    insecure_skip_tls_verify: bool = False
    #: The directory to use for unpacking charts
    #: By default, the system temporary directory is used
    unpack_directory: str | None = None


class CAPIHelmConfig(Section):
    """
    Configuration for the CAPI Helm chart used to deploy clusters.
    """

    #: The Helm chart repo, name and version to use for the CAPI Helm charts
    #: By default, this points to a local chart that is baked into the Docker image
    chart_name: constr(min_length=1) = "/charts/openstack-cluster"
    chart_repository: AnyHttpUrl | None = None
    chart_version: SemVerVersion | None = None
    #: The default values to use for all clusters
    #: Values defined in templates take precedence
    default_values: dict[str, t.Any] = Field(default_factory=dict)
    flavor_specific_node_group_overrides: dict[str, dict[str, t.Any]] = Field(
        default_factory=dict,
        description=(
            "Overrides for node groups based on the flavor. "
            "The key is the flavor name regex, "
            "and the value is a dictionary of overrides."
        ),
    )


class ZenithConfig(Section):
    """
    Configuration for Zenith support.
    """

    #: The admin URL of the Zenith registrar
    #: This is given to the Zenith operator for a cluster
    registrar_admin_url: AnyHttpUrl | None = None
    #: The internal admin URL of the Zenith registrar
    #: By default, this is the same as the registrar_admin_url
    registrar_admin_url_internal: AnyHttpUrl | None = Field(None, validate_default=True)
    #: The host for the Zenith SSHD service
    sshd_host: constr(min_length=1) | None = None
    #: The port for the Zenith SSHD service
    sshd_port: conint(gt=0) = 22

    #: The Zenith chart repository, version and names
    #: By default, these point to local charts that are baked into the Docker image
    apiserver_chart_name: constr(min_length=1) = "/charts/zenith-apiserver"
    operator_chart_name: constr(min_length=1) = "/charts/zenith-operator"
    chart_repository: AnyHttpUrl | None = None
    chart_version: SemVerVersion | None = None

    #: Defaults for use with the apiserver chart
    apiserver_defaults: dict[str, t.Any] = Field(default_factory=dict)
    #: Defaults for use with the operator chart
    operator_defaults: dict[str, t.Any] = Field(default_factory=dict)

    #: Icon URLs for built-in services
    kubernetes_dashboard_icon_url: AnyHttpUrl = (
        "https://raw.githubusercontent.com"
        "/cncf/artwork/master/projects/kubernetes/icon/color/kubernetes-icon-color.png"
    )
    monitoring_icon_url: AnyHttpUrl = (
        "https://raw.githubusercontent.com"
        "/cncf/artwork/master/projects/prometheus/icon/color/prometheus-icon-color.png"
    )
    #: The API version to use when watching Zenith resources on target clusters
    api_version: constr(pattern=r"^[a-z0-9.-]+/[a-z0-9]+$") = (
        "zenith.stackhpc.com/v1alpha1"
    )

    @model_validator(mode="after")
    def validate_zenith_enabled(self):
        """
        Ensures that the SSHD host is set when the registrar URL is given.
        """
        if bool(self.registrar_admin_url) != bool(self.sshd_host):
            raise ValueError(
                "registrar_admin_url and sshd_host are both required to "
                "enable Zenith support"
            )
        return self

    @field_validator("registrar_admin_url_internal")
    @classmethod
    def default_registrar_admin_url_internal(cls, v, info: ValidationInfo):
        """
        Sets the default internal registrar admin URL.
        """
        return v or info.data.get("registrar_admin_url")

    @property
    def enabled(self):
        """
        Indicates if Zenith support is enabled.
        """
        return bool(self.registrar_admin_url)


class PolicyRule(Section):
    """
    Configuration for a rule in a Kubernetes role.
    """
    #: The API groups for the resources that the rule applies to
    api_groups: t.List[constr(min_length = 1)] = Field(default_factory = list)
    #: The resource names that the rule applies to
    resources: t.List[constr(min_length = 1)] = Field(default_factory = list)
    #: The resource names that the rule applies to
    resource_names: t.List[constr(min_length = 1)] = Field(default_factory = list)
    #: The non-resource URLs that the rule applies to
    non_resource_urls: t.List[constr(min_length = 1)] = Field(
        alias = "nonResourceURLs",
        default_factory = list
    )
    #: The list of verbs that the rule applies to
    verbs: t.List[constr(min_length = 1)] = Field(default_factory = list)


class DefaultUserNamespaceRoleConfig(Section):
    """
    Configuration for the default role and binding for OIDC users.
    """
    #: The name for the cluster role and the role binding
    name: constr(min_length = 1) = "oidc:default-users-namespaced"
    #: The namespaces for the role bindings
    namespaces: t.List[constr(min_length = 1)] = Field(default_factory = list)
    #: The rules for the cluster role
    rules: t.List[PolicyRule] = Field(default_factory = list)

class DefaultUserClusterRoleConfig(Section):
    """
    Configuration for the default role and binding for OIDC users.
    """
    #: The name for the cluster role and the role binding
    name: constr(min_length = 1) = "oidc:default-users-cluster"
    #: The rules for the cluster role
    rules: t.List[PolicyRule] = Field(default_factory = list)


class IdentityConfig(Section):
    """
    Configuration for the Azimuth identity support.
    """
    #: Indicates whether OIDC authentication should be enabled for clusters
    oidc_enabled: bool = False
    #: The API version to use for Azimuth identity resources
    api_version: constr(min_length = 1) = "identity.azimuth.stackhpc.com/v1alpha1"
    #: The template to use for cluster OIDC client IDs
    cluster_oidc_client_id_template: constr(min_length = 1) = "kube-{cluster_name}"
    #: The template to use for cluster platform names
    cluster_platform_name_template: constr(min_length=1) = "kube-{cluster_name}"
    #: The template to use for app platform names
    app_platform_name_template: constr(min_length = 1) = "kubeapp-{app_name}"
    #: The default namespace-scoped role bindings to give users of the cluster
    #: These role binding are applied to the platform users group for the realm and the
    #: managed group that is created for cluster users
    default_user_namespace_role: t.Optional[DefaultUserNamespaceRoleConfig] = None
    #: The default cluster-wide role bindings to give users of the cluster
    #: These role binding are applied to the platform users group for the realm and the
    #: managed group that is created for cluster users
    default_user_cluster_role: t.Optional[DefaultUserClusterRoleConfig] = None


class WebhookConfiguration(Section):
    """
    Configuration for the internal webhook server.
    """

    #: The port to run the webhook server on
    port: conint(ge=1000) = 8443
    #: Indicates whether kopf should manage the webhook configurations
    managed: bool = False
    #: The path to the TLS certificate to use
    certfile: FilePath | None = Field(None, validate_default=False)
    #: The path to the key for the TLS certificate
    keyfile: FilePath | None = Field(None, validate_default=False)
    #: The host for the webhook server (required for self-signed certificate generation)
    host: constr(min_length=1) | None = Field(None, validate_default=False)

    @field_validator("certfile")
    @classmethod
    def validate_certfile(cls, v, info: ValidationInfo):
        """
        Validate that certfile is specified when configs are not managed.
        """
        if not info.data.get("managed") and v is None:
            raise ValueError("required when webhook configurations are not managed")
        return v

    @field_validator("keyfile")
    @classmethod
    def validate_keyfile(cls, v, info: ValidationInfo):
        """
        Validate that keyfile is specified when certfile is present.
        """
        if info.data.get("certfile") is not None and v is None:
            raise ValueError("required when certfile is given")
        return v

    @field_validator("host")
    @classmethod
    def validate_host(cls, v, info: ValidationInfo):
        """
        Validate that host is specified when there is no certificate specified.
        """
        if info.data.get("certfile") is None and v is None:
            raise ValueError("required when certfile is not given")
        return v


class Configuration(
    BaseConfiguration,
    default_path="/etc/azimuth/capi-operator.yaml",
    path_env_var="AZIMUTH_CAPI_CONFIG",
    env_prefix="AZIMUTH_CAPI",
):
    """
    Top-level configuration model.
    """

    #: The logging configuration
    logging: LoggingConfiguration = Field(default_factory=LoggingConfiguration)

    #: The API group of the cluster CRDs
    api_group: constr(min_length=1) = "azimuth.stackhpc.com"
    #: A list of categories to place CRDs into
    crd_categories: list[constr(min_length=1)] = Field(
        default_factory=lambda: ["azimuth"]
    )

    #: The prefix to use for operator annotations
    annotation_prefix: str = "azimuth.stackhpc.com"

    #: The number of seconds to wait between timer executions
    timer_interval: conint(gt=0) = 60

    #: The number of minutes to wait befoore marking a cluster as unhealthy
    cluster_timeout_seconds: conint(gt=0) = 30 * 60

    #: The field manager name to use for server-side apply
    easykube_field_manager: constr(min_length=1) = "azimuth-capi-operator"

    #: The amount of time (seconds) before a watch is forcefully restarted
    watch_timeout: conint(gt=0) = 600

    #: The Helm client configuration
    helm_client: HelmClientConfiguration = Field(
        default_factory=HelmClientConfiguration
    )

    #: The webhook configuration
    webhook: WebhookConfiguration = Field(default_factory=WebhookConfiguration)

    #: The CAPI Helm configuration
    capi_helm: CAPIHelmConfig = Field(default_factory=CAPIHelmConfig)

    #: Configuration for Zenith support
    zenith: ZenithConfig = Field(default_factory=ZenithConfig)

    #: Configuration for Azimuth identity support
    identity: IdentityConfig = Field(default_factory=IdentityConfig)


settings = Configuration()
