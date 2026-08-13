#!/usr/bin/env bash
set -e

# ==============================================================================
# AlphaEvolve SQL Optimizer 一键环境配置与初始化脚本
# ==============================================================================

echo "============================================================"
echo "  AlphaEvolve SQL Optimizer 一键配置向导"
echo "============================================================"

# 1. 检查 gcloud 工具
if ! command -v gcloud &> /dev/null; then
    echo "❌ 错误: 未检测到 gcloud CLI，请先安装 Google Cloud SDK:"
    echo "   https://docs.cloud.google.com/sdk/docs/install-sdk"
    exit 1
fi

# 2. 检查与配置本地凭据 (ADC)
echo "🔹 [1/6] 检查本地 Google Cloud 认证凭据..."
if ! gcloud auth application-default print-access-token &>/dev/null; then
    echo "👉 正在引导登录本地凭据 (gcloud auth application-default login)..."
    if [ -e /dev/tty ]; then
        gcloud auth application-default login < /dev/tty
    else
        gcloud auth application-default login
    fi
fi
echo "✅ 本地凭据认证有效"

# 3. 获取 GCP Project ID / Project Number
if [ -z "${PROJECT_ID:-}" ]; then
    CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || true)
    if [ -n "$CURRENT_PROJECT" ] && [ "$CURRENT_PROJECT" != "(unset)" ]; then
        if [ -e /dev/tty ]; then
            read -r -p "使用当前 GCP 项目 [$CURRENT_PROJECT]? (Y/n / 输入新 Project ID): " input_proj < /dev/tty || true
        elif [ -t 0 ]; then
            read -r -p "使用当前 GCP 项目 [$CURRENT_PROJECT]? (Y/n / 输入新 Project ID): " input_proj || true
        else
            input_proj="Y"
        fi
        input_proj=${input_proj:-Y}
        if [ "$input_proj" = "Y" ] || [ "$input_proj" = "y" ]; then
            PROJECT_ID="$CURRENT_PROJECT"
        else
            PROJECT_ID="$input_proj"
        fi
    else
        if [ -e /dev/tty ]; then
            read -r -p "请输入您的 GCP Project ID 或数字 Project Number: " PROJECT_ID < /dev/tty || true
        elif [ -t 0 ]; then
            read -r -p "请输入您的 GCP Project ID 或数字 Project Number: " PROJECT_ID || true
        fi
    fi
fi

if [ -z "$PROJECT_ID" ]; then
    echo "❌ 错误: GCP Project ID 不能为空。"
    exit 1
fi

# 自动将字符串 Project ID 转换为 Discovery Engine API 所需的数字 Project Number
if ! [[ "$PROJECT_ID" =~ ^[0-9]+$ ]]; then
    echo "🔹 正在将 Project ID ($PROJECT_ID) 解析为 Numeric Project Number..."
    RESOLVED_NUM=$(python3 -c "
import urllib.request, json, subprocess, sys
try:
    token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()
    req = urllib.request.Request('https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID}', headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
        print(data.get('projectNumber', ''))
except Exception:
    pass
" 2>/dev/null || true)

    if [ -n "$RESOLVED_NUM" ]; then
        echo "✅ 成功解析为数字 Project Number: $RESOLVED_NUM (原 ID: $PROJECT_ID)"
        PROJECT_ID="$RESOLVED_NUM"
    else
        echo "⚠️ 未能自动获取数字编号，继续尝试使用原 ID: $PROJECT_ID"
    fi
fi
echo "✅ 目标项目: $PROJECT_ID"

# 设置配额项目
gcloud auth application-default set-quota-project "$PROJECT_ID" --quiet 2>/dev/null || true

# 4. 启用 Discovery Engine API
echo "🔹 [2/6] 检查/启用 Discovery Engine API (discoveryengine.googleapis.com)..."
gcloud services enable discoveryengine.googleapis.com --project="$PROJECT_ID" 2>/dev/null || true

# 5. 配置 IAM 角色权限 (自动区分 user 与 serviceAccount)
USER_EMAIL=$(gcloud config get-value account 2>/dev/null || python3 -c "import json, os; p=os.path.expanduser('~/.config/gcloud/application_default_credentials.json'); d=json.load(open(p)) if os.path.exists(p) else {}; print(d.get('account',''))" 2>/dev/null)
if [ -n "$USER_EMAIL" ] && [ "$USER_EMAIL" != "(unset)" ]; then
    if [[ "$USER_EMAIL" == *"gserviceaccount.com"* ]]; then
        MEMBER="serviceAccount:$USER_EMAIL"
    else
        MEMBER="user:$USER_EMAIL"
    fi
    echo "🔹 [3/6] 授予 IAM 角色权限 ($MEMBER)..."
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="$MEMBER" \
        --role="roles/discoveryengine.admin" --quiet 2>/dev/null || true

    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="$MEMBER" \
        --role="roles/iam.serviceAccountTokenCreator" --quiet 2>/dev/null || true
else
    echo "🔹 [3/6] 跳过个人账号 IAM 绑定 (使用默认环境服务凭据)"
fi

# 6. 预配 AlphaEvolve Engine 与 Assistant 资源
ENGINE_ID="alpha-evolve-experiment-engine"
echo "🔹 [4/6] 检查并预配 AlphaEvolve Engine 资源 ($ENGINE_ID)..."

ACCESS_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null || gcloud auth print-access-token 2>/dev/null)
BASE_URL="https://discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_ID}/locations/global/collections/default_collection"

# 创建 Engine（若已存在则自动跳过）
curl -s -X POST "${BASE_URL}/engines?engineId=${ENGINE_ID}" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d '{
    "display_name": "'"${ENGINE_ID}"'",
    "data_store_ids": [],
    "solution_type": "SOLUTION_TYPE_GENERATIVE_CHAT"
  }' > /dev/null 2>&1 || true

# 创建 default_assistant
curl -s -X POST "${BASE_URL}/engines/${ENGINE_ID}/assistants?assistantId=default_assistant" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d '{
    "display_name": "default_assistant"
  }' > /dev/null 2>&1 || true

# 7. 安装与配置 ae CLI
echo "🔹 [5/6] 检查与配置 AlphaEvolve CLI (ae)..."
AE_GIT_URL="git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills"

if ! command -v ae &> /dev/null; then
    if command -v uv &> /dev/null; then
        uv tool install --force --default-index https://pypi.org/simple "$AE_GIT_URL" || \
        uv tool install --force "$AE_GIT_URL" || \
        pip install --index-url https://pypi.org/simple "$AE_GIT_URL" || \
        pip install "$AE_GIT_URL"
    else
        pip install --index-url https://pypi.org/simple "$AE_GIT_URL" || pip install "$AE_GIT_URL"
    fi
fi

ae config --project="$PROJECT_ID" \
          --engine="$ENGINE_ID" \
          --location=global \
          --models=gemini-3.5-flash

# 8. 验证云端连通性
echo "🔹 [6/6] 执行 AlphaEvolve API 连通性终检..."
if ae --json config test; then
    echo ""
    echo "============================================================"
    echo "  🎉 恭喜！AlphaEvolve 环境配置已 100% 跑通！"
    echo "============================================================"
    echo "您现在可以在 AI Coding Agent (如 Antigravity / Gemini CLI) 中直接输入："
    echo ""
    echo "  使用 AlphaEvolve 优化这条 PostgreSQL 查询："
    echo "  SELECT * FROM your_table WHERE ...;"
    echo ""
else
    echo ""
    echo "⚠️ 连通性测试未通过 (403 权限不足)。排查建议："
    echo "1. 如果当前使用的是 GCE VM 默认服务账号 ($USER_EMAIL)，请确保项目管理员授予了以下权限："
    echo "   gcloud projects add-iam-policy-binding $PROJECT_ID --member=\"$MEMBER\" --role=\"roles/discoveryengine.admin\""
    echo "   gcloud projects add-iam-policy-binding $PROJECT_ID --member=\"$MEMBER\" --role=\"roles/iam.serviceAccountTokenCreator\""
    echo "2. 或者运行 'gcloud auth application-default login' 登录您个人的 GCP 账号以替代 VM 默认服务账号。"
    echo "3. 确认该 GCP 项目已激活 Gemini Enterprise 许可。"
fi
