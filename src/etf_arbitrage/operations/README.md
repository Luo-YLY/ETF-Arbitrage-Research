# Operations

该模块负责把研究引擎接入每日运行流程：

- `SZSEMarketSchedule` 识别深市连续竞价时段（09:30-11:30、13:00-15:00）；
- `MarketMonitorJob` 固化当日ETF、PCF和采集间隔；
- `MarketMonitorController` 从Streamlit启动独立后台进程，并通过状态文件和停止标志控制任务；
- 后台任务的运行状态、日志、原始快照和派生观察均写在 `tmp/`，不会进入Git。

看板退出不会中断已经启动的采集器。停止按钮采用标志文件让采集器在当前轮完成后正常退出，
收盘后采集器也会自动结束。
