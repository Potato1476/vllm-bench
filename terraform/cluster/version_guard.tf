# THE COMMENT ON var.k8s_version WAS RIGHT, AND IT DID NOTHING.
#
# It said, in so many words: keep the control plane on a version in STANDARD support,
# because extended support raises the cluster fee from $0.10 to $0.60 per hour. The value
# underneath it was "1.31", which left standard support on 2025-11-26. Nobody changed
# anything; the version simply aged past its date while the warning sat there.
#
# Measured cost of that, from Cost Explorer on 2026-10-07 (gross usage, before credits):
#
#     USE1-AmazonEKS-Hours:perCluster        38.0 h   $ 3.80   $0.10/h
#     USE1-AmazonEKS-Hours:extendedSupport   38.0 h   $19.01   $0.50/h
#
# $19.01 of surcharge -- 22% of everything the project had spent -- for a version that
# gave nothing over a current one. It was found while finalising the FinOps numbers, which
# is the worst possible moment: TC2's curve assumes $0.10/h for the control plane, so the
# platform as actually deployed was quietly more expensive than the analysis said.
#
# A comment cannot notice a date passing. This asks AWS, at plan time, what support status
# the chosen version has TODAY, and refuses to plan if it is not standard. The failure
# happens before anything is created, on the next run after the date, which is exactly
# when someone is able to act on it.
#
# allow_extended_support exists for the one legitimate case -- a forced stay on an old
# version while an upgrade is prepared -- and makes paying six times an explicit decision
# visible in the diff rather than a default nobody chose.

# Filtered in HCL below, NOT through the data source's own cluster_versions_only argument.
# With provider 5.100.0 that argument filters nothing: asked for ["1.35"] it returned all
# seven versions, three of them in extended support, so a guard trusting it refused EVERY
# version including the current ones. Found by testing that 1.35 PASSES, not only that
# 1.31 fails -- the 1.31 test alone made the broken guard look correct.
data "aws_eks_cluster_versions" "all" {}

locals {
  chosen_version_status = [
    for v in data.aws_eks_cluster_versions.all.cluster_versions :
    v.version_status if v.cluster_version == var.k8s_version
  ]
}

resource "terraform_data" "k8s_version_in_standard_support" {
  lifecycle {
    precondition {
      # The length check matters: alltrue([]) is true, so a version AWS does not list at
      # all -- a typo, or one already past end of life -- would otherwise pass silently.
      condition = var.allow_extended_support || (
        length(local.chosen_version_status) > 0 &&
        alltrue([for s in local.chosen_version_status : s == "STANDARD_SUPPORT"])
      )
      error_message = join("", [
        "Kubernetes ${var.k8s_version} is not in EKS standard support ",
        "(status: ${length(local.chosen_version_status) > 0 ? join(",", local.chosen_version_status) : "not listed by AWS"}). ",
        "Extended support bills $0.60/h per cluster instead of $0.10/h. ",
        "Raise k8s_version, or set allow_extended_support = true to pay it deliberately. ",
        "List current status: aws eks describe-cluster-versions --region us-east-1",
      ])
    }
  }
}
