# AlphaEvolve SQL Optimizer 安装手册

本项目是专为 **AI 编程助手（Coding Agent）** 设计的交互式 SQL 优化技能插件（Skill）。

环境检测、`ae` CLI 安装及数据库驱动将由 Agent 在运行时**自动按需装配**。

---

## 1. 准备工作

在开始前，请确认满足以下基础条件：

1. **AI Coding Agent**：已安装并使用支持 Skill 扩展的编程助手（例如：Antigravity、Gemini CLI、Claude Code、Cursor、Windsurf 等）。
2. **GCP 本地凭据登录**：在终端完成应用默认凭据认证：
   ```bash
   gcloud auth application-default login
   ```
3. **GCP 项目权限与许可**：
   * 目标 GCP 项目已开通 **Gemini Enterprise** 许可。
   * 操作账号已被授予 `roles/discoveryengine.admin` 及 `roles/iam.serviceAccountTokenCreator` 角色。

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

## 3. 验证与使用

打开 **AI Coding Agent**（如 Antigravity / Gemini CLI 等）对话窗口，直接发送包含 **“AlphaEvolve”（请使用全称）** 的优化请求。

Agent 会自动识别目标数据库、在后台自动执行环境预检并装配所需驱动，随后启动交互式优化向导：

### 示例 Prompt
```text
使用 AlphaEvolve 优化这条 PostgreSQL 查询：
SELECT
    b.bid,
    b.bbalance,
    (SELECT COUNT(*) FROM pgbench_accounts a WHERE a.bid = b.bid) AS account_count
FROM pgbench_branches b
ORDER BY b.bid;
```
