# Configured from the EKS module outputs rather than a kubeconfig file, so the stack works
# on a fresh machine. `exec` fetches a token per operation instead of baking a 15-minute
# one into state.
#
# On a first apply against a cluster that does not exist yet this provider cannot be
# configured. Apply in two passes:
#   terraform apply -target=module.eks
#   terraform apply
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", local.region]
  }
}

# EKS ships a default StorageClass backed by gp2, the previous EBS generation: lower
# baseline throughput and IOPS tied to volume size. gp3 is cheaper per GiB and faster.
#
# After moving the model weights to instance-store NVMe, the only thing that still claims
# a volume through this class is Prometheus. That PVC is why `make lab-down` must snapshot
# metrics to S3 first -- the volume dies with the cluster.
resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner = "ebs.csi.aws.com"

  # EBS volumes are zonal. Binding late lets the scheduler place the pod first and then
  # create the volume in that node's AZ, instead of stranding a pod against a volume in
  # the wrong zone.
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  reclaim_policy         = "Delete"

  parameters = {
    type      = "gp3"
    encrypted = "true"
  }

  depends_on = [module.eks]
}
