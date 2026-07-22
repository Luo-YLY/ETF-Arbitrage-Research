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
python scripts/probe_sz_quotation.py --code 159915.SZ
```

适配器每轮只批量读取 ETF 和目标成分股，避免重复拉取整个市场。它能从 `cdate` 和 `ctime`
解析供应商时间，并默认把项目内部六位证券代码映射为 Redis 的 `.SZ` 后缀。Redis 只提供
最新价时，应使用指示性模式：系统可以记录行情和计算最新价 Premium，但不会生成可执行
套利信号。

仓库包含一份可复现的159915真实PCF样本：

```text
data/pcf/20260722/pcf_159915_20260722.xml
```

采集脚本可直接解析深交所PCF，不需要手工制作成分权重CSV。内网设备克隆最新版后先测试3次：

```powershell
$env:SZ_REDIS_HOST="内网地址"
python scripts/record_sz_realtime.py `
  --pcf data/pcf/20260722/pcf_159915_20260722.xml `
  --redis-code-suffix .sz `
  --interval 3 `
  --max-polls 3 `
  --output tmp/recordings/20260722/159915_pcf_test.jsonl
```

确认ETF价格、IOPV和时间持续变化后开始正式记录：

```powershell
python scripts/record_sz_realtime.py `
  --pcf data/pcf/20260722/pcf_159915_20260722.xml `
  --redis-code-suffix .sz `
  --interval 3 `
  --output tmp/recordings/20260722/159915_with_pcf.jsonl
```

默认文件为 `tmp/recordings/YYYYMMDD/159915.jsonl`，相同市场快照会自动去重。PCF模式按
`(Σ成分数量×实时价格 + 预估现金差额) / 申赎单位` 计算IOPV；任一非零数量成分股缺价时，
该时点IOPV会被标记为无效。原有 `--components-csv` 权重模式仅保留用于旧研究数据兼容。

收盘后回放记录并运行指示性回测：

```powershell
python scripts/replay_recording.py `
  --input tmp/recordings/20260722/159915_with_pcf.jsonl `
  --pcf data/pcf/20260722/pcf_159915_20260722.xml `
  --mode indicative
```

脚本会校验PCF的ETF代码、交易日、记录数量和重复证券。只有采集数据确实包含买一卖一时，才使用 `--mode executable` 和采集端的
`--require-bid-ask`。回放结果写入 `outputs/replay/`。

## 研究边界

当前回测收益是折溢价头寸的收敛收益，已扣简单双边成本，但尚未覆盖冲击成本、申赎时滞、
最小申赎单位、现金替代、印花税、融券可得性、指数期货对冲及资金占用。Dashboard 默认数据
为确定性模拟行情，仅用于验证系统行为。
