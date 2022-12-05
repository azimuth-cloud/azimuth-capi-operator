import re
import typing as t

from pydantic import Field, AnyHttpUrl, FilePath, conint, constr, root_validator, validator

from configomatic import Configuration as BaseConfiguration, Section, LoggingConfiguration


class SemVerVersion(str):
    """
    Type for a string that is a valid SemVer version.
    """
    REGEX = re.compile(r"^[0-9]+.[0-9]+.[0-9]+(-[a-zA-Z0-9.-]+)?(\+[a-zA-Z0-9.-]+)?$")

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not isinstance(v, str):
            raise TypeError('string required')
        if cls.REGEX.fullmatch(v) is None:
            raise ValueError('invalid semver format')
        return cls(v)

    def __repr__(self):
        return f'{self.__class__.__name__}({super().__repr__()})'


class HelmClientConfiguration(Section):
    """
    Configuration for the Helm client.
    """
    #: The default timeout to use with Helm releases
    #: Can be an integer number of seconds or a duration string like 5m, 5h
    default_timeout: t.Union[int, constr(min_length = 1)] = "1h"
    #: The executable to use
    #: By default, we assume Helm is on the PATH
    executable: constr(min_length = 1) = "helm"
    #: The maximum number of revisions to retain in the history of releases
    history_max_revisions: int = 10
    #: Indicates whether to verify TLS when pulling charts
    insecure_skip_tls_verify: bool = False
    #: The directory to use for unpacking charts
    #: By default, the system temporary directory is used
    unpack_directory: t.Optional[str] = None


class CAPIHelmConfig(Section):
    """
    Configuration for the CAPI Helm chart used to deploy clusters.
    """
    #: The repository containing the CAPI Helm charts
    chart_repository: AnyHttpUrl = "https://stackhpc.github.io/capi-helm-charts"
    #: The name of the CAPI Helm chart to use to deploy clusters
    chart_name: constr(min_length = 1) = "openstack-cluster"
    #: The version of the CAPI Helm chart to use to deploy clusters
    chart_version: SemVerVersion = "0.1.1-dev.0.main.2"
    #: The default values to use for all clusters
    #: Values defined in templates take precedence
    default_values: t.Dict[str, t.Any] = Field(default_factory = dict)


class ZenithConfig(Section):
    """
    Configuration for Zenith support.
    """
    #: The admin URL of the Zenith registrar
    registrar_admin_url: t.Optional[AnyHttpUrl] = None
    #: The host for the Zenith SSHD service
    sshd_host: t.Optional[constr(min_length = 1)] = None
    #: The port for the Zenith SSHD service
    sshd_port: conint(gt = 0) = 22

    #: The repository for the Zenith charts
    chart_repository: AnyHttpUrl = "https://stackhpc.github.io/zenith"
    #: The version of the charts to use
    #: When changing this, be aware that the operator may depend on the layout of
    #: the Helm values at a particular version
    chart_version: SemVerVersion = "0.1.0-dev.0.feature-oidc-callout.183"

    #: Defaults for use with the apiserver chart
    apiserver_defaults: t.Dict[str, t.Any] = Field(default_factory = dict)
    #: Defaults for use with the operator chart
    operator_defaults: t.Dict[str, t.Any] = Field(default_factory = dict)

    #: Icon URLs for built-in services
    kubernetes_dashboard_icon_url: AnyHttpUrl = "https://raw.githubusercontent.com/cncf/artwork/master/projects/kubernetes/icon/color/kubernetes-icon-color.png"
    monitoring_icon_url: AnyHttpUrl = "https://raw.githubusercontent.com/cncf/artwork/master/projects/prometheus/icon/color/prometheus-icon-color.png"

    #: The API version to use when watching Zenith resources on target clusters
    api_version: constr(regex = r"^[a-z0-9.-]+/[a-z0-9]+$") = "zenith.stackhpc.com/v1alpha1"

    @root_validator
    def validate_zenith_enabled(cls, values):
        """
        Ensures that the SSHD host is set when the registrar URL is given.
        """
        if bool(values.get("registrar_admin_url")) != bool(values.get("sshd_host")):
            raise ValueError(
                "registrar_admin_url and sshd_host are both required to "
                "enable Zenith support"
            )
        return values

    @property
    def enabled(self):
        """
        Indicates if Zenith support is enabled.
        """
        return bool(self.registrar_admin_url)


class WebhookConfiguration(Section):
    """
    Configuration for the internal webhook server.
    """
    #: The port to run the webhook server on
    port: conint(ge = 1000) = 8443
    #: Indicates whether kopf should manage the webhook configurations
    managed: bool = False
    #: The path to the TLS certificate to use
    certfile: t.Optional[FilePath] = None
    #: The path to the key for the TLS certificate
    keyfile: t.Optional[FilePath] = None
    #: The host for the webhook server (required for self-signed certificate generation)
    host: t.Optional[constr(min_length = 1)] = None

    @validator("certfile", always = True)
    def validate_certfile(cls, v, values, **kwargs):
        """
        Validate that certfile is specified when configs are not managed.
        """
        if "managed" in values and not values["managed"] and v is None:
            raise ValueError("required when webhook configurations are not managed")
        return v

    @validator("keyfile", always = True)
    def validate_keyfile(cls, v, values, **kwargs):
        """
        Validate that keyfile is specified when certfile is present.
        """
        if "certfile" in values and values["certfile"] is not None and v is None:
            raise ValueError("required when certfile is given")
        return v

    @validator("host", always = True)
    def validate_host(cls, v, values, **kwargs):
        """
        Validate that host is specified when there is no certificate specified.
        """
        if values.get("certfile") is None and v is None:
            raise ValueError("required when certfile is not given")
        return v


class IdentityConfig(Section):
    """
    Configuration for the Azimuth identity support.
    """
    #: The API version to use for Azimuth identity resources.
    api_version: constr(min_length = 1) = "identity.azimuth.stackhpc.com/v1alpha1"
    #: The expiration for client registration tokens issued to Zenith operator instances, in seconds
    #: Defaults to 10 years so that, for all reasonable clusters, the token never expires
    client_registration_token_expiration: conint(gt = 0) = 315360000
    #: The number of clients that client registration tokens issued to Zenith operator
    #: instances are permitted to create
    #: Defaults to one million clients so that, for all reasonable clusters, the operator
    #: is able to create as many clients as are required
    client_registration_token_client_count: conint(gt = 0) = 1000000


class Configuration(BaseConfiguration):
    """
    Top-level configuration model.
    """
    class Config:
        default_path = "/etc/azimuth/capi-operator.yaml"
        path_env_var = "AZIMUTH_CAPI_CONFIG"
        env_prefix = "AZIMUTH_CAPI"

    #: The logging configuration
    logging: LoggingConfiguration = Field(default_factory = LoggingConfiguration)

    #: The API group of the cluster CRDs
    api_group: constr(min_length = 1) = "azimuth.stackhpc.com"
    #: A list of categories to place CRDs into
    crd_categories: t.List[constr(min_length = 1)] = Field(
        default_factory = lambda: ["azimuth"]
    )

    #: The prefix to use for operator annotations
    annotation_prefix: str = "azimuth.stackhpc.com"

    #: The number of seconds to wait between timer executions
    timer_interval: conint(gt = 0) = 60

    #: The field manager name to use for server-side apply
    easykube_field_manager: constr(min_length = 1) = "azimuth-capi-operator"

    #: The Helm client configuration
    helm_client: HelmClientConfiguration = Field(default_factory = HelmClientConfiguration)

    #: The webhook configuration
    webhook: WebhookConfiguration = Field(default_factory = WebhookConfiguration)

    #: The CAPI Helm configuration
    capi_helm: CAPIHelmConfig = Field(default_factory = CAPIHelmConfig)

    #: Configuration for Zenith support
    zenith: ZenithConfig = Field(default_factory = ZenithConfig)

    #: Configuration for Azimuth identity support
    identity: IdentityConfig = Field(default_factory = IdentityConfig)


settings = Configuration()
