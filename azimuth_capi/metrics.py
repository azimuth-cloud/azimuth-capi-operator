import asyncio
import datetime
import functools

import easykube
from aiohttp import web

from .config import settings


class Metric:
    # The prefix for the metric
    prefix = None
    # The suffix for the metric
    suffix = None
    # The type of the metric - info or gauge
    type = "info"
    # The description of the metric
    description = None

    def __init__(self):
        self._objs = []

    def add_obj(self, obj):
        self._objs.append(obj)

    @property
    def name(self):
        return f"{self.prefix}_{self.suffix}"

    def labels(self, obj):
        """The labels for the given object."""
        return {**self.common_labels(obj), **self.extra_labels(obj)}

    def common_labels(self, obj):
        """Common labels for the object."""
        return {}

    def extra_labels(self, obj):
        """Extra labels for the object."""
        return {}

    def value(self, obj):
        """The value for the given object."""
        return 1

    def records(self):
        """Returns the records for the metric, i.e. a list of (labels, value) tuples."""
        for obj in self._objs:
            yield self.labels(obj), self.value(obj)


class ClusterTemplateMetric(Metric):
    prefix = "azimuth_kube_clustertemplate"

    def common_labels(self, obj):
        return {"template_name": obj.metadata.name}


class AppTemplateMetric(Metric):
    prefix = "azimuth_kube_apptemplate"

    def common_labels(self, obj):
        return {"template_name": obj.metadata.name}


class ClusterMetric(Metric):
    prefix = "azimuth_kube_cluster"

    def common_labels(self, obj):
        return {
            "cluster_namespace": obj.metadata.namespace,
            "cluster_name": obj.metadata.name,
        }


class AppMetric(Metric):
    prefix = "azimuth_kube_app"

    def common_labels(self, obj):
        return {
            "app_namespace": obj.metadata.namespace,
            "app_name": obj.metadata.name,
            "app_template": obj.metadata.labels["azimuth.stackhpc.com/app-template"],
            "target_cluster": obj.spec["clusterName"],
        }

    def records(self):
        """Returns the records for the metric, i.e. a list of (labels, value) tuples."""
        for obj in self._objs:
            if "azimuth.stackhpc.com/app-template" in obj.metadata.labels:
                yield self.labels(obj), self.value(obj)


class ClusterTemplateKubeVersion(ClusterTemplateMetric):
    suffix = "kube_version"
    description = "Kubernetes version for the template"

    def extra_labels(self, obj):
        return {"kube_version": obj.spec["values"]["kubernetesVersion"]}


class ClusterTemplateDeprecated(ClusterTemplateMetric):
    suffix = "deprecated"
    type = "gauge"
    description = "Indicates whether the template is deprecated"

    def value(self, obj):
        return 1 if obj.spec.get("deprecated", False) else 0


class AppTemplateInfo(AppTemplateMetric):
    suffix = "info"
    description = "Basic info for the app template"

    def extra_labels(self, obj):
        return {"chart_repo": obj.spec.chart.repo, "chart_name": obj.spec.chart.name}


class AppTemplateLastSync(AppTemplateMetric):
    suffix = "last_sync"
    type = "gauge"
    description = "The time of the last sync for the app template"

    def value(self, obj):
        last_sync = obj.get("status", {}).get("lastSync")
        return as_timestamp(last_sync) if last_sync else 0


class AppTemplateLatestVersion(AppTemplateMetric):
    suffix = "latest_version"
    description = "The latest version for the template"

    def extra_labels(self, obj):
        versions = obj.get("status", {}).get("versions")
        if versions:
            version = versions[0]["name"]
        else:
            version = "0.0.0"
        return {"version": version}


class AppTemplateVersion(AppTemplateMetric):
    suffix = "version"
    description = "The versions supported by each template"

    def records(self):
        for obj in self._objs:
            labels = super().labels(obj)
            for version in obj.get("status", {}).get("versions", []):
                yield {**labels, "version": version["name"]}, 1


class ClusterTemplate(ClusterMetric):
    suffix = "template"
    description = "Cluster template"

    def extra_labels(self, obj):
        return {"template": obj.spec["templateName"]}


class ClusterKubeVersion(ClusterMetric):
    suffix = "kube_version"
    description = "The Kubernetes version of the cluster"

    def extra_labels(self, obj):
        return {"kube_version": obj.get("status", {}).get("kubernetesVersion", "")}


class ClusterPhase(ClusterMetric):
    suffix = "phase"
    description = "Cluster phase"

    def extra_labels(self, obj):
        return {"phase": obj.get("status", {}).get("phase", "Unknown")}


class ClusterNetworkingPhase(ClusterMetric):
    suffix = "networking_phase"
    description = "Cluster networking phase"

    def extra_labels(self, obj):
        return {"phase": obj.get("status", {}).get("networkingPhase", "Unknown")}


class ClusterControlPlanePhase(ClusterMetric):
    suffix = "control_plane_phase"
    description = "Cluster control plane phase"

    def extra_labels(self, obj):
        return {"phase": obj.get("status", {}).get("controlPlanePhase", "Unknown")}


class ClusterNodeCount(ClusterMetric):
    suffix = "node_count"
    type = "gauge"
    description = "Cluster node count"

    def value(self, obj):
        return obj.get("status", {}).get("nodeCount", 0)


class ClusterNodePhase(ClusterMetric):
    suffix = "node_phase"
    description = "Cluster node phase"

    def records(self):
        for obj in self._objs:
            labels = super().labels(obj)
            for name, node in obj.get("status", {}).get("nodes", {}).items():
                node_labels = {
                    **labels,
                    "node": name,
                    "role": node.get("role", "Unknown"),
                    "group": node.get("nodeGroup", ""),
                    "phase": node.get("phase", "Unknown"),
                }
                yield node_labels, 1


class ClusterNodeKubeletVersion(ClusterMetric):
    suffix = "node_kubelet_version"
    description = "Cluster node kubelet version"

    def records(self):
        for obj in self._objs:
            labels = super().labels(obj)
            for name, node in obj.get("status", {}).get("nodes", {}).items():
                node_labels = {
                    **labels,
                    "node": name,
                    "role": node.get("role", "Unknown"),
                    "group": node.get("nodeGroup", ""),
                    "kubelet_version": node.get("kubeletVersion", ""),
                }
                yield node_labels, 1


class ClusterAddonCount(ClusterMetric):
    suffix = "addon_count"
    type = "gauge"
    description = "Cluster addon count"

    def value(self, obj):
        return obj.get("status", {}).get("addonCount", 0)


class ClusterAddonPhase(ClusterMetric):
    suffix = "addon_phase"
    description = "Cluster addon phase"

    def records(self):
        for obj in self._objs:
            labels = super().labels(obj)
            for name, addon in obj.get("status", {}).get("addons", {}).items():
                addon_labels = {
                    **labels,
                    "addon": name,
                    "phase": addon.get("phase", "Unknown"),
                }
                yield addon_labels, 1


class AppInfo(AppMetric):
    suffix = "info"
    description = "Information about the app"

    def extra_labels(self, obj):
        return {
            "target_namespace": obj.spec["targetNamespace"],
            "release_name": obj.spec["releaseName"],
            "chart_repo": obj.spec.chart.repo,
            "chart_name": obj.spec.chart.name,
            "chart_version": obj.spec.chart.version,
        }


class AppPhase(AppMetric):
    suffix = "phase"
    description = "App phase"

    def extra_labels(self, obj):
        return {"phase": obj.get("status", {}).get("phase", "Unknown")}


def escape(content):
    """Escape the given content for use in metric output."""
    return content.replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")


def as_timestamp(datetime_str):
    """Converts a datetime string to a timestamp."""
    dt = datetime.datetime.fromisoformat(datetime_str)
    return round(dt.timestamp())


def format_value(value):
    """Formats a value for output, e.g. using Go formatting."""
    formatted = repr(value)
    dot = formatted.find(".")
    if value > 0 and dot > 6:
        mantissa = f"{formatted[0]}.{formatted[1:dot]}{formatted[dot + 1:]}".rstrip(
            "0."
        )
        return f"{mantissa}e+0{dot - 1}"
    else:
        return formatted


def render_openmetrics(*metrics):
    """Renders the metrics using OpenMetrics text format."""
    output = []
    for metric in metrics:
        if metric.description:
            output.append(f"# HELP {metric.name} {escape(metric.description)}\n")
        output.append(f"# TYPE {metric.name} {metric.type}\n")

        for labels, value in metric.records():
            if labels:
                labelstr = "{{{0}}}".format(
                    ",".join([f'{k}="{escape(v)}"' for k, v in sorted(labels.items())])
                )
            else:
                labelstr = ""
            output.append(f"{metric.name}{labelstr} {format_value(value)}\n")
    output.append("# EOF\n")

    return (
        "application/openmetrics-text; version=1.0.0; charset=utf-8",
        "".join(output).encode("utf-8"),
    )


METRICS = {
    settings.api_group: {
        "clustertemplates": [
            ClusterTemplateKubeVersion,
            ClusterTemplateDeprecated,
        ],
        "apptemplates": [
            AppTemplateInfo,
            AppTemplateLastSync,
            AppTemplateLatestVersion,
            AppTemplateVersion,
        ],
        "clusters": [
            ClusterTemplate,
            ClusterKubeVersion,
            ClusterPhase,
            ClusterNetworkingPhase,
            ClusterControlPlanePhase,
            ClusterNodeCount,
            ClusterNodePhase,
            ClusterNodeKubeletVersion,
            ClusterAddonCount,
            ClusterAddonPhase,
        ],
    },
    "addons.stackhpc.com": {
        "helmreleases": [
            AppInfo,
            AppPhase,
        ]
    },
}


async def metrics_handler(ekclient, request):
    """Produce metrics for the operator."""
    metrics = []
    for api_group, resources in METRICS.items():
        ekapi = await ekclient.api_preferred_version(api_group)
        for resource, metric_classes in resources.items():
            ekresource = await ekapi.resource(resource)
            resource_metrics = [klass() for klass in metric_classes]
            async for obj in ekresource.list(all_namespaces=True):
                for metric in resource_metrics:
                    metric.add_obj(obj)
            metrics.extend(resource_metrics)

    content_type, content = render_openmetrics(*metrics)
    return web.Response(headers={"Content-Type": content_type}, body=content)


async def metrics_server():
    """Launch a lightweight HTTP server to serve the metrics endpoint."""
    ekclient = easykube.Configuration.from_environment().async_client()

    app = web.Application()
    app.add_routes([web.get("/metrics", functools.partial(metrics_handler, ekclient))])

    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", "8080", shutdown_timeout=1.0)
    await site.start()

    # Sleep until we need to clean up
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.shield(runner.cleanup())
