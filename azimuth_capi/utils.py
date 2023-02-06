import functools
import json

from pydantic.json import pydantic_encoder

import yaml

from .config import settings


def argo_application(name, cluster, project, source, namespace = None):
    """
    Produces an Argo application for the given source.
    """
    return {
        "apiVersion": settings.argocd.api_version,
        "kind": "Application",
        "metadata": {
            "name": name,
            "namespace": settings.argocd.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "azimuth-capi-operator",
            },
            "annotations": {
                # The annotation is used to identify the associated cluster
                # when an event is observed for the Argo application
                f"{settings.annotation_prefix}/owner-reference": (
                    f"{settings.api_group}:"
                    f"{cluster._meta.plural_name}:"
                    f"{cluster.metadata.namespace}:"
                    f"{cluster.metadata.name}"
                ),
            },
            "finalizers": [settings.argocd.resource_deletion_finalizer],
        },
        "spec": {
            "destination": {
                "name": settings.argocd.target_cluster,
                "namespace": namespace or cluster.metadata.namespace,
            },
            "project": project.metadata.name,
            "source": source,
            "syncPolicy": {
                "automated": {
                    "prune": True,
                    "selfHeal": settings.argocd.self_heal_cluster_apps,
                },
                "syncOptions": [
                    "ApplyOutOfSyncOnly=true",
                    "CreateNamespace=true",
                    "ServerSideApply=true",
                ],
            },
        },
    }


def mergeconcat(defaults, *overrides):
    """
    Returns a new dictionary obtained by deep-merging multiple sets of overrides
    into defaults, with precedence from right to left.
    """
    def mergeconcat2(defaults, overrides):
        if isinstance(defaults, dict) and isinstance(overrides, dict):
            merged = dict(defaults)
            for key, value in overrides.items():
                if key in defaults:
                    merged[key] = mergeconcat2(defaults[key], value)
                else:
                    merged[key] = value
            return merged
        elif isinstance(defaults, (list, tuple)) and isinstance(overrides, (list, tuple)):
            merged = list(defaults)
            merged.extend(overrides)
            return merged
        else:
            return overrides if overrides is not None else defaults
    return functools.reduce(mergeconcat2, overrides, defaults)


def yaml_dump(obj):
    """
    Dumps the given object as YAML with advanced serialization from Pydantic.
    """
    # In order to benefit from correct serialisation of a variety of objects,
    # including Pydantic models, generic iterables and generic mappings, we go
    # via JSON with the Pydantic encoder
    return yaml.safe_dump(
        json.loads(
            json.dumps(
                obj,
                default = pydantic_encoder
            )
        )
    )
