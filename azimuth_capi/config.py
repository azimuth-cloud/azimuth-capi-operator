import typing as t

from pydantic import Field, AnyHttpUrl, conint, constr, root_validator

from configomatic import Configuration as BaseConfiguration, LoggingConfiguration


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

    @root_validator
    def validate_sshd_host_required(cls, values):
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
    api_group: str = "azimuth.stackhpc.com"
    #: The repository containing the CAPI Helm charts
    capi_helm_repo: str = "https://stackhpc.github.io/capi-helm-charts"

    #: The prefix to use for operator annotations
    annotation_prefix: str = "azimuth.stackhpc.com"

    #: Configuration for Zenith support
    zenith: ZenithConfig = Field(default_factory = ZenithConfig)


settings = Configuration()
