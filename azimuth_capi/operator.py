import asyncio
import functools
import logging

import kopf

from easykube import Client, ApiError, ResourceSpec
from easykube import resources as k8s

from . import capi_resources as capi
from .builder import ClusterStatusBuilder
from .config import settings
from .helm import Chart, Release
from .models import v1alpha1 as api
from .template import default_loader
from .utils import deepmerge
from .zenith import zenith_values


logger = logging.getLogger(__name__)


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
                try:
                    async with Client.from_environment() as client:
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


@on("create", AzimuthCluster.api_version, AzimuthCluster.name)
@on("update", AzimuthCluster.api_version, AzimuthCluster.name, field = "spec")
async def on_cluster_create(client, name, namespace, body, spec, **kwargs):
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
        # The repository is a configuration item
        repository = settings.capi_helm_repo,
        # The chart name and version come from the template spec
        name = template.spec.chart_name,
        # The version comes from the template spec
        version = template.spec.chart_version
    )
    # Convert the cluster spec into overrides for the Helm values
    cluster_values = {
        "global": {
            "cloudCredentialsSecretName": secret.metadata.name,
        },
        "controlPlane": {
            "machineFlavor": spec.control_plane_machine_size,
            "machineRootVolumeSize": spec.machine_root_volume_size,
            "healthCheck": {
                "enabled": spec.autohealing,
            },
        },
        "nodeGroupDefaults": {
            "machineRootVolumeSize": spec.machine_root_volume_size,
            "healthCheck": {
                "enabled": spec.autohealing,
            },
        },
        "nodeGroups": [
            {
                "name": node_group.name,
                "machineFlavor": node_group.machine_size,
                "machineCount": node_group.count,
            }
            for node_group in spec.node_groups
        ],
        "addons": {
            "certManager": {
                "enabled": spec.addons.cert_manager,
            },
            "ingress": {
                "enabled": spec.addons.ingress,
            },
            "monitoring": {
                "enabled": spec.addons.monitoring,
            },
        },
    }
    helm_values = template.spec.values.dict(by_alias = True)
    helm_values = deepmerge(helm_values, cluster_values)
    if settings.zenith.enabled:
        helm_values = deepmerge(
            helm_values,
            await zenith_values(client, body, settings.zenith)
        )
    # Use Helm to install or upgrade the release
    await Release(name, namespace).install_or_upgrade(chart, helm_values)


@on("delete", AzimuthCluster.api_version, AzimuthCluster.name)
async def on_cluster_delete(client, name, namespace, **kwargs):
    """
    Executes whenever a cluster is deleted.
    """
    # If the corresponding Helm release exists, delete it
    release = Release(name, namespace)
    exists = await release.exists()
    if exists:
        await release.delete()
    # Wait until the associated CAPI cluster no longer exists
    while True:
        cluster, events = await capi.Cluster(client).watch_one(name, namespace = namespace)
        if not cluster:
            break
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
    await AzimuthClusterStatus(client).replace(
        name,
        dict(body, status = builder.build()),
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
                    cluster.status = builder.build()
                    # The cluster CRD uses a status subresource, so we have to specifically
                    # use that in order to replace the status
                    await AzimuthClusterStatus(inner["client"]).replace(
                        cluster.metadata.name,
                        cluster,
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
async def on_capi_machine_event(builder, type, body, **kwargs):
    """
    Executes on events for CAPI machines with an associated Azimuth cluster.
    """
    if type == "DELETED":
        builder.machine_deleted(body)
    else:
        builder.machine_updated(body)


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
    Exectes on events for CAPI cluster secrets.
    """
    if type != "DELETED" and name.endswith("-kubeconfig"):
        builder.kubeconfig_secret_updated(body)
