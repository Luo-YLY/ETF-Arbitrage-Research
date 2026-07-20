# Backtest

`ResearchReplay` 将历史快照依次送入 IOPV、Premium Monitor、Risk Engine 和 Signal Engine。
`PremiumBacktester` 模拟价差头寸：ETF 溢价时做空溢价，ETF 折价时做多折价，收益来自偏离修复。

v1.0 输出总收益、年化收益、Sharpe、最大回撤、胜率和平均持有周期。它尚未模拟申赎时滞、
最小申赎单位、冲击成本、现金替代和融券约束，因此结果属于研究信号验证，不是可交易业绩。
