from easykube import ResourceSpec


API_VERSION = "v1beta1"


Cluster = ResourceSpec(f"cluster.x-k8s.io/{API_VERSION}", "clusters", "Cluster", True)
KubeadmControlPlane = ResourceSpec(
    f"controlplane.cluster.x-k8s.io/{API_VERSION}",
    "kubeadmcontrolplanes",
    "KubeadmControlPlane",
    True
)
MachineDeployment = ResourceSpec(
    f"cluster.x-k8s.io/{API_VERSION}",
    "machinedeployments",
    "MachineDeployment",
    True
)
Machine = ResourceSpec(f"cluster.x-k8s.io/{API_VERSION}", "machines", "Machine", True)
