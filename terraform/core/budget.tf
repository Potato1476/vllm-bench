# Four thresholds rather than one. The point is not to learn that the credit is gone --
# it is to learn early enough that a cut plan still has something to cut. 180 USD on a
# 200 USD budget with a ~10 USD contingency leaves almost no room to react, which is
# exactly why the lower alerts matter more than the last one.
resource "aws_budgets_budget" "lab" {
  name         = "${var.project_tag}-lab-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    # Expressed as dollar thresholds, not percentages, so the alert points do not move
    # if the ceiling is ever renegotiated.
    for_each = [50, 100, 150, 180]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_emails
    }
  }

  # Cost Explorer data lags roughly 24 hours. A forecast alert is the only one that can
  # fire before the money is already spent.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = var.budget_limit_usd
    threshold_type             = "ABSOLUTE_VALUE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_emails
  }

  lifecycle {
    precondition {
      condition     = length(var.budget_emails) > 0
      error_message = "budget_emails must list at least one address, otherwise the alerts go nowhere and the budget is decorative."
    }
  }
}

# NOT INCLUDED, and owned by Minh (plan section 8.1): the EventBridge + Lambda uptime
# probe that produces the only evidence for the TC1 uptime number. It belongs in this
# tier because it must outlive the cluster, but its payload depends on the gateway's
# Service address and API key format, which do not exist until week 2. The seam is:
# add aws_lambda_function, aws_iam_role, aws_scheduler_schedule here, and give the
# function the VPC config for the two public subnets plus a security group that can
# reach the gateway.
