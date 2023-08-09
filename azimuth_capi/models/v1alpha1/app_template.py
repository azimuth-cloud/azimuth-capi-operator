import datetime as dt
import typing as t

from pydantic import Extra, Field, AnyHttpUrl, constr

from kube_custom_resource import CustomResource, Scope, schema

from easysemver import SEMVER_VERSION_REGEX, SEMVER_RANGE_REGEX


#: Type for a SemVer version
SemVerVersion = constr(regex = SEMVER_VERSION_REGEX)
#: Type for a SemVer range
SemVerRange = constr(regex = SEMVER_RANGE_REGEX)


class AppTemplateChartSpec(schema.BaseModel):
    """
    The spec for a chart reference for a Kubernetes app template.
    """
    repo: AnyHttpUrl = Field(
        ...,
        description = "The Helm repository that the chart is in."
    )
    name: constr(regex = r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The name of the chart."
    )


class AppTemplateSpec(schema.BaseModel):
    """
    The spec for an Kubernetes app template.
    """
    chart: AppTemplateChartSpec = Field(
        ...,
        description = "The chart to use for the Kubernetes app."
    )
    label: str = Field(
        "",
        description = (
            "The human-readable label for the app template. "
            "If not given, this will be taken from the Chart.yaml of the chart."
        )
    )
    logo: str = Field(
        "",
        description = (
            "The URL of the logo for the app template. "
            "If not given, the URL from the Chart.yaml of the chart will be used."
        )
    )
    description: str = Field(
        "",
        description = (
            "A short description of the app template. "
            "If not given, the description from the Chart.yaml of the chart will be used."
        )
    )
    version_range: SemVerRange = Field(
        ">=0.0.0",
        description = (
            "The range of versions to make available. "
            "If not given, all non-prerelease versions will be made available. "
            "If given, it must be a comma-separated list of constraints, where each "
            "constraint is an operator followed by a version. "
            "The supported operators are ==, !=, >, >=, < and <=."
        )
    )
    keep_versions: schema.conint(gt = 0) = Field(
        5,
        description = "The number of versions to keep. Defaults to 5."
    )
    sync_frequency: schema.conint(gt = 0) = Field(
        86400,  # 24 hours
        description = (
            "The number of seconds to wait before synchronising the versions again. "
            "Defaults to 86400 (24 hours) if not given. "
            "Note that this is only a best effort, and how soon after the sync "
            "frequency an update occurs is determined by the operator configuration."
        )
    )
    default_values: schema.Dict[str, schema.Any] = Field(
        default_factory = dict,
        description = "Default values for deployments of the app, on top of the chart defaults."
    )


class AppTemplateVersion(schema.BaseModel):
    """
    The available versions for the app template.
    """
    name: SemVerVersion = Field(
        ...,
        description = "The name of the version."
    )
    values_schema: schema.Dict[str, schema.Any] = Field(
        default_factory = dict,
        description = "The schema to use to validate the values."
    )
    ui_schema: schema.Dict[str, schema.Any] = Field(
        default_factory = dict,
        description = "The schema to use when rendering the user interface."
    )


class AppTemplateStatus(schema.BaseModel):
    """
    The status of the app template.
    """
    class Config:
        extra = Extra.allow

    label: t.Optional[constr(min_length = 1)] = Field(
        None,
        description = "The human-readable label for the app template."
    )
    logo: t.Optional[constr(min_length = 1)] = Field(
        None,
        description = "The URL of the logo for the app template."
    )
    description: t.Optional[constr(min_length = 1)] = Field(
        None,
        description = "A short description of the app template."
    )
    versions: t.List[AppTemplateVersion] = Field(
        default_factory = list,
        description = "The available versions for the app template."
    )
    last_sync: t.Optional[dt.datetime] = Field(
        None,
        description = "The time that the last successful sync of versions took place."
    )


class AppTemplate(
    CustomResource,
    scope = Scope.CLUSTER,
    subresources = {"status": {}},
    printer_columns = [
        {
            "name": "Chart name",
            "type": "string",
            "jsonPath": ".spec.chart.name",
        },
        {
            "name": "Version range",
            "type": "string",
            "jsonPath": ".spec.versionRange",
        },
        {
            "name": "Keep versions",
            "type": "integer",
            "jsonPath": ".spec.keepVersions",
            "priority": 1,
        },
        {
            "name": "Sync freq.",
            "type": "integer",
            "jsonPath": ".spec.syncFrequency",
            "priority": 1,
        },
        {
            "name": "Label",
            "type": "string",
            "jsonPath": ".status.label",
        },
        {
            "name": "Last sync",
            "type": "date",
            "jsonPath": ".status.lastSync",
        },
    ]
):
    """
    An Kubernetes app template.
    """
    spec: AppTemplateSpec
    status: AppTemplateStatus = Field(default_factory = AppTemplateStatus)
