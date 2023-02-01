import base64
import datetime as dt
import functools
import json
import logging
import pathlib
import ssl
import sys

import kopf
import httpx
import pydantic
import yaml

from easykube import Configuration, ApiError
from kube_custom_resource import CustomResourceRegistry
from pyhelm3 import Client as HelmClient

from . import models, semver, status
from .config import settings
from .models import v1alpha1 as api
from .template import default_loader
from .utils import argo_application, mergeconcat, yaml_dump
from .zenith import zenith_values, zenith_operator_app_source


logger = logging.getLogger(__name__)


# Constants for the Cluster API versions
CLUSTER_API_VERSION = "cluster.x-k8s.io/v1beta1"
CLUSTER_API_CONTROLPLANE_VERSION = f"controlplane.{CLUSTER_API_VERSION}"


# Create an easykube client from the environment
from pydantic.json import pydantic_encoder
ekclient = (
    Configuration
        .from_environment(json_encoder = pydantic_encoder)
        .async_client(default_field_manager = settings.easykube_field_manager)
)


# Create a Helm client to target the underlying cluster
helm_client = HelmClient(
    default_timeout = settings.helm_client.default_timeout,
    executable = settings.helm_client.executable,
    history_max_revisions = settings.helm_client.history_max_revisions,
    insecure_skip_tls_verify = settings.helm_client.insecure_skip_tls_verify,
    unpack_directory = settings.helm_client.unpack_directory
)


# Create a registry of custom resources and populate it from the models module
registry = CustomResourceRegistry(settings.api_group, settings.crd_categories)
registry.discover_models(models)


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
    try:
        for crd in registry:
            await ekclient.apply_object(crd.kubernetes_resource(), force = True)
    except Exception:
        logger.exception("error applying CRDs - exiting")
        sys.exit(1)


@kopf.on.cleanup()
async def on_cleanup(**kwargs):
    """
    Runs on operator shutdown.
    """
    await ekclient.aclose()


async def ekresource_for_model(model, subresource = None):
    """
    Returns an easykube resource for the given model.
    """
    api = ekclient.api(f"{settings.api_group}/{model._meta.version}")
    resource = model._meta.plural_name
    if subresource:
        resource = f"{resource}/{subresource}"
    return await api.resource(resource)


async def save_cluster_status(cluster):
    """
    Save the status of this addon using the given easykube client.
    """
    # Make sure that the status is finalised before saving
    status.finalise(cluster)
    ekresource = await ekresource_for_model(api.Cluster, "status")
    data = await ekresource.replace(
        cluster.metadata.name,
        {
            # Include the resource version for optimistic concurrency
            "metadata": { "resourceVersion": cluster.metadata.resource_version },
            "status": cluster.status.dict(exclude_defaults = True),
        },
        namespace = cluster.metadata.namespace
    )
    # Store the new resource version
    cluster.metadata.resource_version = data["metadata"]["resourceVersion"]


def model_handler(model, register_fn, /, include_instance = True, **kwargs):
    """
    Decorator that registers a handler with kopf for the specified model.
    """
    api_version = f"{settings.api_group}/{model._meta.version}"
    def decorator(func):
        @functools.wraps(func)
        async def handler(**handler_kwargs):
            if include_instance and "instance" not in handler_kwargs:
                handler_kwargs["instance"] = model.parse_obj(handler_kwargs["body"])
            try:
                return await func(**handler_kwargs)
            except ApiError as exc:
                if exc.status_code == 409:
                    # When a handler fails with a 409, we want to retry quickly
                    raise kopf.TemporaryError(str(exc), delay = 5)
                else:
                    raise
        return register_fn(api_version, model._meta.plural_name, **kwargs)(handler)
    return decorator


@model_handler(
    api.ClusterTemplate,
    kopf.on.validate,
    # We want to validate the instance ourselves
    include_instance = False,
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
            raise kopf.AdmissionError(str(exc), code = 400)
    elif operation == "DELETE":
        ekresource = await ekresource_for_model(api.Cluster)
        clusters = ekresource.list(
            labels = { f"{settings.api_group}/cluster-template": name },
            all_namespaces = True
        )
        try:
            _ = await clusters.__anext__()
        except StopAsyncIteration:
            pass  # In this case, the delete is permitted
        else:
            raise kopf.AdmissionError("template is in use by at least one cluster", code = 400)


@model_handler(
    api.Cluster,
    kopf.on.validate,
    # We want to validate the instance ourselves
    include_instance = False,
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
        raise kopf.AdmissionError(str(exc), code = 400)
    # The credentials secret must exist
    ekresource = await ekclient.api("v1").resource("secrets")
    try:
        _ = await ekresource.fetch(
            spec.cloud_credentials_secret_name,
            namespace = namespace
        )
    except ApiError as exc:
        if exc.status_code == 404:
            raise kopf.AdmissionError(
                "specified cloud credentials secret does not exist",
                code = 400
            )
        else:
            raise
    # The specified template must exist
    ekresource = await ekresource_for_model(api.ClusterTemplate)
    try:
        template = await ekresource.fetch(spec.template_name)
    except ApiError as exc:
        if exc.status_code == 404:
            raise kopf.AdmissionError("specified cluster template does not exist", code = 400)
        else:
            raise
    # If the template is being changed, we want to impose additional conditions
    ekresource = await ekresource_for_model(api.Cluster)
    try:
        existing = await ekresource.fetch(name, namespace = namespace)
    except ApiError as exc:
        if exc.status_code == 404:
            existing = {}
        else:
            raise
    if not existing or existing.spec["templateName"] != template.metadata.name:
        # The new template must not be deprecated
        if template.spec.get("deprecated", False):
            raise kopf.AdmissionError("specified cluster template is deprecated", code = 400)
        # The new template must not be a downgrade
        template_vn = template.spec["values"]["kubernetesVersion"]
        existing_vn = existing.get("status", {}).get("kubernetesVersion")
        if existing_vn and template_vn < existing_vn:
            raise kopf.AdmissionError("specified cluster template would be a downgrade", code = 400)


@model_handler(api.Cluster, kopf.on.create)
@model_handler(api.Cluster, kopf.on.update, field = "spec")
async def on_cluster_create(instance: api.Cluster, name, namespace, patch, **kwargs):
    """
    Executes when a new cluster is created or the spec of an existing cluster is updated.
    """
    # Make sure that the secret exists
    ekresource = await ekclient.api("v1").resource("secrets")
    secret = await ekresource.fetch(
        instance.spec.cloud_credentials_secret_name,
        namespace = namespace
    )
    # Then fetch the template
    ekresource = await ekresource_for_model(api.ClusterTemplate)
    template = api.ClusterTemplate.parse_obj(await ekresource.fetch(instance.spec.template_name))
    # Work out what the name of the cluster will be in Argo
    # To ensure uniqueness we include the first 8 characters of the cluster UID
    argo_cluster_name = settings.argocd.cluster_name_template.format(
        namespace = namespace,
        name = name,
        # In order to ensure uniqueness, we include the first 8 characters of the UID
        # Otherwise, ns-bob/cluster-1 and ns/bob-cluster-1 make the same app name
        id = instance.metadata.uid[:8]
    )
    # Create an Argo project to hold everything related to this cluster
    project = await ekclient.apply_object(
        {
            "apiVersion": settings.argocd.api_version,
            "kind": "AppProject",
            "metadata": {
                "name": argo_cluster_name,
                "namespace": settings.argocd.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "azimuth-capi-operator",
                },
                "annotations": {
                    # The annotation is used to identify the associated cluster
                    # when an event is observed for the Argo application
                    f"{settings.annotation_prefix}/owner-reference": (
                        f"{settings.api_group}:"
                        f"{api.Cluster._meta.plural_name}:"
                        f"{namespace}:"
                        f"{name}"
                    ),
                },
            },
            "spec": {
                # Allow the project to deploy any resource from any repo
                "sourceRepos": [
                    "*",
                ],
                "clusterResourceWhitelist": [
                    {
                        "group": "*",
                        "kind": "*",
                    },
                ],
                # TODO: Restrict the project to in-cluster + target cluster
                "destinations": [
                    {
                        "server": "*",
                        "namespace": "*",
                    },
                ],
            },
        },
        force = True
    )
    # Generate the Helm values for the release
    helm_values = mergeconcat(
        settings.capi_helm.default_values,
        template.spec.values.dict(by_alias = True),
        # Add an annotation to the cluster so that the addons go into our Argo project
        {
            "clusterAnnotations": {
                "addons.stackhpc.com/project": argo_cluster_name,
            },
        },
        default_loader.load("cluster-values.yaml", spec = instance.spec, settings = settings)
    )
    if settings.zenith.enabled:
        helm_values = mergeconcat(
            helm_values,
            await zenith_values(ekclient, instance, instance.spec.addons)
        )
    # We use the "app-of-apps" pattern to manage the cluster, addons and Zenith operator
    # as separate apps within a master app
    # See https://argo-cd.readthedocs.io/en/stable/operator-manual/cluster-bootstrapping/#app-of-apps-pattern
    # This also may allow us to use sync waves to remove the addons before deleting the cluster
    # https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/
    # So split the Helm values into those for the cluster and those for the addons
    helm_values_addons = helm_values.pop("addons", {})
    helm_values["addons"] = {"enabled": False}
    # Start with the application for the cluster infrastructure
    applications = [
        argo_application(
            f"{project.metadata.name}-infra",
            instance,
            project,
            {
                # "repoURL": settings.capi_helm.chart_repository,
                # "chart": settings.capi_helm.infra_chart_name,
                # "targetRevision": settings.capi_helm.chart_version,
                "repoURL": "https://github.com/stackhpc/capi-helm-charts.git",
                "path": "charts/openstack-cluster",
                "targetRevision": "feature/argo-apps",
                "helm": {
                    "releaseName": name,
                    "values": yaml_dump(helm_values),
                },
            }
        ),
    ]
    # Apply the defaults from the openstack-cluster in CAPI Helm charts
    helm_values_addons = mergeconcat(
        {
            "enabled": True,
            "openstack": {
                "enabled": True,
            },
        },
        helm_values_addons
    )
    # Add the application for the addons if required
    if helm_values_addons.pop("enabled"):
        applications.append(
            argo_application(
                f"{project.metadata.name}-addons",
                instance,
                project,
                {
                    # "repoURL": settings.capi_helm.chart_repository,
                    # "chart": settings.capi_helm.addons_chart_name,
                    # "targetRevision": settings.capi_helm.chart_version,
                    "repoURL": "https://github.com/stackhpc/capi-helm-charts.git",
                    "path": "charts/cluster-addons",
                    "targetRevision": "feature/argo-apps",
                    "helm": {
                        "releaseName": name,
                        "values": yaml_dump(helm_values_addons),
                    },
                }
            )
        )
    # Add the Argo application for the Zenith operator if required
    if settings.zenith.enabled:
        applications.append(
            argo_application(
                f"{project.metadata.name}-zenith-operator",
                instance,
                project,
                zenith_operator_app_source(instance, secret)
            )
        )
    # Apply the app-of-apps application using the raw chart
    await ekclient.apply_object(
        argo_application(
            project.metadata.name,
            instance,
            project,
            {
                "repoURL": "https://charts.helm.sh/incubator",
                "chart": "raw",
                "targetRevision": "0.2.5",
                "helm": {
                    "releaseName": name,
                    "values": yaml_dump({ "resources": applications }),
                },
            },
            settings.argocd.namespace
        ),
        force = True
    )
    # Patch the labels to include the cluster template
    # This is used by the admission webhook to search for clusters using a template in
    # order to prevent deletion of cluster templates that are in use
    labels = patch.setdefault("metadata", {}).setdefault("labels", {})
    labels[f"{settings.api_group}/cluster-template"] = instance.spec.template_name


@model_handler(api.Cluster, kopf.on.delete)
async def on_cluster_delete(instance: api.Cluster, name, namespace, **kwargs):
    """
    Executes whenever a cluster is deleted.
    """
    argo_cluster_name = settings.argocd.cluster_name_template.format(
        namespace = namespace,
        name = name,
        id = instance.metadata.uid[:8]
    )
    ekargo = ekclient.api(settings.argocd.api_version)
    # Delete the Argo app associated with the cluster and wait for it to be gone
    ekapps = await ekargo.resource("applications")
    try:
        app = await ekapps.fetch(argo_cluster_name, namespace = settings.argocd.namespace)
    except ApiError as exc:
        if exc.status_code != 404:
            raise
    else:
        if not app.metadata.get("deletionTimestamp"):
            await ekapps.delete(app.metadata.name, namespace = app.metadata.namespace)
        raise kopf.TemporaryError("waiting for cluster app to delete", delay = 5)
    # Delete the Argo project associated with the cluster and wait for it to be gone
    ekappprojects = await ekargo.resource("appprojects")
    try:
        project = await ekappprojects.fetch(
            argo_cluster_name,
            namespace = settings.argocd.namespace
        )
    except ApiError as exc:
        if exc.status_code != 404:
            raise
    else:
        if not project.metadata.get("deletionTimestamp"):
            await ekappprojects.delete(
                project.metadata.name,
                namespace = project.metadata.namespace
            )
        raise kopf.TemporaryError("waiting for cluster project to delete", delay = 5)
    # Make sure that the Cluster API cluster is gone
    ekclusters = await ekclient.api(CLUSTER_API_VERSION).resource("clusters")
    try:
        await ekclusters.fetch(name, namespace = namespace)
    except ApiError as exc:
        if exc.status_code != 404:
            raise
    else:
        raise kopf.TemporaryError("waiting for cluster to delete", delay = 5)


@model_handler(api.Cluster, kopf.on.resume)
async def on_cluster_resume(instance, name, namespace, **kwargs):
    """
    Executes for each cluster when the operator is resumed.
    """
    # When we are resumed, the managed event handlers will be called with all the CAPI
    # objects that exist
    # However if CAPI objects have been deleted while the operator was down, we will
    # not receive delete events for those
    # So on resume we remove any items in the status that no longer exist
    labels = { "capi.stackhpc.com/cluster": name }
    # Get easykube resources for the Cluster API types
    ekcapi = ekclient.api(CLUSTER_API_VERSION)
    ekclusters = await ekcapi.resource("clusters")
    ekmachines = await ekcapi.resource("machines")
    ekcapicp = ekclient.api(CLUSTER_API_CONTROLPLANE_VERSION)
    ekcontrolplanes = await ekcapicp.resource("kubeadmcontrolplanes")
    # Remove unknown machines from the node list
    status.remove_unknown_nodes(
        instance,
        [
            machine
            async for machine in ekmachines.list(
                labels = labels,
                namespace = namespace
            )
        ]
    )
    # Check if the control plane still exists
    try:
        kcps = ekcontrolplanes.list(labels = labels, namespace = namespace)
        _ = await kcps.__anext__()
    except StopAsyncIteration:
        status.control_plane_absent(instance)
    # Check if the cluster still exists
    try:
        clusters = ekclusters.list(labels = labels, namespace = namespace)
        _ = await clusters.__anext__()
    except StopAsyncIteration:
        status.cluster_absent(instance)
    # Remove any addons that do not have a corresponding resource
    ekaddons = ekclient.api("addons.stackhpc.com/v1alpha1")
    ekhelmreleases = await ekaddons.resource("helmreleases")
    ekmanifests = await ekaddons.resource("manifests")
    status.remove_unknown_addons(
        instance,
        [
            addon
            async for addon in ekhelmreleases.list(
                namespace = namespace,
                labels = { "capi.stackhpc.com/cluster": name }
            )
        ] + [
            addon
            async for addon in ekmanifests.list(
                namespace = namespace,
                labels = { "capi.stackhpc.com/cluster": name }
            )
        ]
    )
    await save_cluster_status(instance)


def on_managed_object_event(*args, cluster_label = "capi.stackhpc.com/cluster", **kwargs):
    """
    Decorator that registers a function as updating the Azimuth cluster state in response
    to a CAPI resource changing.
    """
    # Limit the query to objects that have a cluster label
    kwargs.setdefault("labels", {}).update({ cluster_label: kopf.PRESENT })
    def decorator(func):
        @kopf.on.event(*args, **kwargs)
        @functools.wraps(func)
        async def wrapper(**inner):
            ekclusters = await ekresource_for_model(api.Cluster)
            # Retry the fetch and updating of the state until it succeeds without conflict
            # kopf retry logic does not apply to events
            while True:
                try:
                    cluster = api.Cluster.parse_obj(
                        await ekclusters.fetch(
                            inner["labels"][cluster_label],
                            namespace = inner["namespace"]
                        )
                    )
                    await func(cluster = cluster, **inner)
                    await save_cluster_status(cluster)
                except ApiError as exc:
                    # On a 404, don't bother trying again as the cluster is gone
                    if exc.status_code == 404:
                        break
                    # On a conflict response, go round again
                    elif exc.status_code == 409:
                        continue
                    # Any other error should be bubbled up
                    else:
                        raise
                else:
                    # On success, we can break the loop
                    break
        return wrapper
    return decorator


@on_managed_object_event(CLUSTER_API_VERSION, "clusters")
async def on_capi_cluster_event(cluster, type, body, **kwargs):
    """
    Executes on events for CAPI clusters with an associated Azimuth cluster.
    """
    if type == "DELETED":
        status.cluster_deleted(cluster, body)
    else:
        status.cluster_updated(cluster, body)


@on_managed_object_event(CLUSTER_API_CONTROLPLANE_VERSION, "kubeadmcontrolplanes")
async def on_capi_controlplane_event(cluster, type, body, **kwargs):
    """
    Executes on events for CAPI control planes with an associated Azimuth cluster.
    """
    if type == "DELETED":
        status.control_plane_deleted(cluster, body)
    else:
        status.control_plane_updated(cluster, body)


@on_managed_object_event(CLUSTER_API_VERSION, "machines")
async def on_capi_machine_event(cluster, type, body, **kwargs):
    """
    Executes on events for CAPI machines with an associated Azimuth cluster.
    """
    if type == "DELETED":
        status.machine_deleted(cluster, body)
    else:
        # Get the underlying infrastructure machine as we need it for the size
        infra_ref = body["spec"]["infrastructureRef"]
        infra_resource = await ekclient.api(infra_ref["apiVersion"]).resource(infra_ref["kind"])
        infra_machine = await infra_resource.fetch(
            infra_ref["name"],
            namespace = infra_ref["namespace"]
        )
        status.machine_updated(cluster, body, infra_machine)


@on_managed_object_event("addons.stackhpc.com/v1alpha1", "helmreleases")
async def on_helmrelease_event(cluster, type, body, **kwargs):
    """
    Executes on events for HelmRelease addons.
    """
    if type == "DELETED":
        status.addon_deleted(cluster, body)
    else:
        status.addon_updated(cluster, body)


@on_managed_object_event("addons.stackhpc.com/v1alpha1", "manifests")
async def on_manifests_event(cluster, type, body, **kwargs):
    """
    Executes on events for Manifests addons.
    """
    if type == "DELETED":
        status.addon_deleted(cluster, body)
    else:
        status.addon_updated(cluster, body)


@on_managed_object_event(
    "v1",
    "secrets",
    # The kubeconfig secret does not have the capi.stackhpc.com/cluster label
    # But it does have cluster.x-k8s.io/cluster-name
    cluster_label = "cluster.x-k8s.io/cluster-name"
)
async def on_cluster_secret_event(cluster, type, body, name, **kwargs):
    """
    Executes on events for CAPI cluster secrets.
    """
    if type != "DELETED" and name.endswith("-kubeconfig"):
        status.kubeconfig_secret_updated(cluster, body)


async def annotate_addon_for_reservation(reservation, reservation_deleted = False):
    """
    Annotates the addon for the reservation, if one exists, with information
    about the reservation.
    """
    # Bail early if we can
    # We will only make changes for reservations that are being deleted or are Ready
    if (
        not reservation_deleted and
        reservation.get("status", {}).get("phase", "Unknown") != "Ready"
    ):
        return
    # If the reservation is not part of an Argo application, it isn't part of an addon
    reservation_annotations = reservation["metadata"].get("annotations", {})
    tracking_id = reservation_annotations.get(settings.argocd.tracking_id_annotation)
    if not tracking_id:
        return
    # Get the Argo app name from the tracking ID and try to find it
    ekapps = await ekclient.api(settings.argocd.api_version).resource("applications")
    app_name, _ = tracking_id.split(":", maxsplit = 1)
    try:
        app = await ekapps.fetch(app_name, namespace = settings.argocd.namespace)
    except ApiError as exc:
        if exc.status_code == 404:
            return
        else:
            raise
    # See if the Argo app has an owner reference to an addon
    owner_ref = (
        app["metadata"]
            .get("annotations", {})
            .get("addons.stackhpc.com/owner-reference")
    )
    if not owner_ref:
        return
    # Fetch the addon that produced the Argo app
    api_group, plural_name, namespace, name = owner_ref.split(":")
    ekaddons = await ekclient.api_preferred_version(api_group)
    ekresource = await ekaddons.resource(plural_name)
    try:
        addon = await ekresource.fetch(name, namespace = namespace)
    except ApiError as exc:
        if exc.status_code == 404:
            return
        else:
            raise
    # Get the service name for the reservation
    reservation_name = reservation["metadata"]["name"]
    reservation_namespace = reservation["metadata"]["namespace"]
    service_name = (
        reservation_name
        if reservation_name.startswith(reservation_namespace)
        else f"{reservation_namespace}-{reservation_name}"
    )
    # Update the services for the addon to reflect the reservation
    # Note that this function will not run concurrently for two reservations on the same
    # cluster, so this is a safe operation
    annotations = addon.metadata.get("annotations", {})
    services = json.loads(annotations.get("azimuth.stackhpc.com/services", "{}"))
    if not reservation_deleted:
        # If the reservation is in the ready state, update the service
        services[service_name] = dict(
            fqdn = reservation["status"]["fqdn"],
            label = reservation_annotations.get(
                "azimuth.stackhpc.com/service-label",
                # If no label is specified, derive one from the name
                " ".join(word.capitalize() for word in reservation_name.split("-"))
            ),
            icon_url = reservation_annotations.get("azimuth.stackhpc.com/service-icon-url"),
            description = reservation_annotations.get("azimuth.stackhpc.com/service-description")
        )
    else:
        # If the reservation was deleted, remove the service
        services.pop(service_name, None)
    await ekresource.patch(
        addon.metadata.name,
        {
            "metadata": {
                "annotations": {
                    "azimuth.stackhpc.com/services": json.dumps(services),
                },
            },
        },
        namespace = addon.metadata.namespace
    )


@model_handler(
    api.Cluster,
    kopf.daemon,
    include_instance = False,
    cancellation_timeout = 1
)
async def monitor_cluster_services(name, namespace, **kwargs):
    """
    Daemon that monitors Zenith reservations
    """
    if not settings.zenith.enabled:
        return
    eksecrets = await ekclient.api("v1").resource("secrets")
    try:
        kubeconfig = await eksecrets.fetch(
            f"{name}-kubeconfig",
            namespace = namespace
        )
    except ApiError as exc:
        if exc.status_code == 404:
            raise kopf.TemporaryError("could not find kubeconfig for cluster")
        else:
            raise
    kubeconfig_data = base64.b64decode(kubeconfig.data["value"])
    ekclient_target = (
        Configuration
            .from_kubeconfig_data(kubeconfig_data, json_encoder = pydantic_encoder)
            .async_client(default_field_manager = settings.easykube_field_manager)
    )
    async with ekclient_target:
        try:
            ekzenithapi = ekclient_target.api(settings.zenith.api_version)
            ekzenithreservations = await ekzenithapi.resource("reservations")
            initial, events = await ekzenithreservations.watch_list(all_namespaces = True)
            for reservation in initial:
                await annotate_addon_for_reservation(reservation)
            # For subsequent events, we just need to patch the state of the specified service
            async for event in events:
                event_type, reservation = event["type"], event.get("object")
                if not reservation:
                    continue
                await annotate_addon_for_reservation(reservation, event_type == "DELETED")
        except (ApiError, ssl.SSLCertVerificationError) as exc:
            # These are expected, recoverable errors that we can retry
            raise kopf.TemporaryError(str(exc))


@model_handler(api.AppTemplate, kopf.on.create)
# Use a param to distinguish an update to the spec, as we want to act instantly
# in this case rather than waiting for the sync frequency to elapse
@model_handler(api.AppTemplate, kopf.on.update, field = "spec", param = "update")
@model_handler(api.AppTemplate, kopf.on.resume)
@model_handler(
    api.AppTemplate,
    kopf.on.timer,
    # Since we have create and update handlers, we want to idle after a change
    interval = settings.timer_interval,
    idle = settings.timer_interval
)
async def reconcile_app_template(instance, param, **kwargs):
    """
    Reconciles an app template when it is created or updated, when the operator
    is resumed or periodically.
    """
    # First, we need to decide whether to do anything
    now = dt.datetime.now(dt.timezone.utc)
    sync_delta = dt.timedelta(seconds = instance.spec.sync_frequency)
    # We can exit here is there is nothing to do
    if not (
        # If there was an update, we want to check for new versions
        param == "update" or
        # If there has never been a successful sync, do one
        not instance.status.last_sync or
        # If it has been longer than the sync delta since the last sync
        instance.status.last_sync + sync_delta < now
    ):
        return
    # Fetch the repository index from the specified URL
    async with httpx.AsyncClient(base_url = instance.spec.chart.repo) as http:
        response = await http.get("index.yaml")
        response.raise_for_status()
    # Get the available versions for the chart that match our constraint, sorted
    # with the most recent first
    version_range = semver.Range(instance.spec.version_range)
    chart_versions = sorted(
        (
            v
            for v in yaml.safe_load(response.text)["entries"][instance.spec.chart.name]
            if semver.Version(v["version"]) in version_range
        ),
        key = lambda v: semver.Version(v["version"]),
        reverse = True
    )
    # Throw away any versions that we aren't keeping
    chart_versions = chart_versions[:instance.spec.keep_versions]
    if not chart_versions:
        raise kopf.PermanentError("no versions matching constraint")
    next_label = instance.status.label
    next_logo = instance.status.logo
    next_description = instance.status.description
    next_versions = []
    # For each version, we need to make sure we have a values schema and optionally a UI schema
    for chart_version in chart_versions:
        existing_version = next(
            (
                version
                for version in instance.status.versions
                if version.name == chart_version["version"]
            ),
            None
        )
        # If we already know about the version, just use it as-is
        if existing_version:
            next_versions.append(existing_version)
            continue
        # Use the label, logo and description from the first version that has them
        # The label goes in a custom annotation as there isn't really a field for it, falling back
        # to the chart name if it is not present
        next_label = (
            next_label or
            chart_version.get("annotations", {}).get("azimuth.stackhpc.com/label") or
            chart_version["name"]
        )
        next_logo = next_logo or chart_version.get("icon")
        next_description = next_description or chart_version.get("description")
        # Pull the chart to extract the values schema and UI schema, if present
        chart_context = helm_client.pull_chart(
            instance.spec.chart.name,
            repo = instance.spec.chart.repo,
            version = chart_version["version"]
        )
        async with chart_context as chart:
            chart_directory = pathlib.Path(chart.ref)
            values_schema_file = chart_directory / "values.schema.json"
            ui_schema_file = chart_directory / "azimuth-ui.schema.yaml"
            if values_schema_file.is_file():
                with values_schema_file.open() as fh:
                    values_schema = json.load(fh)
            else:
                values_schema = {}
            if ui_schema_file.is_file():
                with ui_schema_file.open() as fh:
                    ui_schema = yaml.safe_load(fh)
            else:
                ui_schema = {}
        next_versions.append(
            api.AppTemplateVersion(
                name = chart_version["version"],
                values_schema = values_schema,
                ui_schema = ui_schema
            )
        )
    instance.status.label = instance.spec.label or next_label
    instance.status.logo = instance.spec.logo or next_logo
    instance.status.description = instance.spec.description or next_description
    instance.status.versions = next_versions
    instance.status.last_sync = dt.datetime.now(dt.timezone.utc)
    ekresource = await ekresource_for_model(api.AppTemplate, "status")
    _ = await ekresource.replace(
        instance.metadata.name,
        {
            # Include the resource version for optimistic concurrency
            "metadata": { "resourceVersion": instance.metadata.resource_version },
            "status": instance.status.dict(exclude_defaults = True),
        }
    )
