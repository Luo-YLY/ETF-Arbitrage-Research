# Risk

`RiskEngine` 将成分股停牌/缺价、涨跌停、成分流动性、ETF 成交额和买卖价差转换为
可解释的风险快照。`blocked=True` 表示理论价差存在，但组合证券或 ETF 端不满足可执行条件。

阈值集中在 `RiskConfig`，正式研究应按 ETF、交易时段和交易规模分别校准。
