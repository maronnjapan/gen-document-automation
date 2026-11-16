#!/usr/bin/env bash
# 3つのコンテナイメージ（生成・レビュー・Web）をビルドして Amazon ECR に登録し、AWS SAM でデプロイする。
#
# 使い方:
#   export SAM_PARAMS='RedisUrl=rediss://... LlmUrl=https://... LlmApiKey=... ConfluenceUrl=https://... ConfluenceToken=... SlackWebhookUrl=https://...'
#   STACK=doc-automation REGION=ap-northeast-1 ./infra/deploy.sh
#
# 必要なもの: aws CLI（認証済み）、docker、sam CLI
set -euo pipefail
cd "$(dirname "$0")/.."

STACK=${STACK:-doc-automation}
REGION=${REGION:-ap-northeast-1}
TAG=${TAG:-$(git rev-parse --short HEAD)}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

declare -A IMAGES
for app in generator reviewer api; do
  repo="$STACK-$app"
  aws ecr describe-repositories --repository-names "$repo" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$repo" --region "$REGION" --image-scanning-configuration scanOnPush=true >/dev/null
  IMAGES[$app]="$REGISTRY/$repo:$TAG"
  docker build --platform linux/amd64 --provenance=false -f "infra/Dockerfile.$app" -t "${IMAGES[$app]}" .
  docker push "${IMAGES[$app]}"
done

# shellcheck disable=SC2086  # SAM_PARAMS は "Key=Value Key=Value" 形式で分割して渡す
sam deploy \
  --template-file infra/template.yaml \
  --stack-name "$STACK" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "GeneratorImageUri=${IMAGES[generator]}" \
    "ReviewerImageUri=${IMAGES[reviewer]}" \
    "ApiImageUri=${IMAGES[api]}" \
    ${SAM_PARAMS:-}

aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query "Stacks[0].Outputs" --output table
