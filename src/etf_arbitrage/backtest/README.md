# Backtest

`ResearchReplay` 将历史快照依次送入 IOPV、Premium Monitor、Risk Engine 和 Signal Engine。
`PremiumBacktester` 模拟价差头寸：ETF 溢价时做空溢价，ETF 折价时做多折价，研究重点是
偏离是否收敛、多久收敛以及收敛前还会恶化多少。

回测有两种明确模式：

- `indicative`：使用最新价或中间价Premium，允许忽略唯一的 `missing_bid_ask` 风险阻断；
- `executable`：开仓必须使用 `premium_at_bid` 或 `discount_at_ask`，缺少买一卖一时不会交易。

两种模式的指数变化均为Premium收敛代理，不代表ETF申赎组合的真实账户净值。

当前主要输出：

- 收敛交易数、收敛率，以及30秒、60秒、300秒内收敛率；
- 收敛时间中位数与P90；
- 每笔交易的毛价差捕获、成本、净价差捕获；
- 最大不利偏移MAE和最大有利偏移MFE；
- 6bp、15bp、30bp、50bp等往返成本敏感性。

原收益、Sharpe和最大回撤计算仍保留作兼容，但控制台不再把它们作为核心结论；其中最大回撤
只表示“折溢价收敛研究指数回撤”。模型尚未模拟真实申赎现金流、冲击成本、现金替代和券源，
因此结果属于收敛机制研究，不是可交易业绩。
