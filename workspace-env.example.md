# 统一 Agent 工作台启动配置模板

> 这是可提交、可转交的无密钥模板。将占位符替换为测试环境凭据后，在仓库根目录复制整个代码块执行。
> 真实版本应保存为 `workspace-env.md`；该文件已被 `.gitignore` 排除，不得提交到 GitHub。

```bash
export ADMIN_API_KEY="<ADMIN_API_KEY>"
export ADMIN_AUTH_REQUIRED="true"
export DATA_DIR="$PWD/data-workspace-agent"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="$NO_PROXY"
export BOOTSTRAP_ADMIN_ID="<BOOTSTRAP_ADMIN_ID>"
export AUTH_REQUIRED="true"
export BOOTSTRAP_TENANT_ID="<BOOTSTRAP_TENANT_ID>"
export BOOTSTRAP_CLIENT_ID="<BOOTSTRAP_CLIENT_ID>"
export BOOTSTRAP_CLIENT_KEY="<BOOTSTRAP_CLIENT_KEY>"
export SUBJECT_HASH_KEY="<SUBJECT_HASH_KEY>"

export MODEL_PROVIDER="<MODEL_PROVIDER>"
export MODEL_BASE_URL="<MODEL_BASE_URL>"
export MODEL_NAME="<MODEL_NAME>"
export MODEL_MAX_OUTPUT_TOKENS="4096"
export MODEL_DECISION_TIMEOUT_SECONDS="15"
export MODEL_DECISION_MAX_OUTPUT_TOKENS="300"
export MODEL_DECISION_THINKING_ENABLED="false"
export MODEL_ENABLED="true"
export CUSTOMER_TEST_ENABLED="true"
export MODEL_API_KEY="<MODEL_API_KEY>"

# 当前统一工作台使用的通用模型/会话参数；工作台最近消息窗口仍由后端固定为 12 条。
export MODEL_MOCK_MODE="false"
export MODEL_TIMEOUT_SECONDS="45"
export MODEL_TEMPERATURE="0.2"
export MODEL_THINKING_ENABLED="false"
export MODEL_STREAMING="true"
export MODEL_RETRY_ATTEMPTS="1"
export SESSION_HISTORY_LIMIT="6"

# 仅供下面启动命令使用，不是应用配置项。
WORKSPACE_HOST="127.0.0.1"
WORKSPACE_PORT="8091"

if [ ! -x .venv/bin/python ]; then
  echo "缺少 .venv：请先执行 python3 -m venv .venv && .venv/bin/python -m pip install -e '[dev]'"
else
  WORKSPACE_PROBE_OK="true"
  if [ "$MODEL_ENABLED" = "true" ] && [ "$MODEL_MOCK_MODE" != "true" ]; then
    PYTHONPATH=src .venv/bin/python -m ecommerce_agent.cli model-probe || WORKSPACE_PROBE_OK="false"
  fi
  if [ "$WORKSPACE_PROBE_OK" != "true" ]; then
    echo "真实模型探针失败，统一工作台未启动"
  elif ! PYTHONPATH=src .venv/bin/python -m ecommerce_agent.cli init; then
    echo "初始化失败，统一工作台未启动"
  elif curl -fsS "http://$WORKSPACE_HOST:$WORKSPACE_PORT/health" >/dev/null 2>&1; then
    echo "统一工作台已在 http://$WORKSPACE_HOST:$WORKSPACE_PORT/admin 运行，请勿重复启动"
  else
    PYTHONPATH=src .venv/bin/python -m ecommerce_agent.cli serve --host "$WORKSPACE_HOST" --port "$WORKSPACE_PORT"
  fi
fi
```

页面入口：

- 统一工作台：`http://127.0.0.1:8091/admin`
- 原高级管理控制台：`http://127.0.0.1:8091/admin/advanced`

只做离线页面/API smoke 时，先把上面代码块中的两项改为：

```bash
export MODEL_ENABLED="false"
export MODEL_MOCK_MODE="true"
```

`eval` 和 `simulate-store` 不是启动前置；后者会向当前 `DATA_DIR` 写入虚拟数据。
