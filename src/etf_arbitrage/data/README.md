# Data Layer

`models.py` 定义统一的 ETF 行情、股票行情、基金主数据和成分权重结构。

`DataFeed` 是供应商无关接口。`DataFrameReplayFeed` 用于 CSV/DataFrame 历史回放，
`SyntheticDataFeed` 提供可重复的研究样例。接入 Wind、Tushare、内网Redis或券商接口时，
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
默认字段映射支持 `closepx`、`bidpx1`、`askpx1`、`volume`、`amount`，时间优先读取
`timestamp`，缺失时组合 `cdate` 与 `ctime`。若没有买一卖一，可将
`require_bid_ask=False` 用于指示性记录；最新价不会被伪装成买一卖一。
`SZRedisDataFeed` 默认将项目内部的六位代码加上 `.SZ` 后缀再查询 Redis，输出的
`MarketSnapshot` 仍使用六位标准代码。

`recording.py` 中的 `JsonlSnapshotStore` 将一个ETF及其成分股的同一时点行情原子化地写入
一条JSONL记录，自动跳过完全相同的市场快照。原始记录可重新转换为ETF和成分股DataFrame，
再交给 `DataFrameReplayFeed` 回放。采集文件位于被Git忽略的 `tmp/`，不会上传内网行情。

`pcf.py` 使用Python标准库解析深交所PCF XML，并校验命名空间、ETF代码、交易日、申赎单位、
记录数量、重复证券、现金替代标志和数值类型。`PCFDocument.component_weights()`产生行情采集
代码列表；这些值是篮子数量，不应解释为指数权重。

生产环境应优先使用交易所 PCF/申购赎回清单中的组合证券数量与现金替代标志；当前 v1.0
按归一化权重进行理论价值研究。
