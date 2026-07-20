# Monitor

`PremiumMonitor` 同时记录中间价折溢价和两条可执行边界：

- 溢价套利边界：`ETF bid / IOPV - 1`；
- 折价套利边界：`IOPV / ETF ask - 1` 的线性等价表达。

监控器维护偏离开始时间、持续秒数、全程最大绝对偏离及最近一次修复耗时。
