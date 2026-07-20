# Signal

信号只识别机会，不连接委托执行。

`FixedThresholdSignal` 使用 ETF bid/ask 后的可执行价差，避免把中间价偏离误认为可成交收益。
`ZScoreSignal` 使用滚动均值和标准差识别极端偏离。两类信号都接受 `RiskSnapshot`，并保留
“检测到价差但被风险阻断”的信息。
