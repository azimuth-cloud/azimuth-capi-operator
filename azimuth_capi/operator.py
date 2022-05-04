import asyncio
import functools
import logging

import pydantic
from pydantic.json import pydantic_encoder

import kopf

from easykube import (
    Configuration,
    ApiError,
    ResourceSpec,
    PRESENT,
    resources as k8s
)

from . import capi_resources as capi
from .builder import ClusterStatusBuilder
from .config import settings
from .helm import Chart, Release
from .models import v1alpha1 as api
from .template import default_loader
from .utils import mergeconcat
from .zenith import zenith_values


logger = logging.getLogger(__name__)


# Get the easykube configuration from the environment
ekconfig = Configuration.from_environment(json_encoder = pydantic_encoder)


@kopf.on.startup()
def apply_settings(**kwargs):
    """
    Apply kopf settings.
    """
    kopf_settings = kwargs["settings"]
    kopf_settings.persistence.finalizer = f"{settings.annotation_prefix}/finalizer"
    kopf_settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix = settings.annotation_prefix
    )
    kopf_settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix = settings.annotation_prefix,
        key = "last-handled-configuration",
    )
    kopf_settings.admission.server = kopf.WebhookServer(
        addr = "0.0.0.0",
        port = settings.webhook.port,
        host = settings.webhook.host,
        certfile = settings.webhook.certfile,
        pkeyfile = settings.webhook.keyfile
    )
    if settings.webhook.managed:
        kopf_settings.admission.managed = f"webhook.{settings.api_group}"


# Load the CRDs and create easykube resource specs for them
cluster_template_crd = default_loader.load("crd-clustertemplates.yaml")
cluster_crd = default_loader.load("crd-clusters.yaml")

AzimuthClusterTemplate = ResourceSpec.from_crd(cluster_template_crd)
AzimuthCluster = ResourceSpec.from_crd(cluster_crd)
# Resource spec for the status subresource
AzimuthClusterStatus = ResourceSpec(
    AzimuthCluster.api_version,
    f"{AzimuthCluster.name}/status",
    AzimuthCluster.kind,
    AzimuthCluster.namespaced
)


def on(event, *args, **kwargs):
    """
    Wrapper for kopf decorators that ensures an easykube client is made available
    to the wrapped function, and converts easykube API errors to kopf temporary errors.
    """
    kopf_decorator = getattr(kopf.on, event)
    def decorator(fn):
        @kopf_decorator(*args, **kwargs)
        @functools.wraps(fn)
        async def wrapper(client = None, **inner):
            # Allow for on to be nested
            if client:
                return await fn(client = client, **inner)
            else:
                async with ekconfig.async_client() as client:
                    try:
                        return await fn(client = client, **inner)
                    except ApiError as exc:
                        raise kopf.TemporaryError(str(exc), delay = 1)
        return wrapper
    return decorator


@on("startup")
async def register_crds(client, **kwargs):
    """
    Ensures that the CRDs are registered when the operator is started.
    """
    await asyncio.gather(
        client.apply_object(cluster_template_crd),
        client.apply_object(cluster_crd)
    )


@on(
    "validate",
    AzimuthClusterTemplate.api_version,
    AzimuthClusterTemplate.name,
    id = "validate-cluster-template"
)
async def validate_cluster_template(client, name, spec, operation, **kwargs):
    """
    Validates cluster template objects.
    """
    if operation in {"CREATE", "UPDATE"}:
        try:
            spec = api.ClusterTemplateSpec.parse_obj(spec)
        except pydantic.ValidationError as exc:
            raise kopf.AdmissionError(str(exc))
        # The template values are immutable, so test for that
        try:
            existing = await AzimuthClusterTemplate(client).fetch(name)
        except ApiError as exc:
            if exc.response.status_code != 404:
                raise
        else:
            if existing.spec["values"] != spec.values.dict(by_alias = True):
                raise kopf.AdmissionError("field 'values' cannot be changed")
    elif operation == "DELETE":
        clusters = AzimuthCluster(client).list(
            labels = { f"{settings.api_group}/cluster-template": name },
            all_namespaces = True
        )
        try:
            _ = await clusters.__anext__()
        except StopAsyncIteration:
            pass  # In this case, the delete is permitted
        else:
            raise kopf.AdmissionError("template is in use by at least one cluster")


@on(
    "validate",
    AzimuthCluster.api_version,
    AzimuthCluster.name,
    id = "validate-cluster"
)
async def validate_cluster(client, name, namespace, spec, operation, **kwargs):
    """
    Validates cluster objects.
    """
    if operation not in {"CREATE", "UPDATE"}:
        return
    try:
        spec = api.ClusterSpec.parse_obj(spec)
    except pydantic.ValidationError as exc:
        raise kopf.AdmissionError(str(exc))
    # The credentials secret must exist
    try:
        _ = await k8s.Secret(client).fetch(
            spec.cloud_credentials_secret_name,
            namespace = namespace
        )
    except ApiError as exc:
        if exc.response.status_code == 404:
            raise kopf.AdmissionError("specified cloud credentials secret does not exist")
        else:
            raise
    # The specified template must exist
    try:
        template = await AzimuthClusterTemplate(client).fetch(spec.template_name)
    except ApiError as exc:
        if exc.response.status_code == 404:
            raise kopf.AdmissionError("specified cluster template does not exist")
        else:
            raise
    # If the template is being changed (including on create), it must not be deprecated
    if template.spec.deprecated:
        try:
            existing = await AzimuthCluster(client).fetch(name, namespace = namespace)
        except ApiError as exc:
            if exc.response.status_code == 404:
                raise kopf.AdmissionError("specified cluster template is deprecated")
            else:
                raise
        else:
            if existing.spec["templateName"] != template.metadata.name:
                raise kopf.AdmissionError("specified cluster template is deprecated")


@on("create", AzimuthCluster.api_version, AzimuthCluster.name)
@on("update", AzimuthCluster.api_version, AzimuthCluster.name, field = "spec")
async def on_cluster_create(client, name, namespace, body, spec, patch, **kwargs):
    """
    Executes when a new cluster is created or the spec of an existing cluster is updated.
    """
    spec = api.ClusterSpec.parse_obj(spec)
    # First, make sure that the secret exists
    secret = await k8s.Secret(client).fetch(
        spec.cloud_credentials_secret_name,
        namespace = namespace
    )
    # Then fetch the template
    template = api.ClusterTemplate.parse_obj(
        await AzimuthClusterTemplate(client).fetch(spec.template_name)
    )
    chart = Chart(
        repository = settings.capi_helm.chart_repository,
        name = settings.capi_helm.chart_name,
        version = settings.capi_helm.chart_version
    )
    # Generate the Helm values for the release
    helm_values = mergeconcat(
        settings.capi_helm.default_values,
        template.spec.values.dict(by_alias = True),
        default_loader.load("cluster-values.yaml", spec = spec, settings = settings)
    )
    if settings.zenith.enabled:
        helm_values = mergeconcat(helm_values, await zenith_values(client, body, secret))
    # Use Helm to install or upgrade the release
    await Release(name, namespace).install_or_upgrade(chart, helm_values)
    # Patch the labels to include the cluster template
    # This is used by the admission webhook to search for clusters using a template in
    # order to prevent deletion of cluster templates that are in use
    labels = patch.setdefault("metadata", {}).setdefault("labels", {})
    labels[f"{settings.api_group}/cluster-template"] = spec.template_name


async def ensure_finalizer(resource, name, namespace, finalizer):
    """
    Ensures that a finalizer is present on an object.
    """
    obj = await resource.fetch(name, namespace = namespace)
    finalizers = obj.metadata.get("finalizers", [])
    if finalizer not in finalizers:
        _ = await resource.patch(
            name,
            {
                "metadata": {
                    "finalizers": list(finalizers) + [finalizer],
                },
            },
            namespace = namespace
        )


async def remove_finalizer(resource, name, namespace, finalizer):
    """
    Ensures that a finalizer is not present on an object.
    """
    obj = await resource.fetch(name, namespace = namespace)
    finalizers = obj.metadata.get("finalizers", [])
    if finalizer in finalizers:
        _ = await resource.patch(
            name,
            {
                "metadata": {
                    "finalizers": [f for f in finalizers if f != finalizer],
                },
            },
            namespace = namespace
        )


@on("delete", AzimuthCluster.api_version, AzimuthCluster.name)
async def on_cluster_delete(client, name, namespace, **kwargs):
    """
    Executes whenever a cluster is deleted.
    """
    # Due to a bug in the CAPO operator, we must ensure all openstackmachines are deleted
    # before the openstackcluster is deleted
    # See https://github.com/kubernetes-sigs/cluster-api-provider-openstack/issues/1143
    # We do this by setting an additional finalizer on the infra cluster and removing it
    # once all the machines are gone
    finalizer = f"{settings.annotation_prefix}/finalizer"
    try:
        cluster = await capi.Cluster(client).fetch(name, namespace = namespace)
    except ApiError as exc:
        if exc.status_code == 404:
            infra_ref = None
            infra_resource = None
        else:
            raise
    else:
        infra_ref = cluster.spec["infrastructureRef"]
        infra_resource = await client.api(infra_ref["apiVersion"]).resource(infra_ref["kind"])
    if infra_resource:
        try:
            infra_cluster = await infra_resource.fetch(
                infra_ref["name"],
                namespace = infra_ref["namespace"]
            )
        except ApiError as exc:
            if exc.status_code != 404:
                raise
        else:
            finalizers = infra_cluster.metadata.get("finalizers", [])
            if finalizer not in finalizers:
                _ = await infra_resource.patch(
                    infra_ref["name"],
                    {
                        "metadata": {
                            "finalizers": list(finalizers) + [finalizer],
                        },
                    },
                    namespace = infra_ref["namespace"]
                )
    # If the corresponding Helm release exists, delete it
    release = Release(name, namespace)
    exists = await release.exists()
    if exists:
        await release.delete()
    # Wait for there to be no machines for the cluster
    # Once there are no machines, remove the finalizer from the infra cluster
    if infra_resource:
        # We do this by watching the Azimuth cluster object instead of listing machines
        az_cluster, events = await AzimuthCluster(client).watch_one(name, namespace = namespace)
        if az_cluster and len(az_cluster.get("status", {}).get("nodes", {})) > 0:
            async for event in events:
                if (
                    event["type"] == "DELETED" or
                    (
                        event["type"] == "MODIFIED" and
                        len(event["object"].get("status", {}).get("nodes", {})) < 1
                    )
                ):
                    break
        try:
            infra_cluster = await infra_resource.fetch(
                infra_ref["name"],
                namespace = infra_ref["namespace"]
            )
        except ApiError as exc:
            if exc.status_code != 404:
                raise
        else:
            finalizers = infra_cluster.metadata.get("finalizers", [])
            if finalizer in finalizers:
                _ = await infra_resource.patch(
                    infra_ref["name"],
                    {
                        "metadata": {
                            "finalizers": [f for f in finalizers if f != finalizer],
                        },
                    },
                    namespace = infra_ref["namespace"]
                )
    # Wait until the associated CAPI cluster no longer exists
    cluster, events = await capi.Cluster(client).watch_one(name, namespace = namespace)
    if cluster:
        async for event in events:
            if event["type"] == "DELETED":
                break


@on("resume", AzimuthCluster.api_version, AzimuthCluster.name)
async def on_cluster_resume(client, name, namespace, body, status, **kwargs):
    """
    Executes for each cluster when the operator is resumed.
    """
    # When we are resumed, the managed event handlers will be called with all the CAPI
    # objects that exist
    # However if CAPI objects have been deleted while the operator was down, we will
    # not receive delete events for those
    # So on resume we remove any items in the status that no longer exist
    builder = ClusterStatusBuilder(status)
    labels = { "capi.stackhpc.com/cluster": name }
    builder.remove_unknown_nodes([
        machine
        async for machine in capi.Machine(client).list(
            labels = labels,
            namespace = namespace
        )
    ])
    try:
        kcps = capi.KubeadmControlPlane(client).list(labels = labels, namespace = namespace)
        _ = await kcps.__anext__()
    except StopAsyncIteration:
        builder.control_plane_absent()
    try:
        clusters = capi.Cluster(client).list(labels = labels, namespace = namespace)
        _ = await clusters.__anext__()
    except StopAsyncIteration:
        builder.cluster_absent()
    # The addon jobs need different labels
    # Each addon that is deployed will have an install job
    builder.remove_unknown_addons([
        job
        async for job in k8s.Job(client).list(
            namespace = namespace,
            labels = {
                "app.kubernetes.io/name": "addons",
                "app.kubernetes.io/instance": name,
                "capi.stackhpc.com/operation": "install",
            }
        )
    ])
    # Also account for services that have been removed
    builder.remove_unknown_services([
        secret
        async for secret in k8s.Secret(client).list(
            namespace = namespace,
            labels = {
                "capi.stackhpc.com/cluster": name,
                "azimuth.stackhpc.com/service-name": PRESENT,
            }
        )
    ])
    changed, next_status = builder.build()
    if changed:
        await AzimuthClusterStatus(client).replace(
            name,
            dict(body, status = next_status),
            namespace = namespace
        )


def on_managed_object_event(*args, cluster_label = "capi.stackhpc.com/cluster", **kwargs):
    """
    Decorator that registers a function as updating the Azimuth cluster state in response
    to a CAPI resource changing.
    """
    # Limit the query to objects that have a cluster label
    kwargs.setdefault("labels", {}).update({ cluster_label: kopf.PRESENT })
    def decorator(fn):
        @on("event", *args, **kwargs)
        @functools.wraps(fn)
        async def wrapper(**inner):
            # Retry the fetch and updating of the state until it succeeds without conflict
            # kopf retry logic does not apply to events
            while True:
                try:
                    # Fetch the current state of the associated Azimuth cluster and pass it to the
                    # decorated function as the 'parent' keyword argument
                    cluster = await AzimuthCluster(inner["client"]).fetch(
                        inner["labels"][cluster_label],
                        namespace = inner["namespace"]
                    )
                    # Initialise a builder object for the next status
                    builder = ClusterStatusBuilder(cluster.get("status", {}))
                    # Run the update function with the builder
                    await fn(parent = cluster, builder = builder, **inner)
                    changed, next_status = builder.build()
                    if changed:
                        # The cluster CRD uses a status subresource, so we have to specifically
                        # use that in order to replace the status
                        await AzimuthClusterStatus(inner["client"]).replace(
                            cluster.metadata.name,
                            dict(cluster, status = next_status),
                            namespace = cluster.metadata.namespace
                        )
                except ApiError as exc:
                    # On a 404, don't bother trying again as the cluster is gone
                    if exc.response.status_code == 404:
                        break
                    # On a conflict response, go round again
                    elif exc.response.status_code == 409:
                        continue
                    # Any other error should be bubbled up
                    else:
                        raise
                else:
                    # On success, we can break the loop
                    break
        return wrapper
    return decorator


@on_managed_object_event(capi.Cluster.api_version, capi.Cluster.name)
async def on_capi_cluster_event(builder, type, body, **kwargs):
    """
    Executes on events for CAPI clusters with an associated Azimuth cluster.
    """
    if type == "DELETED":
        builder.cluster_deleted(body)
    else:
        builder.cluster_updated(body)


@on_managed_object_event(capi.KubeadmControlPlane.api_version, capi.KubeadmControlPlane.name)
async def on_capi_controlplane_event(builder, type, body, **kwargs):
    """
    Executes on events for CAPI control planes with an associated Azimuth cluster.
    """
    if type == "DELETED":
        builder.control_plane_deleted(body)
    else:
        builder.control_plane_updated(body)


@on_managed_object_event(capi.Machine.api_version, capi.Machine.name)
async def on_capi_machine_event(client, builder, type, body, **kwargs):
    """
    Executes on events for CAPI machines with an associated Azimuth cluster.
    """
    if type == "DELETED":
        builder.machine_deleted(body)
    else:
        # Get the underlying infrastructure machine as we need it for the size
        infra_ref = body["spec"]["infrastructureRef"]
        infra_resource = await client.api(infra_ref["apiVersion"]).resource(infra_ref["kind"])
        infra_machine = await infra_resource.fetch(
            infra_ref["name"],
            namespace = infra_ref["namespace"]
        )
        builder.machine_updated(body, infra_machine)


@on_managed_object_event(
    k8s.Job.api_version,
    k8s.Job.name,
    # The addon jobs use the label app.kubernetes.io/instance to identify the cluster
    cluster_label = "app.kubernetes.io/instance",
    # They should also have the following labels
    labels = {
        "app.kubernetes.io/name": "addons",
        "app.kubernetes.io/component": kopf.PRESENT,
        "capi.stackhpc.com/operation": kopf.PRESENT,
    }
)
async def on_addon_job_event(builder, type, body, **kwargs):
    """
    Executes on events for CAPI addon jobs.
    """
    if type == "DELETED":
        builder.addon_job_deleted(body)
    else:
        builder.addon_job_updated(body)


@on_managed_object_event(
    k8s.Secret.api_version,
    k8s.Secret.name,
    # The kubeconfig secret does not have the capi.stackhpc.com/cluster label
    # But it does have cluster.x-k8s.io/cluster-name
    cluster_label = "cluster.x-k8s.io/cluster-name"
)
async def on_cluster_secret_event(builder, type, body, name, **kwargs):
    """
    Executes on events for CAPI cluster secrets.
    """
    if type != "DELETED" and name.endswith("-kubeconfig"):
        builder.kubeconfig_secret_updated(body)


@on_managed_object_event(
    k8s.Secret.api_version,
    k8s.Secret.name,
    # Secrets representing services should have a service-name label
    labels = {
        "azimuth.stackhpc.com/service-name": kopf.PRESENT,
    }
)
async def on_service_secret_event(builder, type, body, **kwargs):
    """
    Executes on events for Zenith service secrets.
    """
    if type == "DELETED":
        builder.service_secret_deleted(body)
    else:
        builder.service_secret_updated(body)
