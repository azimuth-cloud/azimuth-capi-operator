import re
import typing as t

from pydantic import Field, AnyHttpUrl, conint, constr, root_validator

from configomatic import Configuration as BaseConfiguration, LoggingConfiguration


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


class CAPIHelmChartConfig(BaseConfiguration):
    """
    Configuration for the CAPI Helm chart used to deploy clusters.
    """
    #: The repository containing the CAPI Helm charts
    repository: AnyHttpUrl = "https://stackhpc.github.io/capi-helm-charts"
    #: The name of the CAPI Helm chart to use to deploy clusters
    name: constr(min_length = 1) = "openstack-cluster"
    #: The version of the CAPI Helm chart to use to deploy clusters
    version: SemVerVersion = "0.1.0-dev.0.main.123"


class ZenithConfig(BaseConfiguration):
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
    chart_version: SemVerVersion = "0.1.0-dev.0.main.139"

    #: Defaults for use with the apiserver chart
    apiserver_defaults: t.Dict[str, t.Any] = Field(default_factory = dict)
    #: Defaults for use with the proxy chart
    proxy_defaults: t.Dict[str, t.Any] = Field(default_factory = dict)

    #: Icon URLs for built-in services
    kubernetes_dashboard_icon_url: AnyHttpUrl = "https://raw.githubusercontent.com/cncf/artwork/master/projects/kubernetes/icon/color/kubernetes-icon-color.png"
    monitoring_icon_url: AnyHttpUrl = "https://raw.githubusercontent.com/cncf/artwork/master/projects/prometheus/icon/color/prometheus-icon-color.png"

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

    #: The CAPI Helm chart configuration
    capi_helm_chart: CAPIHelmChartConfig = Field(default_factory = CAPIHelmChartConfig)

    #: Configuration for Zenith support
    zenith: ZenithConfig = Field(default_factory = ZenithConfig)


settings = Configuration()
