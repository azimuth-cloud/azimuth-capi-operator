import typing as t

from kube_custom_resource import CustomResource, Scope, schema
from pydantic import Field


class ClusterTemplateValues(schema.BaseModel, extra="allow"):
    """
    The values to use when deploying the Helm chart.
    """

    kubernetes_version: schema.constr(min_length=1) = Field(
        ..., description="The Kubernetes version that will be deployed."
    )
    machine_image_id: schema.constr(min_length=1) = Field(
        ..., description="The ID of the image to use for cluster machines."
    )


class ClusterTemplateSpec(schema.BaseModel):
    """
    The spec of an Azimuth cluster template.
    """

    label: schema.constr(min_length=1) = Field(
        ..., description="The human-readable name of the template."
    )
    deprecated: bool = Field(
        False, description="Indicates if this is a deprecated template."
    )
    description: schema.constr(min_length=1) = Field(
        ...,
        description=(
            "Brief description of the capabilities of clusters deployed "
            "using the template."
        ),
    )
    tags: list[schema.constr(min_length=1)] = Field(
        default_factory=list, description="Tags for the cluster."
    )
    values: ClusterTemplateValues = Field(
        ..., description="The values to use when deploying the Helm chart."
    )


class ClusterTemplate(
    CustomResource,
    scope=Scope.CLUSTER,
    printer_columns=[
        {
            "name": "Label",
            "type": "string",
            "jsonPath": ".spec.label",
        },
        {
            "name": "Kubernetes Version",
            "type": "string",
            "jsonPath": ".spec.values.kubernetesVersion",
        },
        {
            "name": "Deprecated",
            "type": "boolean",
            "jsonPath": ".spec.deprecated",
        },
    ],
):
    """
    An Azimuth cluster template.
    """

    spec: ClusterTemplateSpec
