# 深市 ETF 单市场套利研究系统

ETF Arbitrage v1.0 是一个面向研究的 Python 原型，用统一数据接口完成 ETF 理论价值计算、
折溢价监控、机会识别、风险阻断、历史回放和价差头寸回测。第一阶段覆盖深交所 ETF，默认
展示 159919、159915 和 159922，后续可沿同一接口扩展沪市、沪深港和 QDII ETF。

## 核心口径

当前权重模型按以下公式计算：

```text
IOPV(t) = sum(normalized_weight_i * component_price_i(t)) + Cash / Shares
Premium(t) = (ETF_mid(t) - IOPV(t)) / IOPV(t)
```

系统另外计算 ETF bid 对应的溢价套利边界和 ETF ask 对应的折价套利边界。信号使用可执行
边界，而不是只使用中间价偏离。停牌、涨跌停、缺价和流动性不足不会被静默忽略，它们会
形成风险快照并可阻断信号。

生产 IOPV 应进一步使用交易所 PCF 中的组合证券数量、现金替代标志、预估现金差额、申赎
单位和实时汇率。v1.0 的权重模型用于验证研究架构，不应视为交易所官方 IOPV 的复刻。

## 项目结构

```text
ETF_Arbitrage_Project/
├── src/etf_arbitrage/
│   ├── data/          # 数据模型、DataFeed、历史回放和模拟行情
│   ├── valuation/     # IOPV 估值与质量诊断
│   ├── monitor/       # 折溢价、持续时间与修复速度
│   ├── signal/        # 固定阈值和 Z-score 信号
│   ├── risk/          # 停牌、涨跌停和流动性风险
│   ├── backtest/      # 统一回放与价差头寸模拟
│   ├── dashboard/     # Streamlit 研究看板
│   └── utils/         # 绩效统计
├── config/            # 示例研究参数
├── scripts/           # 命令行演示
├── tests/             # 基础与集成测试
└── streamlit_app.py   # 看板入口
```

采用 `src/etf_arbitrage/signal` 命名空间而非项目根目录的 `signal` 包，是为了避免覆盖 Python
标准库同名模块。

## 快速开始

推荐使用独立的 Python 3.14 Conda 环境，避免修改 Anaconda base：

```powershell
cd ETF_Arbitrage_Project
conda env create -f environment.yml
conda activate etf-arbitrage-py314
python scripts/run_demo.py
python -m streamlit run streamlit_app.py
```

当前工作区也可将环境直接创建在 `.conda/py314`，通过
`.\.conda\py314\python.exe` 使用，无需修改全局环境。两个运行入口会自动加载本地 `src` 目录。

## 数据接入

实时或历史供应商只需实现 `DataFeed` 的三个方法：基金主数据、成分权重和按时间排序的行情
快照。估值、监控、风险、信号与回测均不依赖具体供应商。

推荐的正式接入顺序：交易所 PCF 和基金主数据，ETF/成分股实时行情，停复牌与涨跌停状态，
盘口深度和费率，最后再接申赎执行与券源约束。

### 深市 Redis 行情入口

项目已提供只读的 `SZRedisQuotationClient` 和 `SZRedisDataFeed`。Redis 连接信息通过
`SZ_REDIS_HOST`、`SZ_REDIS_PORT`、`SZ_REDIS_DB`、`SZ_REDIS_PASSWORD` 和
`SZ_REDIS_TIMEOUT` 环境变量配置，仓库不保存内网地址或凭据。

首次连接内网后，先安装可选依赖并探测 159915 的真实字段：

```powershell
python -m pip install -e ".[redis]"
$env:SZ_REDIS_HOST="内网地址"
python scripts/probe_sz_quotation.py --code 159915
```

当前适配器一次读取一个 Redis 全市场快照。持续轮询、断线重连和交易日服务将在确认真实
行情结构后实现。

## 研究边界

当前回测收益是折溢价头寸的收敛收益，已扣简单双边成本，但尚未覆盖冲击成本、申赎时滞、
最小申赎单位、现金替代、印花税、融券可得性、指数期货对冲及资金占用。Dashboard 默认数据
为确定性模拟行情，仅用于验证系统行为。
