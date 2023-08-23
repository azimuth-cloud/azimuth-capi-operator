import base64
import datetime as dt
import functools
import json
import logging
import pathlib
import secrets
import ssl
import string
import sys

import kopf
import httpx
import pydantic
import yaml

from easykube import Configuration, ApiError
import easysemver
from kube_custom_resource import CustomResourceRegistry
from pyhelm3 import Client as HelmClient, errors as helm_errors

from . import models, status
from .config import settings
from .models import v1alpha1 as api
from .template import default_loader
from .utils import mergeconcat
from .zenith import zenith_values, zenith_operator_resources


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
async def on_cluster_create(instance, name, namespace, patch, **kwargs):
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
    # Get the name for the Helm release
    if not instance.status.helm_release_name:
        try:
            # Check if a Helm release already exists with the cluster name
            _ = await helm_client.get_current_revision(name, namespace = namespace)
        except helm_errors.ReleaseNotFoundError:
            # This means that we have an opportunity to use a random suffix
            chars = string.ascii_lowercase + string.digits
            suffix = "".join(secrets.choice(chars) for _ in range(settings.suffix_length))
            instance.status.helm_release_name = f"{name}-{suffix}"
        else:
            # This means that the cluster was created prior to the random suffixes
            instance.status.helm_release_name = name
        await save_cluster_status(instance)
    # Generate the Helm values for the release
    helm_values = mergeconcat(
        settings.capi_helm.default_values,
        template.spec.values.dict(by_alias = True),
        default_loader.load("cluster-values.yaml", spec = instance.spec, settings = settings)
    )
    if settings.zenith.enabled:
        helm_values = mergeconcat(
            helm_values,
            await zenith_values(ekclient, instance, instance.spec.addons)
        )
    # Use Helm to install or upgrade the release
    _ = await helm_client.install_or_upgrade_release(
        instance.status.helm_release_name,
        await helm_client.get_chart(
            settings.capi_helm.chart_name,
            repo = settings.capi_helm.chart_repository,
            version = settings.capi_helm.chart_version
        ),
        helm_values,
        namespace = namespace,
        # The target namespace already exists, because the cluster is in it
        create_namespace = False
    )
    # Ensure that a Zenith operator instance exists for the cluster
    if settings.zenith.enabled:
        operator_resources = await zenith_operator_resources(instance, secret)
        for resource in operator_resources:
            kopf.adopt(resource, instance.dict(by_alias = True))
            await ekclient.apply_object(resource, force = True)
    # Patch the labels to include the cluster template and the release name
    # These are used to search for the cluster later
    patch.setdefault("metadata", {}).setdefault("labels", {}).update({
        f"{settings.api_group}/cluster-template": instance.spec.template_name,
        f"{settings.api_group}/release-name": instance.status.helm_release_name,
    })


@model_handler(api.Cluster, kopf.on.delete)
async def on_cluster_delete(instance, name, namespace, **kwargs):
    """
    Executes whenever a cluster is deleted.
    """
    # Delete the corresponding Helm release
    release_name = instance.status.helm_release_name or name
    try:
        await helm_client.uninstall_release(release_name, namespace = namespace)
    except helm_errors.ReleaseNotFoundError:
        pass
    # Wait until the associated CAPI cluster no longer exists
    ekresource = await ekclient.api(CLUSTER_API_VERSION).resource("clusters")
    cluster, stream = await ekresource.watch_one(release_name, namespace = namespace)
    if cluster:
        async with stream as events:
            async for event in events:
                if event["type"] == "DELETED":
                    break


@model_handler(api.Cluster, kopf.on.resume)
async def on_cluster_resume(instance, name, namespace, patch, **kwargs):
    """
    Executes for each cluster when the operator is resumed.
    """
    # When we are resumed, the managed event handlers will be called with all the CAPI
    # objects that exist
    # However if CAPI objects have been deleted while the operator was down, we will
    # not receive delete events for those
    # So on resume we remove any items in the status that no longer exist
    release_name = instance.status.helm_release_name or name
    labels = { "capi.stackhpc.com/cluster": release_name }
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
            async for addon in ekhelmreleases.list(labels = labels, namespace = namespace)
        ] + [
            addon
            async for addon in ekmanifests.list(labels = labels, namespace = namespace)
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
                # Try to find the cluster with the name in the cluster label
                # We use the release name label first, falling back to by name
                # This is because all of these objects are created by the Helm release
                cluster_name = inner["labels"][cluster_label]
                cluster_namespace = inner["namespace"]
                cluster = await ekclusters.first(
                    labels = { f"{settings.api_group}/release-name": cluster_name },
                    namespace = cluster_namespace
                )
                if not cluster:
                    try:
                        cluster = await ekclusters.fetch(
                            cluster_name,
                            namespace = cluster_namespace
                        )
                    except ApiError as exc:
                        if exc.status_code == 404:
                            # We couldn't find the cluster - let's give up for now
                            break
                        else:
                            raise
                cluster = api.Cluster.parse_obj(cluster)
                try:
                    await func(cluster = cluster, **inner)
                    await save_cluster_status(cluster)
                except ApiError as exc:
                    if exc.status_code == 409:
                        # On a conflict response, go round again
                        continue
                    else:
                        # Any other error should be bubbled up
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


@on_managed_object_event("addons.stackhpc.com", "helmreleases")
async def on_helmrelease_event(cluster, type, body, **kwargs):
    """
    Executes on events for HelmRelease addons.
    """
    if type == "DELETED":
        status.addon_deleted(cluster, body)
    else:
        status.addon_updated(cluster, body)


@on_managed_object_event("addons.stackhpc.com", "manifests")
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


async def find_realm(cluster: api.Cluster):
    """
    Return the identity realm for the given cluster.
    """
    ekrealms = await ekclient.api(settings.identity.api_version).resource("realms")
    if cluster.spec.zenith_identity_realm_name:
        # If the cluster specifies a realm, use it
        try:
            return await ekrealms.fetch(
                cluster.spec.zenith_identity_realm_name,
                namespace = cluster.metadata.namespace
            )
        except ApiError as exc:
            if exc.status_code == 404:
                raise kopf.TemporaryError(
                    f"Could not find identity realm '{cluster.spec.zenith_identity_realm_name}'"
                )
            else:
                raise
    else:
        # Otherwise, try to discover a realm in the namespace
        realm = await ekrealms.first(namespace = cluster.metadata.namespace)
        if realm:
            return realm
        else:
            raise kopf.TemporaryError("No identity realm available to use")


@model_handler(api.Cluster, kopf.on.resume)
@model_handler(api.Cluster, kopf.on.update, field = "status.services")
async def on_cluster_services_updated(instance: api.Cluster, **kwargs):
    """
    Executed whenever the cluster services change.
    """
    realm = await find_realm(instance)
    platform = {
        "apiVersion": settings.identity.api_version,
        "kind": "Platform",
        "metadata": {
            "name": settings.identity.cluster_platform_name_template.format(
                cluster_name = instance.metadata.name,
            ),
            "namespace": instance.metadata.namespace,
        },
        "spec": {
            "realmName": realm.metadata.name,
            "zenithServices": {
                name: {
                    "subdomain": service.subdomain,
                    "fqdn": service.fqdn,
                }
                for name, service in instance.status.services.items()
            },
        },
    }
    kopf.adopt(platform, instance.dict())
    await ekclient.apply_object(platform, force = True)


@on_managed_object_event(
    "addons.stackhpc.com",
    "helmreleases",
    # Use the label applied by the addon operator to locate the corresponding cluster
    cluster_label = "addons.stackhpc.com/cluster",
    # This label is only present on Helm releases that correspond to Azimuth platforms
    labels = { "azimuth.stackhpc.com/app-template": kopf.PRESENT }
)
async def on_kubernetes_app_event(
    cluster,
    type,
    name,
    namespace,
    body,
    annotations,
    logger,
    **kwargs
):
    """
    Executes on events for HelmRelease addons that are labelled as representing an Azimuth app.
    """
    if type == "DELETED":
        return
    # kopf does not retry events, but we need to make sure that this is retried until it succeeds
    while True:
        try:
            realm = await find_realm(cluster)
            services_annotation = annotations.get("azimuth.stackhpc.com/services")
            if services_annotation:
                services = json.loads(services_annotation)
            else:
                services = {}
            platform = {
                "apiVersion": settings.identity.api_version,
                "kind": "Platform",
                "metadata": {
                    "name": settings.identity.app_platform_name_template.format(app_name = name),
                    "namespace": namespace,
                },
                "spec": {
                    "realmName": realm.metadata.name,
                    "zenithServices": {
                        name: {
                            "subdomain": service["subdomain"],
                            "fqdn": service["fqdn"],
                        }
                        for name, service in services.items()
                    },
                },
            }
            # The platform should be owned by the HelmRelease
            kopf.adopt(platform, body)
            await ekclient.apply_object(platform, force = True)
        except kopf.TemporaryError as exc:
            logger.error(f"{str(exc)} - retrying")
        except Exception:
            logger.exception("Exception while updating platform - retrying")
        else:
            break


async def annotate_addon_for_reservation(
    instance,
    reservation,
    service_name,
    service_status = None
):
    """
    Annotates the addon for the reservation, if one exists, with information
    about the reservation.
    """
    cluster_name = instance.status.helm_release_name or instance.metadata.name
    cluster_namespace = instance.metadata.namespace
    # If the reservation is not part of a Helm release, it isn't part of an addon
    annotations = reservation["metadata"].get("annotations", {})
    release_namespace = annotations.get("meta.helm.sh/release-namespace")
    release_name = annotations.get("meta.helm.sh/release-name")
    if not release_namespace or not release_name:
        return
    # Search the addons for the one that produced the release
    labels = {
        "addons.stackhpc.com/cluster": cluster_name,
        "addons.stackhpc.com/release-namespace": release_namespace,
        "addons.stackhpc.com/release-name": release_name,
    }
    ekaddons = ekclient.api("addons.stackhpc.com/v1alpha1")
    ekresource = await ekaddons.resource("helmreleases")
    addon = await ekresource.first(labels = labels, namespace = cluster_namespace)
    if not addon:
        ekresource = await ekaddons.resource("manifests")
        addon = await ekresource.first(labels = labels, namespace = cluster_namespace)
    if not addon:
        return
    # Add the service to the services annotation for the addon
    # Note that this function will not run concurrently for two reservations on the same
    # cluster, so this is a safe operation
    annotations = addon.metadata.get("annotations", {})
    services = json.loads(annotations.get("azimuth.stackhpc.com/services", "{}"))
    if service_status:
        services[service_name] = service_status.dict()
    else:
        services.pop(service_name, None)
    return await ekresource.patch(
        addon.metadata.name,
        {
            "metadata": {
                "annotations": {
                    "azimuth.stackhpc.com/services": json.dumps(services),
                }
            }
        },
        namespace = addon.metadata.namespace
    )


def get_service_name(reservation):
    """
    Returns the service name for the reservation.
    """
    name = reservation["metadata"]["name"]
    namespace = reservation["metadata"]["namespace"]
    return name if name.startswith(namespace) else f"{namespace}-{name}"


def get_service_status(reservation):
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
        subdomain = reservation["status"]["subdomain"],
        fqdn = reservation["status"]["fqdn"],
        label = label.strip(),
        icon_url = annotations.get("azimuth.stackhpc.com/service-icon-url"),
        description = annotations.get("azimuth.stackhpc.com/service-description")
    )


@model_handler(
    api.Cluster,
    kopf.daemon,
    cancellation_timeout = 1
)
async def monitor_cluster_services(instance, name, namespace, **kwargs):
    """
    Daemon that monitors Zenith reservations
    """
    if not settings.zenith.enabled:
        return
    if not instance.status.kubeconfig_secret_name:
        raise kopf.TemporaryError("kubeconfig for cluster not set yet")
    eksecrets = await ekclient.api("v1").resource("secrets")
    try:
        kubeconfig = await eksecrets.fetch(
            instance.status.kubeconfig_secret_name,
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
    ekclusterstatus = await ekresource_for_model(api.Cluster, "status")
    async with ekclient_target:
        try:
            ekzenithapi = ekclient_target.api(settings.zenith.api_version)
            ekzenithreservations = await ekzenithapi.resource("reservations")
            initial, events = await ekzenithreservations.watch_list(all_namespaces = True)
            # The initial reservations represent the current known state of the services
            # So we rebuild and replace the full service state for the cluster
            cluster_services = {}
            for reservation in initial:
                if reservation.get("status", {}).get("phase", "Unknown") != "Ready":
                    continue
                service_name = get_service_name(reservation)
                service_status = get_service_status(reservation)
                addon = await annotate_addon_for_reservation(
                    instance,
                    reservation,
                    service_name,
                    service_status
                )
                # If the addon has the Helm chart label, store the service
                if addon and "capi.stackhpc.com/cluster" in addon.metadata.get("labels", {}):
                    cluster_services[service_name] = service_status
            await ekclusterstatus.json_patch(
                name,
                [
                    {
                        "op": "replace",
                        "path": "/status/services",
                        "value": cluster_services,
                    },
                ],
                namespace = namespace
            )
            # For subsequent events, we just need to patch the state of the specified service
            async for event in events:
                event_type, reservation = event["type"], event.get("object")
                if not reservation:
                    continue
                service_name = get_service_name(reservation)
                if event_type in {"ADDED", "MODIFIED"}:
                    if reservation.get("status", {}).get("phase", "Unknown") == "Ready":
                        service_status = get_service_status(reservation)
                        addon = await annotate_addon_for_reservation(
                            instance,
                            reservation,
                            service_name,
                            service_status
                        )
                        if addon and "capi.stackhpc.com/cluster" in addon.metadata.get("labels", {}):
                            await ekclusterstatus.patch(
                                name,
                                {
                                    "status": {
                                        "services": {
                                            service_name: service_status,
                                        },
                                    },
                                },
                                namespace = namespace
                            )
                elif event_type == "DELETED":
                    service_name = get_service_name(reservation)
                    await ekclusterstatus.json_patch(
                        name,
                        [
                            {
                                "op": "remove",
                                "path": f"/status/services/{service_name}",
                            },
                        ],
                        namespace = namespace
                    )
                    await annotate_addon_for_reservation(
                        instance,
                        reservation,
                        service_name
                    )
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
    version_range = easysemver.Range(instance.spec.version_range)
    chart_versions = sorted(
        (
            v
            for v in yaml.safe_load(response.text)["entries"][instance.spec.chart.name]
            if easysemver.Version(v["version"]) in version_range
        ),
        key = lambda v: easysemver.Version(v["version"]),
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
