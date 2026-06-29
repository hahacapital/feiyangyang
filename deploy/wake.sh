#!/usr/bin/env bash
# Wake the service after idle auto-stop set desiredCount=0.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; source deploy/.env; set +a
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --desired-count 1 --region "$AWS_REGION" >/dev/null
echo "Waking $ECS_SERVICE — allow 1-3 min for cache warmup, then load ff.theblueprint.xyz"
