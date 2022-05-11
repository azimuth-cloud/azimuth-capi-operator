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


class CAPIHelmConfig(Section):
    """
    Configuration for the CAPI Helm chart used to deploy clusters.
    """
    #: The repository containing the CAPI Helm charts
    chart_repository: AnyHttpUrl = "https://stackhpc.github.io/capi-helm-charts"
    #: The name of the CAPI Helm chart to use to deploy clusters
    chart_name: constr(min_length = 1) = "openstack-cluster"
    #: The version of the CAPI Helm chart to use to deploy clusters
    chart_version: SemVerVersion = "0.1.0-dev.0.main.158"
    #: The default values to use for all clusters
    #: Values defined in templates take precedence
    default_values: t.Dict[str, t.Any] = Field(default_factory = dict)


class KubeappsConfig(Section):
    """
    Configuration for the Kubeapps installation on workload clusters.
    """
    #: The repository containing the kubeapps Helm chart
    chart_repository: AnyHttpUrl = "https://charts.bitnami.com/bitnami"
    #: The name of the kubeapps Helm chart
    chart_name: constr(min_length = 1) = "kubeapps"
    #: The version of the kubeapps Helm chart to use
    chart_version: SemVerVersion = "~7.7.4"
    #: The release namespace for kubeapps installations
    release_namespace: constr(min_length = 1) = "kubeapps"
    # The values to use for the release
    release_values: t.Dict[str, t.Any] = Field(default_factory = dict)


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
    chart_version: SemVerVersion = "0.1.0-dev.0.main.160"

    #: Defaults for use with the apiserver chart
    apiserver_defaults: t.Dict[str, t.Any] = Field(default_factory = dict)
    #: Defaults for use with the operator chart
    operator_defaults: t.Dict[str, t.Any] = Field(default_factory = dict)

    #: Icon URLs for built-in services
    kubernetes_dashboard_icon_url: AnyHttpUrl = "https://raw.githubusercontent.com/cncf/artwork/master/projects/kubernetes/icon/color/kubernetes-icon-color.png"
    monitoring_icon_url: AnyHttpUrl = "https://raw.githubusercontent.com/cncf/artwork/master/projects/prometheus/icon/color/prometheus-icon-color.png"
    kubeapps_icon_url: AnyHttpUrl = "https://user-images.githubusercontent.com/642657/153432175-b4aefccc-b94d-4373-b471-7afa02575a4b.png"

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
    #: The prefix to use for operator annotations
    annotation_prefix: str = "azimuth.stackhpc.com"

    #: The webhook configuration
    webhook: WebhookConfiguration = Field(default_factory = WebhookConfiguration)

    #: The CAPI Helm configuration
    capi_helm: CAPIHelmConfig = Field(default_factory = CAPIHelmConfig)

    #: Configuration for the Kubeapps release
    kubeapps: KubeappsConfig = Field(default_factory = KubeappsConfig)

    #: Configuration for Zenith support
    zenith: ZenithConfig = Field(default_factory = ZenithConfig)


settings = Configuration()
