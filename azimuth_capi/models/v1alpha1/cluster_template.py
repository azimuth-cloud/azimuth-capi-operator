import typing as t

from pydantic import Extra, Field, constr

from ..util import BaseModel


class ClusterTemplateValues(BaseModel):
    """
    The values to use when deploying the Helm chart.
    """
    class Config:
        extra = Extra.allow

    kubernetes_version: constr(min_length = 1) = Field(
        ...,
        description = "The Kubernetes version that will be deployed."
    )
    machine_image: constr(min_length = 1) = Field(
        ...,
        description = (
            "The name of the image to use for cluster machines. "
            "This is used when creating machines with ephemeral root disks."
        )
    )
    machine_image_id: constr(min_length = 1) = Field(
        ...,
        description = (
            "The ID of the image to use for cluster machines. "
            "This is used when creating machines with volumes as root disks."
        )
    )


class ClusterTemplateSpec(BaseModel):
    """
    The spec of an Azimuth cluster template.
    """
    label: constr(min_length = 1) = Field(
        ...,
        description = "The human-readable name of the template."
    )
    cloud_provider: t.Literal["openstack"] = Field(
        "openstack",
        description = "The name of the cloud provider for which this template applies."
    )
    target_cloud: t.Optional[str] = Field(
        None,
        description = (
            "The name of the target cloud for the template. "
            "If not given, the template is assumed to apply for all clouds with "
            "the specified provider."
        )
    )
    chart_version: constr(min_length = 1) = Field(
        ...,
        description = "The version of the CAPI Helm charts to use."
    )
    values: ClusterTemplateValues = Field(
        ...,
        description = "The values to use when deploying the Helm chart."
    )
    deprecated: bool = Field(
        False,
        description = "Indicates if this is a deprecated template."
    )


class ClusterTemplate(BaseModel):
    """
    An Azimuth cluster template.
    """
    spec: ClusterTemplateSpec
