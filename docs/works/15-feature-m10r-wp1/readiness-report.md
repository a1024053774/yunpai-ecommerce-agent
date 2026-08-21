# M10-R WP1 准备度报告

## 汇总

- 预测目标：missing 1
- 候选信号：missing 4
- 供给约束：missing 2
- 交付约束：missing 2
- 执行主数据：missing 2

## 输入明细

### 预测目标

- 每日需求事实（store+SKU）；证据=missing；来源=-；data_as_of=-；粒度=daily；SKU覆盖=0；缺失原因=无需求事实：等待订单/需求导入（M7-R WP1 导入契约）（未登记 field evidence，按行存在推断）
### 候选信号

- 流量曝光/点击（revision→SKU）；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=0；缺失原因=无流量数据：M9-R/Traffic Lab 接入后可用（未登记 field evidence，按行存在推断）；未使用（WP2 接线）
- 广告投放指标；证据=missing；来源=-；data_as_of=-；粒度=daily；SKU覆盖=-；缺失原因=无广告数据：等待营销指标导入（未登记 field evidence，按行存在推断）；未使用（WP2 接线）
- 竞品信号（approved-only）；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=0；缺失原因=无已批准竞品匹配（D-025 approved-only）（未登记 field evidence，按行存在推断）；未使用（WP2 接线）
- 退款/售后；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=-；缺失原因=无退款/售后记录：等待订单售后导入（SKU 经订单行关联）（未登记 field evidence，按行存在推断）；未使用（WP2 接线）
### 供给约束

- 可售/在途/预留库存；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=0；缺失原因=无库存快照：等待库存导入（未登记 field evidence，按行存在推断）
- 补货策略（lead/review/MOQ/服务水平）；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=0；缺失原因=无补货策略：等待策略配置（未登记 field evidence，按行存在推断）
### 交付约束

- 供应商生产/备货周期；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=0；缺失原因=无补货策略/供应商参数（未登记 field evidence，按行存在推断）
- 运输周期；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=-；缺失原因=运输周期未接入（M7-R 财务/物流输入）（未登记 field evidence，按行存在推断）
### 执行主数据

- 商品/SKU；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=0；缺失原因=无商品主数据：等待 catalog 导入（未登记 field evidence，按行存在推断）
- 内部料号映射；证据=missing；来源=-；data_as_of=-；粒度=-；SKU覆盖=-；缺失原因=料号映射未完成（M7-R WP3）（未登记 field evidence，按行存在推断）
