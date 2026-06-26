#!/usr/bin/env bash
# Build -> push to ECR -> register task def -> create/update the ECS service.
# Requires AWS CLI v2 with credentials, and a filled deploy/.env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; source deploy/.env; set +a

command -v envsubst >/dev/null || { echo "Install gettext (provides envsubst)"; exit 1; }

REPO_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "==> Login to ECR"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION" >/dev/null

echo "==> Build & push ${REPO_URI}:${IMAGE_TAG} (${DOCKER_PLATFORM})"
docker build --platform "${DOCKER_PLATFORM}" -t "${REPO_URI}:${IMAGE_TAG}" .
docker push "${REPO_URI}:${IMAGE_TAG}"

echo "==> Ensure log group exists (awslogs driver won't create it without logs:CreateLogGroup)"
aws logs create-log-group --log-group-name /ecs/feiyangyang --region "$AWS_REGION" 2>/dev/null || true

echo "==> Register task definition"
TD=$(envsubst < deploy/task-definition.json)
TASK_DEF_ARN=$(aws ecs register-task-definition --region "$AWS_REGION" \
  --cli-input-json "$TD" --query 'taskDefinition.taskDefinitionArn' --output text)
echo "    $TASK_DEF_ARN"

NET="awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUP}],assignPublicIp=${ASSIGN_PUBLIC_IP}}"
LB="targetGroupArn=${TARGET_GROUP_ARN},containerName=web,containerPort=8080"

if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
     --region "$AWS_REGION" --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  echo "==> Update existing service"
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --task-definition "$TASK_DEF_ARN" --desired-count 1 --region "$AWS_REGION" >/dev/null
else
  echo "==> Create service (behind the ff.theblueprint.xyz target group)"
  aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
    --task-definition "$TASK_DEF_ARN" --desired-count 1 --launch-type FARGATE \
    --health-check-grace-period-seconds 600 \
    --deployment-configuration "minimumHealthyPercent=0,maximumPercent=100" \
    --network-configuration "$NET" --load-balancers "$LB" --region "$AWS_REGION" >/dev/null
fi
echo "==> Done. Watch: aws ecs describe-services --cluster $ECS_CLUSTER --services $ECS_SERVICE --region $AWS_REGION"
