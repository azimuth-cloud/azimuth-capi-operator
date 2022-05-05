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
    machine_image_id: constr(min_length = 1) = Field(
        ...,
        description = "The ID of the image to use for cluster machines."
    )


class ClusterTemplateSpec(BaseModel):
    """
    The spec of an Azimuth cluster template.
    """
    label: constr(min_length = 1) = Field(
        ...,
        description = "The human-readable name of the template."
    )
    deprecated: bool = Field(
        False,
        description = "Indicates if this is a deprecated template."
    )
    description: constr(min_length = 1) = Field(
        ...,
        description = (
            "Brief description of the capabilities of clusters deployed "
            "using the template."
        )
    )
    values: ClusterTemplateValues = Field(
        ...,
        description = "The values to use when deploying the Helm chart."
    )


class ClusterTemplate(BaseModel):
    """
    An Azimuth cluster template.
    """
    spec: ClusterTemplateSpec
