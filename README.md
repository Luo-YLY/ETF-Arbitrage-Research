# 沪深 ETF 套利研究系统（跨境模块暂停）

ETF Arbitrage v1.0 是一个面向研究的 Python 原型，用统一数据接口完成 ETF 理论价值计算、
折溢价监控、机会识别、风险阻断、历史回放和价差头寸回测。深交所 XML PCF 与上交所公开
PCF 查询 JSON 已统一映射到同一模型；Panel 控制台可以按代码或名称搜索，也允许直接输入
新的六位 ETF 代码。沪市账户、申赎、结算和生产行情规则仍属于待接入边界。

当前盘中主线只接受沪深上市、且有效成分全部属于沪深的境内ETF。“实盘套利模拟”可按
当日PCF并行采集多只ETF及其完整篮子的Redis五档快照，并为每只ETF保留独立JSONL文件。
2026-08-19 已在交易日Hash单条记录中观察到五档
`bidPrice/bidVolume/offerPrice/offerVolume` 字段；ETF单条成功不等于PCF全部成分同步可用，
盘前仍必须检查全篮子覆盖、双边盘口和时间戳。缺少任一执行要素时只记录快照并阻断纸面
可执行结论。港股/跨境入口和历史研究代码暂时保留，但因当前没有同口径实时IOPV，本轮不
启动、不进入多ETF任务。公共第三方行情下载不属于项目盘中数据链路。

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
│   ├── domain/        # 跨市场证券标识与标准领域事件
│   ├── eventing/      # 事件总线、存储、回放和快照组装
│   ├── exchanges/     # 深市/沪市解析适配器与能力边界
│   ├── operations/    # 交易时段与后台采集任务控制
│   ├── strategies/    # 连续套利与事件型策略扩展点
│   └── utils/         # 绩效统计
├── config/            # 示例研究参数
├── scripts/           # 命令行演示
├── tests/             # 基础与集成测试
├── streamlit_app.py   # Streamlit 看板入口
├── panel_app.py       # 沪深 Panel 双页面控制台入口
└── panel_hk_app.py    # 独立港股 PCF 与行情接口控制台入口
```

采用 `src/etf_arbitrage/signal` 命名空间而非项目根目录的 `signal` 包，是为了避免覆盖 Python
标准库同名模块。

事件驱动底座的模块边界、状态转换、沪市扩展缺口和兼容策略见
[`docs/事件驱动架构设计.md`](docs/事件驱动架构设计.md)。可用现有深市 PCF 和确定性模拟行情
运行三 Tick 的本地演示：

```powershell
python.exe scripts/run_event_demo.py --ticks 3
```

演示只写入本地 `data/events/` 并组装研究快照，不连接券商或真实交易。

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

当前只需启动沪深Panel。控制台打开地址为 `http://127.0.0.1:8505/panel_app`，直接进入套利模拟可使用
`http://127.0.0.1:8505/panel_app?page=executable`。控制台分为“实盘均值回复监控”和
“实盘套利模拟”两个独立页面。前者接入 PCF、内网 Redis 最新价采集、观察文件回放和收盘
折溢价收敛研究；后者接入 PCF、模拟行情、上传/本地历史行情、标准化 Redis 快照或交易日
Hash 原始五档行情，使用 ETF
与成分股 Bid/Ask、多档深度完成纸面申赎套利、订单成交、一级市场申赎和 PnL 模拟。两页
均复用持久化 Bokeh 数据源进行增量刷新，切页时会停止隐藏页面的定时任务。均值回复页默认
选择当天并允许通过日历选择任意日期；页面和标签内容按需加载，不再一次创建全部隐藏组件。

### 跨境入口（代码保留，当前暂停）

跨境ETF控制台代码原运行于 `http://127.0.0.1:8506/panel_hk_app`，默认选择 159920。它从
深交所公开文件域名自动下载当日XML PCF并保存到
`data/pcf/YYYYMMDD/pcf_159920_YYYYMMDD.xml`。控制台将 159900“申赎现金”识别为清算用
虚拟证券，只展示申购预收现金，不把它与93只港股成分重复计入IOPV。

该入口本轮不启动。其历史实现仍拆为“均值回复监控”和“实盘套利模拟”两个页面：均值回复页先作为最新价
采集台：展示159920.SZ与PCF中大写`.HK`成分的最新价和时间戳，盘中不展示IOPV或Premium；
页面本身不再联网下载历史行情。模拟页可读取Redis最新价、本地采集记录或完全
模拟数据，再生成ETF/成分多档盘口、纸面订单、申赎与损益。申购方向按“港股卖一×FX卖价”
复算，赎回方向按“港股买一×FX买价”复算；港股通资格、代理买卖、现金替代多退少补和T+
交收完成建模前，结果固定为 `INDICATIVE_PAPER`。直接打开模拟页可使用
`http://127.0.0.1:8506/panel_hk_app?page=executable`。

## 数据接入

实时或历史供应商只需实现 `DataFeed` 的三个方法：基金主数据、成分权重和按时间排序的行情
快照。估值、监控、风险、信号与回测均不依赖具体供应商。

推荐的正式接入顺序：交易所 PCF 和基金主数据，ETF/成分股实时行情，停复牌与涨跌停状态，
盘口深度和费率，最后再接申赎执行与券源约束。

### 内网 Redis 行情入口

项目已提供只读的 `SZRedisQuotationClient` 和 `SZRedisDataFeed`。Redis 连接信息通过
`SZ_REDIS_HOST`、`SZ_REDIS_PORT`、`SZ_REDIS_DB`、`SZ_REDIS_PASSWORD` 和
`SZ_REDIS_TIMEOUT` 环境变量配置，仓库不保存内网地址或凭据。

首次连接内网后，先安装可选依赖并探测标的的真实字段。跨境ETF还应逐一抽查PCF中的
`.HK` 成分代码：

```powershell
python -m pip install -e ".[redis]"
$env:SZ_REDIS_HOST="内网地址"
python scripts/probe_sz_quotation.py --code 159920.SZ
python scripts/probe_sz_quotation.py --code 00700.HK
```

适配器每轮只批量读取 ETF 和目标成分股，避免重复拉取整个市场。当前五档字段为
`bidPrice1..5`、`bidVolume1..5`、`offerPrice1..5`、`offerVolume1..5`，同时兼容旧的
`bidpx1/askpx1` 买一卖一命名。它能从 `cdate` 和 `ctime`
解析供应商时间，并优先按 PCF 挂牌市场映射 Redis 代码：上交所 `.SH`、深交所 `.SZ`、
北交所 `.BJ`、港交所 `.HK`。`--redis-code-suffix` 仅在交易所无法识别时作为兼容兜底。
Redis 只提供最新价时，沪深标的仍按原指示性研究口径运行；港股通入口只记录原始最新价，
估值状态保持`PENDING_FX`，不在盘中计算Premium或生成套利信号。

在启动实盘套利模拟前，可用任一境内ETF的当日PCF做一次全篮子只读预检：

```powershell
python scripts/probe_executable_redis.py `
  --pcf data/pcf/YYYYMMDD/pcf_159915_YYYYMMDD.xml `
  --trade-date YYYYMMDD
```

只有 `quality_blockers` 为空、ETF有双边五档且 `two_sided_components` 覆盖全部实物成分时，
才可把结果称为当时点的纸面可执行模拟。当前日内主线只启用沪深境内成分ETF；跨境/港股
篮子因缺少同口径实时IOPV暂不进入多ETF任务。Panel“实盘套利模拟”的“数据与配置”页可
多选境内ETF，每只ETF按自己的当日PCF并行读取交易日Hash原始五档，默认每3秒追加到：

```text
tmp/executable_recordings/YYYYMMDD/ETF代码.jsonl
```

对应的IOPV、数据质量和申购/赎回机会摘要写入：

```text
tmp/executable_results/YYYYMMDD/ETF代码.jsonl
```

完全相同的快照在同一进程及异常重启后都会跳过。后台任务自动等待开盘、午休保持、异常
重启并在收盘后结束；关闭浏览器不会停止任务。该入口只生成研究快照和纸面机会判断，不含
任何真实券商报单或申赎接口。

### 港股通盘中采集与收盘后汇率回填（当前暂停）

跨境控制台在开始采集前联网下载并校验当日官方PCF，PCF文件属于设备当日运行数据，不要求
通过Git同步。进入内网后点击“启动当日采集”，后台只读取`159920.SZ`及PCF中全部大写
`.HK`成分的`closepx/cdate/ctime`，原始记录写入：

```text
tmp/recordings/YYYYMMDD/159920.jsonl
```

盘中不创建159920的IOPV/Premium观察文件。收盘后准备带时间戳的HKD/CNY文件，至少包含：

```csv
timestamp,hkd_cny_mid
2026-08-05 09:30:00,0.9201
```

再执行：

```powershell
python scripts/backfill_cross_border_fx.py `
  --pcf data/pcf/YYYYMMDD/pcf_159920_YYYYMMDD.xml `
  --fx-file data/reference/fx/hkd_cny_YYYYMMDD.csv
```

程序只使用`fx_timestamp <= 行情timestamp`的最近汇率，默认最大时滞60秒；原始快照不会被
覆盖，派生结果默认写入
`outputs/cross_border_backfill/YYYYMMDD/159920/observations.csv`。该结果标记为
`MODEL_IOPV_POST_CLOSE`，不是交易所官方IOPV。

### 沪市 PCF 入口

沪市 PCF 使用上交所基金网站公开查询接口的头部与成分证券 JSON。Panel 中输入如 `510300`
并切换到“官方下载”后，无需填写 URL；系统会同时获取申赎约束和成分证券，校验交易日、
基金代码与成分数量后保存为 `data/pcf/YYYYMMDD/pcf_510300_YYYYMMDD.json`。解析器保留
挂牌市场、现金替代标志、申购现金替代溢价率、赎回现金替代折价率与官方原始字段。

该入口目前是研究级公开数据适配，不表示已经具备沪市真实账户申赎、结算或生产下单能力。

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
并以0.25x至100x倍速连续回放；总览同步绘制ETF Bid/Ask和内部IOPV时间序列，
鼠标悬停时使用统一时间指示线。价格图采用与均值回复监控一致的实际价格窄幅自动缩放，
官方IOPV暂不绘制；右轴以首个有效ETF Last为0 bp显示相对开盘变化。

默认参数为159915、模拟行情、自动优先本地采集价格基准、1 CU、库存锁定模式、禁止主动使用
允许现金替代、Redis实时接口关闭、自动影子交易关闭和只记录机会。只有在页面中主动启用
“自动影子交易”并关闭“只记录机会”后，系统才创建带有 `simulated_only=true` 标记的虚拟订单。
项目没有券商柜台、真实报单或撤单接口。

### 三种行情模式

- `SIMULATED`：默认模式。根据PCF生成内部一致的成分股、IOPV和ETF多档盘口，可注入溢价、
  折价、深度不足、陈旧、缺失、停牌、涨跌停、解码错误和序号断档等场景；相同随机种子可复现。
  侧边栏“模拟价格来源”默认选择“本地历史数据（逐条回放）”，流式读取
  `tmp/recordings/YYYYMMDD/ETF代码.jsonl`。每条记录保留原始时间戳、ETF Last和成分股Last，
  并在该时点合成Bid/Ask与多档深度，无需连接Redis，也不会把数百MB文件一次性载入内存。
  也可手工指定采集文件、切换为完全模拟，或在内网环境显式读取Redis最新价。需要注意：
  本地回放中的价格轨迹是真实采集值，盘口深度、冲击和成交仍是模拟值。
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

## 事件驱动扩展报告（2026-07-29）

当前事件闭环、跨市场数据需求、与实盘的差距及 GitHub 同类项目对标见：
[`docs/事件驱动扩展落地与实盘差距报告.md`](docs/事件驱动扩展落地与实盘差距报告.md)。
