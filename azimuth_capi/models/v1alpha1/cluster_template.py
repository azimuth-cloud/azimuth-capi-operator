import typing as t

from pydantic import Extra, Field, constr

from kube_custom_resource import CustomResource, Scope, schema


class ClusterTemplateValues(schema.BaseModel):
    """
    The values to use when deploying the Helm chart.
    """
    class Config:
        extra = Extra.allow

    kubernetes_version: constr(min_length = 1) = Field(
        ...,
        description = "The Kubernetes version that will be deployed."
    )
    machine_image_id: constr(min_length = 1) = Field(
        ...,
        description = "The ID of the image to use for cluster machines."
    )


class ClusterTemplateSpec(schema.BaseModel):
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
    tags: t.List[constr(min_length = 1)] = Field(
        default_factory = list,
        description = "Tags for the cluster."
    )
    values: ClusterTemplateValues = Field(
        ...,
        description = "The values to use when deploying the Helm chart."
    )


class ClusterTemplate(
    CustomResource,
    scope = Scope.CLUSTER,
    printer_columns = [
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
    ]
):
    """
    An Azimuth cluster template.
    """
    spec: ClusterTemplateSpec
