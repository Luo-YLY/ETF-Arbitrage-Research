# Valuation

`IOPVCalculator` 实现权重归一化、现金项、缺失价格策略和数据质量标记。

- `strict`：任一成分缺价即拒绝输出有效 IOPV；
- `reweight`：仅在有价成分间重新归一化，适合研究连续性但会产生模型偏差；
- `fallback`：允许使用调用方提供的昨收/估值价，不自动重分配缺失权重。

停牌成分保留最后成交价用于估值，同时单独标记停牌权重，交由风险模块阻断不可执行机会。

`PCFIOPVCalculator` 是正式PCF研究入口：

```text
IOPV = (sum(ComponentShare_i * Price_i) + EstimateCashComponent)
       / CreationRedemptionUnit
```

PCF篮子数量是固定申赎合同，缺价时不能重分配，因此默认采用严格模式。数量为零的强制现金
替代证券不会要求行情；非零数量成分缺价则返回无效IOPV。当前公式用于理论篮子估值，尚未把
申购和赎回现金替代金额、替代溢价及实际执行费用加入套利现金流。
