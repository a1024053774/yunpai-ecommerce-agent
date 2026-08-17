# M10-R WP2 预测与补货产品化 — 交付证据

Commit：`699c56a`（feature/m10r-wp2-productized-forecast）

## 交付内容

- `src/ecommerce_agent/forecasting/product.py`
  - `ForecastProductService`：复用 Demand Fact / Forecast Engine / Inventory Planning，
    提供批量 SKU 运行（单 SKU 失败隔离）、单 SKU 显式重跑、只读审核。
  - 只读审核统一返回 forecast / backtest / plan / risks / readiness。
- `src/ecommerce_agent/forecasting_api.py`
  - `POST /v1/forecasting/batch/runs`：批量运行。
  - `POST /v1/forecasting/skus/{sku_id}/rerun`：显式重跑。
  - `GET /v1/forecasting/skus/{sku_id}/review`：只读审核。
- `tests/test_product.py`
  - 6 个用例：空店只读审核、批量失败隔离、单 SKU 重跑、批量/重跑/审核路由，
    以及成功路径 + 确定性 + 新鲜度守卫。

## 测试与门禁

```powershell
$env:NO_PROXY='127.0.0.1,localhost'; $env:no_proxy='127.0.0.1,localhost';
$env:ALL_PROXY='http://127.0.0.1:9'; $env:HTTP_PROXY='http://127.0.0.1:9';
$env:HTTPS_PROXY='http://127.0.0.1:9'
.\.venv\Scripts\python.exe -m pytest tests\test_product.py -q
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

- `pytest tests\test_product.py`：`6 passed`。
- `compileall`：退出 0；`git diff --check`：无输出。

## 验收要点

- 批量运行：无需求事实的 SKU 返回 `failed`，不中断其余 SKU，错误透出 `forecast_history_not_found`。
- 成功路径：虚拟订单/库存 → 需求重建 → 策略配置 → `batch/runs`，产出 forecast（30 个预测点、
  有 champion_model）与 plan（`action_mode=advisory_only`）。
- 确定性：同一 SKU 重跑，`recommended_order_qty` 保持一致；`forecast_run_id` 变化、旧 run 不冒充 current。

## 范围与未完成

- 不新建预测算法，不绕过模型数值；复用 F-317～F-321 引擎与既有的冷启动/缺货降级语义。
- 候选外生信号进入 champion 的无泄漏 rolling backtest 门禁仍待补（WP1 完成条件第 4 条，
  计划中后置到 WP2 信号接入阶段）。
