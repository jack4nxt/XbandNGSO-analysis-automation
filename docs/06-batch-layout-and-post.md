# 06 批次目录、仿真与后处理

## 1. 批次目录(布局模板在 `paths.yaml`,可改)

```
$DATA_ROOT/outputs/<batch_id>/
├── batch.yaml                    # 冻结的任务定义副本
├── coordination_table.csv        # 标准协调表(+ validation_report.md)
├── preflight/                    # 待决清单与预检报告(每次预检一份,带时间戳)
├── batch_state.json              # 各 pair 状态:pending/running/done/failed/skipped
├── pairs/
│   └── 001_<interferer>__<victim>/
│       ├── frozen/               # INPUT.xlsx、antenna/*.csv、provenance.json、overrides.yaml
│       └── raw/                  # 引擎原始输出(只写不改)
└── post_runs/
    └── <post_name>_<ts>/
        ├── batch_summary.xlsx
        └── pairs/001_.../        # 单 pair 的统计、链路表、散点图、CCDF
```

目录名与序号规则由 `layout.pair_dir` 等模板控制。**不改变引擎对单 pair 输出的内部结构**,批次层只做"包一层 + 建索引"。

## 2. 仿真(`run`)

- 输入:冻结包;**不再访问数据库、不再解析天线**;
- 输出:`raw/`(逐时刻链路量:几何、离轴角、收发增益、路径损耗、`i_psd`、C、C/N、I/N 等,由引擎定义)+ `manifest`;
- 行为:`resume` 跳过已完成 pair;`continue_on_error` 隔离失败;`max_workers` 并行;
- 时间:使用 `batch.yaml` 的冻结 `start_utc`,所有 pair 一致。

## 3. 后处理(`post`)

- 输入:批次目录 + `post.yaml`;**只读 raw**;
- 单 pair:调引擎统计(链路计算表、统计值、散点图、CCDF);
- 跨 pair:汇总 `batch_summary.xlsx`,列(来自 `post.yaml: summary.columns`,可配):评估对象、受扰网络、场景、判定、最大超标百分比、最小裕量、最坏链路;
- 参数(门限、附加损耗、统计分母、时间窗、站筛选)全部在 `post.yaml`;
- 每次后处理写入新的 `post_runs/<name>_<ts>/`,不覆盖旧结果、不碰 raw。

## 4. 运算量与存储(落地前必须实测)

- 估算公式:总耗时 ≈ pair 数 × 单 pair 耗时 ÷ 并行度;总存储 ≈ pair 数 × 单 pair raw 体积;
- 建议先用 1–2 个真实 pair(含大星座)跑通,记录耗时、峰值内存、raw 体积,把数值写入 `docs/08` 的验收记录,再确定 `max_workers` 与外置盘预留;
- 可复用:同一干扰网络的轨道/星历、同一受扰网络的提取结果(见 02 §5)。

## 5. 复现与审计

- 冻结包 + `batch.yaml` + `antenna_decisions.yaml` 的哈希写入 `batch_state.json`;
- 任何 pair 的结果都能回溯到:列表行 → 匹配结果 → 覆盖项 → 天线来源与决策 → 引擎版本。
