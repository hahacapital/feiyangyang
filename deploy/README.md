# Deploy feiyangyang to ECS Fargate (ap-northeast-1, cluster `ff`)

Personal, no-auth service fronted by the existing **ff.theblueprint.xyz** ALB.

## Prerequisites (confirm these exist)
- AWS CLI v2, Docker (with buildx/QEMU if cross-building arches), and `envsubst`
  (from gettext — macOS: `brew install gettext`). `CPU_ARCH` / `DOCKER_PLATFORM` in
  `.env` must match each other and the build host (this host is aarch64 → ARM64).
- ECS cluster `ff` in `ap-northeast-1`.
- An ALB + HTTPS listener terminating `ff.theblueprint.xyz`, with a host rule
  routing to a **target group** (`target-type: ip`, port 8080, health check `/healthz`).
  Put its ARN in `TARGET_GROUP_ARN`.
- The OHLC S3 bucket `staking-ledger-bpt` (prefix `jojo_quant/ohlc/`) readable from
  this account.

## One-time IAM
```bash
set -a; source deploy/.env; set +a
# Execution role (ECR pull + logs)
aws iam create-role --role-name feiyangyang-exec \
  --assume-role-policy-document file://deploy/iam/trust-policy.json
aws iam attach-role-policy --role-name feiyangyang-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
# Task role (S3 read + self idle-stop)
aws iam create-role --role-name feiyangyang-task \
  --assume-role-policy-document file://deploy/iam/trust-policy.json
envsubst < deploy/iam/task-role-policy.json > /tmp/task-role-policy.json
aws iam put-role-policy --role-name feiyangyang-task \
  --policy-name feiyangyang-s3-ecs --policy-document file:///tmp/task-role-policy.json
```
Put the resulting role ARNs in `deploy/.env`.

## Deploy
```bash
cp deploy/.env.example deploy/.env   # fill in the blanks
bash deploy/deploy.sh
```
First deploy `create-service`s with `healthCheckGracePeriodSeconds=600` so the
1-3 min cold-start warmup isn't health-checked to death; later deploys
`update-service`. Open https://ff.theblueprint.xyz once `/api/status` is `ready`.

## Cost / idle auto-stop
Always-on 2 vCPU / 8 GB ≈ ~$85/mo. To save ~90%, in the UI set an idle window
(`POST /api/idle-policy {minutes}`); the app drops `desiredCount` to 0 when idle.
Wake it next session with `bash deploy/wake.sh` (the app can't wake itself).
