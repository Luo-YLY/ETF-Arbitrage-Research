# Panel + Bokeh 增量回放实验台

该实验台与现有 Streamlit 控制台并行存在，不替换原界面。它读取已经生成的观察结果，并把
行情持续追加到同一组 Bokeh 数据源中，图表更新时无需重新构建整个页面。

在项目根目录启动：

```powershell
.\.conda\py314\python.exe -m panel serve panel_app.py --address 127.0.0.1 --port 8505
```

打开 `http://127.0.0.1:8505/panel_app`。

实验台会自动发现：

- `tmp/observations/YYYYMMDD/{ETF}.jsonl`
- `outputs/data_check/YYYYMMDD/{ETF}/observations.csv`

支持开始、暂停、单步、重置及 `1x/5x/20x/100x` 回放。ETF 价格、IOPV、买一、卖一和
折溢价共用时间轴与十字光标；原始 Redis 行情采集仍由独立进程负责，不受该实验台控制。
