from pydantic import Field

from configomatic import Configuration as BaseConfiguration, LoggingConfiguration


class Configuration(BaseConfiguration):
    """
    Top-level configuration model.
    """
    class Config:
        default_path = "/etc/azimuth/capi.yaml"
        path_env_var = "AZIMUTH_CAPI_CONFIG"
        env_prefix = "AZIMUTH_CAPI"

    #: The logging configuration
    logging: LoggingConfiguration = Field(default_factory = LoggingConfiguration)

    #: The API group of the cluster CRDs
    api_group: str = "azimuth.stackhpc.com"
    #: The repository containing the CAPI Helm charts
    capi_helm_repo: str = "https://stackhpc.github.io/capi-helm-charts"


settings = Configuration()
