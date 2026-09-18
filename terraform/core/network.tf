data "aws_availability_zones" "available" {
  state = "available"

  # Opt-in zones (Local Zones, Wavelength) are not enabled on a fresh account and EKS
  # cannot place managed node groups there.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = "${var.project}-lab"
  cidr = var.vpc_cidr
  azs  = local.azs

  public_subnets = ["10.0.0.0/20", "10.0.16.0/20"]

  # Declared but unused in the lab. They exist so the production topology in the design
  # document can be reached by changing two lines rather than re-addressing the VPC.
  private_subnets = ["10.0.32.0/20", "10.0.48.0/20"]

  # ---------------------------------------------------------------------------
  # BUDGET PROFILE -- NOT A PRODUCTION PATTERN
  #
  # No NAT gateway. It bills ~$0.045/hour whether traffic flows or not, which over six
  # weeks is ~45 USD against a 200 USD budget, and its only job here is letting nodes
  # pull images. Nodes therefore sit in public subnets with public IPs.
  #
  # The compensation is that no inbound port is opened anywhere: the API server is
  # restricted to the team's CIDRs, admin access is through SSM, and the node security
  # group carries no CIDR-based ingress rule at all.
  # ---------------------------------------------------------------------------
  enable_nat_gateway = false

  # Managed node groups in a public subnet only reach the internet if the subnet assigns
  # a public IP at launch. Without this the nodes come up and never join the cluster.
  map_public_ip_on_launch = true

  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }
}

# Gateway endpoints have no hourly and no per-GB charge. Every pod start pulls the model
# weights from S3, so this keeps that traffic on the AWS backbone rather than out through
# the internet gateway. It attaches to the PUBLIC route tables because that is where the
# nodes are; pointing it at the private tables would leave it serving nothing.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(module.vpc.public_route_table_ids, module.vpc.private_route_table_ids)

  tags = {
    Name = "${var.project}-lab-s3"
  }
}

# Deliberately absent: interface endpoints for ECR, STS, CloudWatch Logs and KMS. Each
# bills $0.01/hour PER AZ, so the five the production design needs would be $0.10/hour --
# about 16 USD over the lab's cluster hours, for traffic that reaches the internet
# gateway for free. The production design in the report does include them, because there
# the nodes are in private subnets and there is no other way out.
