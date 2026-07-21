# Data Layer

`models.py` 定义统一的 ETF 行情、股票行情、基金主数据和成分权重结构。

`DataFeed` 是供应商无关接口。`DataFrameReplayFeed` 用于 CSV/DataFrame 历史回放，
`SyntheticDataFeed` 提供可重复的研究样例。接入 Wind、Tushare、AKShare 或券商接口时，
新增 `DataFeed` 实现即可，不需要修改估值、信号和回测模块。

`sz_redis.py` 将原有深市 Redis 行情脚本拆分为两个部分：

- `SZRedisQuotationClient` 只读访问以 `YYYYMMDD` 为键的 Redis 行情哈希；
- `SZRedisDataFeed` 将原始行情字段映射为项目统一的 `MarketSnapshot`。

连接信息只从环境变量读取，不应写入代码或提交到 Git：

```powershell
$env:SZ_REDIS_HOST="内网地址"
$env:SZ_REDIS_PORT="6379"
$env:SZ_REDIS_DB="0"
$env:SZ_REDIS_PASSWORD=""
python scripts/probe_sz_quotation.py --code 159915.SZ
```

恢复内网后应先运行探测脚本，确认 Redis 中真实的买一、卖一、成交量、成交额和时间字段。
默认字段映射为 `closepx`、`bidpx1`、`askpx1`、`volume`、`amount` 和 `timestamp`；若供应商
字段不同，应通过 `SZRedisFieldMap` 显式配置，不能用最新价代替缺失的买一或卖一。
`SZRedisDataFeed` 默认将项目内部的六位代码加上 `.SZ` 后缀再查询 Redis，输出的
`MarketSnapshot` 仍使用六位标准代码。

生产环境应优先使用交易所 PCF/申购赎回清单中的组合证券数量与现金替代标志；当前 v1.0
按归一化权重进行理论价值研究。
