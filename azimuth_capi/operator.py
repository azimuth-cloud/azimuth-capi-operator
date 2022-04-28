import base64
import dataclasses
import functools
import logging
import ssl

import pydantic

import kopf

from easykube import Configuration, ApiError, ResourceSpec, resources as k8s

from . import capi_resources as capi
from .builder import ClusterStatusBuilder
from .config import settings
from .helm import Chart, Release
from .models import v1alpha1 as api
from .template import default_loader
from .utils import mergeconcat
from .zenith import zenith_values, zenith_operator_resources


logger = logging.getLogger(__name__)


# Create an easykube client from the environment
from pydantic.json import pydantic_encoder
ekclient = Configuration.from_environment(json_encoder = pydantic_encoder).async_client()


# Load the CRDs and create easykube resource specs for them
cluster_template_crd = default_loader.load("crd-clustertemplates.yaml")
AzimuthClusterTemplate = ResourceSpec.from_crd(cluster_template_crd)

cluster_crd = default_loader.load("crd-clusters.yaml")
AzimuthCluster = ResourceSpec.from_crd(cluster_crd)
AzimuthClusterStatus = dataclasses.replace(AzimuthCluster, name = f"{AzimuthCluster.name}/status")


@kopf.on.startup()
async def apply_settings(**kwargs):
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
    # Create the CRDs
    await ekclient.apply_object(cluster_template_crd)
    await ekclient.apply_object(cluster_crd)


@kopf.on.validate(
    AzimuthClusterTemplate.api_version,
    AzimuthClusterTemplate.name,
    id = "validate-cluster-template"
)
async def validate_cluster_template(name, spec, operation, **kwargs):
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
            existing = await AzimuthClusterTemplate(ekclient).fetch(name)
        except ApiError as exc:
            if exc.response.status_code != 404:
                raise
        else:
            if existing.spec["values"] != spec.values.dict(by_alias = True):
                raise kopf.AdmissionError("field 'values' cannot be changed")
    elif operation == "DELETE":
        clusters = AzimuthCluster(ekclient).list(
            labels = { f"{settings.api_group}/cluster-template": name },
            all_namespaces = True
        )
        try:
            _ = await clusters.__anext__()
        except StopAsyncIteration:
            pass  # In this case, the delete is permitted
        else:
            raise kopf.AdmissionError("template is in use by at least one cluster")


@kopf.on.validate(
    AzimuthCluster.api_version,
    AzimuthCluster.name,
    id = "validate-cluster"
)
async def validate_cluster(name, namespace, spec, operation, **kwargs):
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
        _ = await k8s.Secret(ekclient).fetch(
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
        template = await AzimuthClusterTemplate(ekclient).fetch(spec.template_name)
    except ApiError as exc:
        if exc.response.status_code == 404:
            raise kopf.AdmissionError("specified cluster template does not exist")
        else:
            raise
    # If the template is being changed (including on create), it must not be deprecated
    if template.spec.deprecated:
        try:
            existing = await AzimuthCluster(ekclient).fetch(name, namespace = namespace)
        except ApiError as exc:
            if exc.response.status_code == 404:
                raise kopf.AdmissionError("specified cluster template is deprecated")
            else:
                raise
        else:
            if existing.spec["templateName"] != template.metadata.name:
                raise kopf.AdmissionError("specified cluster template is deprecated")


@kopf.on.create(AzimuthCluster.api_version, AzimuthCluster.name)
@kopf.on.update(AzimuthCluster.api_version, AzimuthCluster.name, field = "spec")
async def on_cluster_create(name, namespace, body, spec, patch, **kwargs):
    """
    Executes when a new cluster is created or the spec of an existing cluster is updated.
    """
    spec = api.ClusterSpec.parse_obj(spec)
    # First, make sure that the secret exists
    secret = await k8s.Secret(ekclient).fetch(
        spec.cloud_credentials_secret_name,
        namespace = namespace
    )
    # Then fetch the template
    template = api.ClusterTemplate.parse_obj(
        await AzimuthClusterTemplate(ekclient).fetch(spec.template_name)
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
        helm_values = mergeconcat(helm_values, await zenith_values(ekclient, body, spec.addons))
    # Use Helm to install or upgrade the release
    await Release(name, namespace).install_or_upgrade(chart, helm_values)
    # Ensure that a Zenith operator instance exists for the cluster
    if settings.zenith.enabled:
        operator_resources = await zenith_operator_resources(name, namespace, secret)
        for resource in operator_resources:
            kopf.adopt(resource, body)
            await ekclient.apply_object(resource)
    # Patch the labels to include the cluster template
    # This is used by the admission webhook to search for clusters using a template in
    # order to prevent deletion of cluster templates that are in use
    labels = patch.setdefault("metadata", {}).setdefault("labels", {})
    labels[f"{settings.api_group}/cluster-template"] = spec.template_name


@kopf.on.delete(AzimuthCluster.api_version, AzimuthCluster.name)
async def on_cluster_delete(name, namespace, **kwargs):
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
        cluster = await capi.Cluster(ekclient).fetch(name, namespace = namespace)
    except ApiError as exc:
        if exc.status_code == 404:
            infra_ref = None
            infra_resource = None
        else:
            raise
    else:
        infra_ref = cluster.spec["infrastructureRef"]
        infra_resource = await ekclient.api(infra_ref["apiVersion"]).resource(infra_ref["kind"])
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
        az_cluster, stream = await AzimuthCluster(ekclient).watch_one(name, namespace = namespace)
        if az_cluster and len(az_cluster.get("status", {}).get("nodes", {})) > 0:
            async with stream as events:
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
    cluster, stream = await capi.Cluster(ekclient).watch_one(name, namespace = namespace)
    if cluster:
        async with stream as events:
            async for event in events:
                if event["type"] == "DELETED":
                    break


@kopf.on.resume(AzimuthCluster.api_version, AzimuthCluster.name)
async def on_cluster_resume(name, namespace, body, status, **kwargs):
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
        async for machine in capi.Machine(ekclient).list(
            labels = labels,
            namespace = namespace
        )
    ])
    try:
        kcps = capi.KubeadmControlPlane(ekclient).list(labels = labels, namespace = namespace)
        _ = await kcps.__anext__()
    except StopAsyncIteration:
        builder.control_plane_absent()
    try:
        clusters = capi.Cluster(ekclient).list(labels = labels, namespace = namespace)
        _ = await clusters.__anext__()
    except StopAsyncIteration:
        builder.cluster_absent()
    # The addon jobs need different labels
    # Each addon that is deployed will have an install job
    builder.remove_unknown_addons([
        job
        async for job in k8s.Job(ekclient).list(
            namespace = namespace,
            labels = {
                "app.kubernetes.io/name": "addons",
                "app.kubernetes.io/instance": name,
                "capi.stackhpc.com/operation": "install",
            }
        )
    ])
    changed, next_status = builder.build()
    if changed:
        await AzimuthClusterStatus(ekclient).replace(
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
        @kopf.on.event(*args, **kwargs)
        @functools.wraps(fn)
        async def wrapper(**inner):
            # Retry the fetch and updating of the state until it succeeds without conflict
            # kopf retry logic does not apply to events
            while True:
                try:
                    # Fetch the current state of the associated Azimuth cluster and pass it to the
                    # decorated function as the 'parent' keyword argument
                    cluster = await AzimuthCluster(ekclient).fetch(
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
                        await AzimuthClusterStatus(ekclient).replace(
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
async def on_capi_machine_event(builder, type, body, **kwargs):
    """
    Executes on events for CAPI machines with an associated Azimuth cluster.
    """
    if type == "DELETED":
        builder.machine_deleted(body)
    else:
        # Get the underlying infrastructure machine as we need it for the size
        infra_ref = body["spec"]["infrastructureRef"]
        infra_resource = await ekclient.api(infra_ref["apiVersion"]).resource(infra_ref["kind"])
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


ZenithReservation = ResourceSpec(
    settings.zenith.api_version,
    "reservations",
    "Reservation",
    True
)


def service_name(reservation):
    """
    Returns the service name for the reservation.
    """
    name = reservation["metadata"]["name"]
    namespace = reservation["metadata"]["namespace"]
    return name if name.startswith(namespace) else f"{namespace}-{name}"


def service_status(reservation):
    """
    Returns the service status for the given reservation.
    """
    annotations = reservation["metadata"].get("annotations", {})
    # If no label is specified, derive one from the name
    if "azimuth.stackhpc.com/service-label" in annotations:
        label = annotations["azimuth.stackhpc.com/service-label"]
    else:
        name = reservation["metadata"]["name"]
        label = " ".join(word.capitalize() for word in name.split("-"))
    return api.ServiceStatus(
        fqdn = reservation["status"]["fqdn"],
        label = label.strip(),
        icon_url = annotations.get("azimuth.stackhpc.com/service-icon-url"),
        description = annotations.get("azimuth.stackhpc.com/service-description")
    )


@kopf.daemon(AzimuthCluster.api_version, AzimuthCluster.name, cancellation_timeout = 1)
async def monitor_cluster_services(name, namespace, **kwargs):
    """
    Daemon that monitors Zenith reservations
    """
    if not settings.zenith.enabled:
        return
    try:
        kubeconfig = await k8s.Secret(ekclient).fetch(
            f"{name}-kubeconfig",
            namespace = namespace
        )
    except ApiError as exc:
        if exc.status_code == 404:
            raise kopf.TemporaryError("could not find kubeconfig for cluster")
        else:
            raise
    config = Configuration.from_kubeconfig_data(
        base64.b64decode(kubeconfig.data["value"]),
        json_encoder = pydantic_encoder
    )
    async with config.async_client() as client:
        try:
            initial, events = await ZenithReservation(client).watch_list(all_namespaces = True)
            # The initial reservations represent the current known state of the services
            # So we rebuild and replace the full service state
            await AzimuthClusterStatus(ekclient).json_patch(
                name,
                [
                    {
                        "op": "replace",
                        "path": "/status/services",
                        "value": {
                            service_name(reservation): service_status(reservation)
                            for reservation in initial
                            if reservation.get("status", {}).get("phase", "Unknown") == "Ready"
                        },
                    },
                ],
                namespace = namespace
            )
            # For subsequent events, we just need to patch the state of the specified service
            async for event in events:
                event_type, reservation = event["type"], event.get("object")
                if event_type in {"ADDED", "MODIFIED"}:
                    if reservation.get("status", {}).get("phase", "Unknown") == "Ready":
                        await AzimuthClusterStatus(ekclient).patch(
                            name,
                            {
                                "status": {
                                    "services": {
                                        service_name(reservation): service_status(reservation),
                                    },
                                },
                            },
                            namespace = namespace
                        )
                elif event_type == "DELETED":
                    await AzimuthClusterStatus(ekclient).json_patch(
                        name,
                        [
                            {
                                "op": "remove",
                                "path": f"/status/services/{service_name(reservation)}",
                            },
                        ],
                        namespace = namespace
                    )
        except (ApiError, ssl.SSLCertVerificationError) as exc:
            # These are expected, recoverable errors that we can retry
            raise kopf.TemporaryError(str(exc))
