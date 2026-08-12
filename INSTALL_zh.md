# AlphaEvolve SQL Optimizer 安装手册

本项目是专为 **AI 编程助手（Coding Agent）** 设计的交互式 SQL 优化技能插件（Skill）。

---

## 1. 准备工作

* **AI Coding Agent**：支持 Skill 扩展的编程助手（如 Antigravity、Gemini CLI、Claude Code、Cursor、Windsurf 等）。
* **Google Cloud SDK**：本地已安装 [`gcloud`](https://docs.cloud.google.com/sdk/docs/install-sdk) 命令行工具。
* **GCP 许可**：目标 GCP 项目已开通 **Gemini Enterprise** 许可。

---

## 2. 安装 Agent 技能 (Skill)

在您的 Coding Agent 中载入本技能：

```bash
# 推荐：通过 skills CLI 一键安装
npx skills add yanbasic/alpha-evolve-sql-optimizer

# 或通过 Git 克隆至技能目录
git clone https://github.com/yanbasic/alpha-evolve-sql-optimizer.git ~/.gemini/config/skills/alpha_evolve_sql_optimizer
```

---

## 3. 一键配置环境与云端资源

在项目根目录下直接运行一键配置脚本（自动完成 ADC 登录、Project Number 解析、API 启用、IAM 授权、Engine 预配及连通性测试）：

```bash
./setup.sh
```

*(也可在任意终端直接运行以下脚本内容)*：

```bash
#!/usr/bin/env bash
set -e

# 1. 检查凭据 (ADC)
if ! gcloud auth application-default print-access-token &>/dev/null; then
    gcloud auth application-default login
fi

# 2. 获取与解析 Project ID / Number
if [ -z "${PROJECT_ID:-}" ]; then
    CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || true)
    read -p "请输入 GCP Project ID [当前: $CURRENT_PROJECT]: " input_proj
    PROJECT_ID="${input_proj:-$CURRENT_PROJECT}"
fi

if ! [[ "$PROJECT_ID" =~ ^[0-9]+$ ]]; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null || true)
    RESOLVED_NUM=$(curl -s "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID}" \
      -H "Authorization: Bearer ${ADC_TOKEN}" | python3 -c "import sys, json; print(json.load(sys.stdin).get('projectNumber',''))" 2>/dev/null || true)
    PROJECT_ID="${RESOLVED_NUM:-$PROJECT_ID}"
fi

gcloud auth application-default set-quota-project "$PROJECT_ID" --quiet 2>/dev/null || true

# 3. 启用 API 与配置权限
gcloud services enable discoveryengine.googleapis.com --project="$PROJECT_ID" 2>/dev/null || true

USER_EMAIL=$(gcloud config get-value account 2>/dev/null || true)
if [ -n "$USER_EMAIL" ] && [ "$USER_EMAIL" != "(unset)" ]; then
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="user:$USER_EMAIL" --role="roles/discoveryengine.admin" --quiet 2>/dev/null || true
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="user:$USER_EMAIL" --role="roles/iam.serviceAccountTokenCreator" --quiet 2>/dev/null || true
fi

# 4. 预配 Engine 资源
ENGINE_ID="alpha-evolve-experiment-engine"
ACCESS_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null || gcloud auth print-access-token 2>/dev/null)
BASE_URL="https://discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_ID}/locations/global/collections/default_collection"

curl -s -X POST "${BASE_URL}/engines?engineId=${ENGINE_ID}" \
  -H "Content-Type: application/json" -H "x-goog-user-project: ${PROJECT_ID}" -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d '{"display_name":"'"${ENGINE_ID}"'","data_store_ids":[],"solution_type":"SOLUTION_TYPE_GENERATIVE_CHAT"}' > /dev/null 2>&1 || true

curl -s -X POST "${BASE_URL}/engines/${ENGINE_ID}/assistants?assistantId=default_assistant" \
  -H "Content-Type: application/json" -H "x-goog-user-project: ${PROJECT_ID}" -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d '{"display_name":"default_assistant"}' > /dev/null 2>&1 || true

# 5. 安装 ae CLI 并配置
AE_GIT_URL="git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills"
if ! command -v ae &> /dev/null; then
    if command -v uv &> /dev/null; then
        uv tool install --force "$AE_GIT_URL"
    else
        pip install "$AE_GIT_URL"
    fi
fi

ae config --project="$PROJECT_ID" --engine="$ENGINE_ID" --location=global --models=gemini-3.5-flash
ae --json config test
```

---

## 4. 验证与使用

打开 **AI Coding Agent**（如 Antigravity / Gemini CLI）对话框，发送包含 **“AlphaEvolve”（请使用全称）** 的优化请求。Agent 将自动检测环境、按需安装数据库驱动并启动交互向导：

```text
使用 AlphaEvolve 优化这条 PostgreSQL 查询：
SELECT
    b.bid,
    b.bbalance,
    (SELECT COUNT(*) FROM pgbench_accounts a WHERE a.bid = b.bid) AS account_count
FROM pgbench_branches b
ORDER BY b.bid;
```
