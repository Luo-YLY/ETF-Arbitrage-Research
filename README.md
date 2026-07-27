# 深市 ETF 单市场套利研究系统

ETF Arbitrage v1.0 是一个面向研究的 Python 原型，用统一数据接口完成 ETF 理论价值计算、
折溢价监控、机会识别、风险阻断、历史回放和价差头寸回测。第一阶段覆盖深交所 ETF，默认
优先监控 159915、159901、159949 和 159903，后续可沿同一接口扩展沪市、沪深港和 QDII ETF。

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
│   ├── dashboard/     # Streamlit 每日研究控制台
│   ├── dashboard_panel/ # Panel + Bokeh 双页面研究控制台
│   ├── operations/    # 交易时段与后台采集任务控制
│   └── utils/         # 绩效统计
├── config/            # 示例研究参数
├── scripts/           # 命令行演示
├── tests/             # 基础与集成测试
├── streamlit_app.py   # Streamlit 看板入口
└── panel_app.py       # Panel 双页面控制台入口
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
python -m panel serve panel_app.py --address 127.0.0.1 --port 8505
```

当前工作区也可将环境直接创建在 `.conda/py314`，通过
`.\.conda\py314\python.exe` 使用，无需修改全局环境。各运行入口会自动加载本地 `src` 目录。

Panel 控制台打开地址为 `http://127.0.0.1:8505/panel_app`，直接进入套利模拟可使用
`http://127.0.0.1:8505/panel_app?page=executable`。控制台分为“实盘均值回复监控”和
“实盘套利模拟”两个独立页面。前者接入 PCF、内网 Redis 最新价采集、观察文件回放和收盘
折溢价收敛研究；后者接入 PCF、模拟行情、上传/本地历史行情或标准化 Redis 快照，使用 ETF
与成分股 Bid/Ask、多档深度完成纸面申赎套利、订单成交、一级市场申赎和 PnL 模拟。两页
均复用持久化 Bokeh 数据源进行增量刷新，切页时会停止隐藏页面的定时任务。均值回复页默认
选择当天并允许通过日历选择任意日期；页面和标签内容按需加载，不再一次创建全部隐藏组件。

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
  --redis-code-suffix .SZ `
  --interval 3 `
  --max-polls 3 `
  --output tmp/recordings/20260722/159915_pcf_test.jsonl
```

确认ETF价格、IOPV和时间持续变化后开始正式记录：

```powershell
python scripts/record_sz_realtime.py `
  --pcf data/pcf/20260722/pcf_159915_20260722.xml `
  --redis-code-suffix .SZ `
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

## 每日看板流程

看板支持 159915、159901、159949、159903 的半自动工作流。启动方式：

```powershell
$env:SZ_REDIS_HOST="内网地址"
python -m streamlit run streamlit_app.py
```

盘前在可访问外网的账户中选择交易日和ETF。项目已为四只深市ETF预置深交所PCF直链模板，
通常直接点击“下载并校验PCF”即可。看板也兼容深交所 `eft_download_new.html` 下载页面链接，
会自动解析并尝试对应的XML/TXT文件；仍可上传已经下载的XML或ZIP作为兜底。系统会校验
证券代码、交易日、申赎单位和成分数量后才落盘。自定义下载地址可保存在本机的
`config/pcf_sources.local.json`，该文件不会提交到Git；地址支持 `{etf_code}`、
`{trade_date}` 和 `{trade_date_dash}` 占位符。下载器会在深交所公开配置的主用
`reportdocs.static.szse.cn` 与备用 `reportdocs.static.sse.org.cn` 文件域名之间自动切换。

切换回内网账户后，确认四只ETF的当日PCF均显示“已校验”，再点击“启动当日采集”。后台
进程只在 09:30-11:30 和 13:00-15:00 轮询Redis，午间等待，15:00后自动退出。原始快照写入
`tmp/recordings/YYYYMMDD/`，实时价格、IOPV和Premium写入 `tmp/observations/YYYYMMDD/`。

收盘后进入“收盘回测”，选择ETF并设置开仓阈值、平仓阈值、最长持有快照数和成本。系统
主要报告收敛率、30/60/300秒收敛率、收敛时间、MAE/MFE、逐笔价差捕获和往返成本敏感性。
当前Redis没有买一卖一时应使用“指示性（最新价）”；图中的收敛研究指数不是账户净值，
价差指数回撤也不是实盘组合最大回撤。

收盘回测提供上下联动的 Bokeh 图：上图展示折溢价、阈值、开平仓点和完整持仓周期，下图展示
对应的收敛研究指数；两图共享时间范围和缩放操作，并支持原始快照、30秒、1分钟和5分钟频率。

## 可执行申赎套利模拟

项目在原均值回复研究之外增加了独立的“可执行套利模拟”页面，原页面、原回测入口和原有
计算结果保持不变。新模块使用以下链路：

```text
ETF与成分股多档Bid/Ask + 官方PCF
-> 逐档扫单得到1 CU可执行篮子价格
-> 申购/赎回净利润与无套利区间
-> 数据质量、限额、资金和库存检查
-> 延迟后盘口重新定价
-> 虚拟多腿成交与一级市场申赎
-> 虚拟账户、最终现金差额和PnL
```

启动项目后，在Streamlit左侧页面导航中进入“实盘套利模拟”：

```powershell
conda activate etf-arbitrage-py314
python -m streamlit run streamlit_app.py
```

当前工作区使用项目内环境时也可以运行：

```powershell
.\.conda\py314\python.exe -m streamlit run streamlit_app.py
```

左侧导航固定显示为“实盘均值回复监控”和“实盘套利模拟”。模拟页面可设置有限Tick时间轴，
并以0.25x至100x倍速连续回放；总览同步绘制ETF Bid/Ask、官方IOPV和内部IOPV时间序列，
鼠标悬停时使用统一时间指示线。

默认参数为159915、模拟行情、1 CU、库存锁定模式、禁止主动使用允许现金替代、Redis关闭、
自动影子交易关闭和只记录机会。只有在页面中主动启用“自动影子交易”并关闭“只记录机会”后，
系统才创建带有 `simulated_only=true` 标记的虚拟订单。项目没有券商柜台、真实报单或撤单接口。

### 三种行情模式

- `SIMULATED`：默认模式。根据PCF生成内部一致的成分股、IOPV和ETF多档盘口，可注入溢价、
  折价、深度不足、陈旧、缺失、停牌、涨跌停、解码错误和序号断档等场景；相同随机种子可复现。
  可勾选“使用内网Redis最新价初始化”，用当日ETF和全部PCF实物成分股的 `closepx` 锚定
  首个快照；Bid/Ask、盘口深度、冲击和后续路径仍是模拟值。
- `FILE_REPLAY`：支持CSV、Parquet、JSON和JSONL，可输入文件、目录或通配符。载入层负责字段
  映射，回放只读取 `timestamp <= decision_time` 的记录，不使用未来行情。
- `REDIS`：可选实时接口，默认 `enabled=false`，并使用延迟导入。未安装Redis包或连接失败时，
  模拟与文件回放仍可使用；密码不会在页面明文显示。

文件回放至少需要时间、证券代码、ETF标记、买卖方向、档位、价格和数量等字段。常见的
`bid1_price`、`bid1_volume`、`ask1_price`、`ask1_volume` 可在载入层映射，不会进入套利引擎。
页面会显示文件数量、记录数、时间范围、证券数量、缺失字段和数据预览，并支持重新加载。

### PCF与计算口径

可执行页面支持官方链接下载、本地路径和XML上传，并显示交易日、申赎单位、预估现金差额、
申赎开关、限额、现金替代统计、文件时间和SHA256哈希。下载失败只会保留错误信息并提示使用
本地文件，不会自动换用未经确认的第三方PCF。

默认现金替代口径为：禁止和允许现金替代的证券均按实物盘口处理；必须现金替代的证券使用
PCF中方向对应的现金金额。启用“允许现金替代”属于高级情景研究，使用前应再次确认所用PCF
供应商对溢价率和折价率字段的单位约定。

申购方向按“买入成分篮子、卖出ETF、模拟申购”计算；赎回方向按“买入ETF、卖出成分篮子、
模拟赎回”计算。所有腿均逐档扫单，任一必需盘口深度不足时整笔拒绝。安全缓冲只用于信号
门槛，最终PnL按虚拟成交、费用和最终现金差额计算，不把缓冲当作真实损失重复扣除。

`INVENTORY_LOCKED` 用已有库存或虚拟券源近似锁价；`SEQUENTIAL_NO_BORROW` 需要等待申赎结果
后再完成另一侧交易，存在底层价格和折溢价消失风险，页面会明确标记其不是无风险套利。

### 运行记录与测试

页面的“历史与导出”页签可导出配置、快照、机会、拒绝原因、虚拟订单、成交、一级市场请求、
交易、PnL和质量事件。完整运行记录保存在 `data/runs/{run_id}/`，每个循环都有独立 `cycle_id`。

安装和测试：

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
```

Redis仅在需要时安装：

```powershell
python -m pip install -e ".[redis]"
```

## 研究边界

原均值回复回测指数是折溢价头寸的收敛研究指数，并不是账户净值或申赎套利收益。新增模块已覆盖多档深度、
最小申赎单位、现金替代、费用、延迟、账户资源和一级市场现金差额的可配置模拟，但仍不代表
真实可成交结果。真实研究还需接入并核验交易所级盘口、券源、申赎权限、结算时点、税费、
资金占用和券商规则。Dashboard默认数据为确定性模拟行情，只用于验证系统行为和研究流程。
