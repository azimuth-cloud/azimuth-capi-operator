import typing as t

from pydantic import Extra, Field, constr

from ..util import BaseModel


class ClusterTemplateGlobalValues(BaseModel):
    """
    The global values used when deploying the Helm chart.
    """
    class Config:
        extra = Extra.allow

    kubernetes_version: constr(min_length = 1) = Field(
        ...,
        description = "The Kubernetes version that will be deployed."
    )


class ClusterTemplateValues(BaseModel):
    """
    The values to use when deploying the Helm chart.
    """
    class Config:
        fields = {
            "global_": "global",
        }
        extra = Extra.allow

    global_: ClusterTemplateGlobalValues = Field(
        ...,
        description = "The global values for the deployment."
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
    description: constr(min_length = 1) = Field(
        ...,
        description = (
            "Brief description of the capabilities of clusters deployed "
            "using the template."
        )
    )
    chart_name: constr(min_length = 1) = Field(
        "openstack-cluster",
        description = "The name of the CAPI Helm chart to use."
    )
    chart_version: constr(min_length = 1) = Field(
        ...,
        description = "The version of the specified CAPI Helm chart to use."
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
