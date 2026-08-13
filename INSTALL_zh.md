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

直接运行一键配置脚本 [setup.sh](https://github.com/yanbasic/alpha-evolve-sql-optimizer/blob/main/setup.sh)（自动完成 ADC 登录、Project Number 解析、API 启用、IAM 授权、Engine 预配及连通性测试）：

```bash
# 方式 A：克隆项目后在根目录运行（推荐）
./setup.sh

# 方式 B：通过 curl 远程一键执行
bash <(curl -sSL https://raw.githubusercontent.com/yanbasic/alpha-evolve-sql-optimizer/main/setup.sh)
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
