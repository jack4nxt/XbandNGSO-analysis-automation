# 02 架构

## 1. 分层

```
cli ──▶ 编排(coordination / preflight / freeze / runner / post)
              │
              ├──▶ antenna  (来源插件 + 策略 + 决策 + 变换)   纯逻辑,可单测
              ├──▶ overrides(覆盖 schema + 应用)               纯逻辑,可单测
              └──▶ adapters (EngineAdapter / CatalogAdapter)   ← 唯一与外部系统耦合处
                                   │
                    X-band-NGSO 引擎、itu_space PostgreSQL、外置盘
```

## 2. 模块职责

| 包 | 职责 | 关键对象 |
|---|---|---|
| `config` | pydantic schema;YAML 加载;`${ENV}` 展开;拒绝 `TODAY` | `BatchConfig`、`load_config` |
| `paths` | 数据根(来自环境变量名配置)与目录布局模板 | `Layout` |
| `coordination` | 解析 → 规范化 → 库匹配 → 分类 → 标准表 | `CoordinationRow`、`Parser` 注册表 |
| `antenna` | 天线请求→按优先级取曲线→校验→(有决策才)变换→解析结果 | `AntennaResolver`、`AntennaSource`、`Decision` |
| `overrides` | 覆盖 schema 与应用;记录"申报值→覆盖值" | `OverrideSet`、`apply_overrides` |
| `preflight` | 汇总全部 pair 的参数与天线问题,生成待决清单 | `PreflightReport` |
| `freeze` | 通过预检后,经适配器生成每 pair 冻结包 | `FrozenPair` |
| `runner` | 并行/续跑/容错地执行冻结包 | `BatchRunner`、`PairState` |
| `post` | 调引擎统计,再汇总跨 pair 结果 | `run_batch_post` |
| `adapters` | 引擎、目录库的抽象与具体实现 | `EngineAdapter`、`CatalogAdapter` |

## 3. 适配器接口(迁移的唯一接缝)

`adapters/base.py` 定义两个 `Protocol`:

- `CatalogAdapter`:按名称查网络候选(`find_networks`)、列出网络在给定频段的波束/载波/地球站概要(`describe_network`)。
- `EngineAdapter`:`extract_network` → 应用覆盖与天线 → `build_input`(写冻结包)→ `run_pair`(执行仿真)→ `run_stats`(统计)。

具体实现通过 `batch.yaml` 的 `engine.adapter: "package.module:Class"` 以字符串指定、`importlib` 加载。`adapters/xband_ngso.py` 是对现有仓库的适配草稿,每个方法标注了应调用的现有函数。

## 4. 数据流与落盘物

```
协调列表(任意格式) ─▶ coordination_table.csv + validation_report.md
                                       │
覆盖 + 天线策略 + 决策 ─▶ preflight ─▶ pending_antennas.csv / .md  (或 PASS)
                                       │
                                  freeze ─▶ pairs/NNN_xxx/frozen/{INPUT.xlsx, provenance.json}
                                       │
                                    run ─▶ pairs/NNN_xxx/raw/...   + batch_state.json
                                       │
                                    post ─▶ post_runs/<name>_<ts>/{pairs/..., batch_summary.xlsx}
```

## 5. 并发与缓存(设计要求)

- `runner.max_workers` 控制进程并行;pair 之间无共享可变状态。
- 同一干扰网络的轨道/星历、同一受扰网络的提取结果,按"网络+通知单+start_utc+持续天数+步长"为键缓存(缓存目录来自 `paths.yaml`),具体缓存对象由适配器决定。
- 落地前先用 1–2 个真实 pair 实测耗时与 raw 体积,再决定并行度(见路线图)。

## 6. 错误模型

| 类别 | 处理 |
|---|---|
| 配置错误(缺字段、`TODAY`) | 立即失败,指出文件与字段 |
| 匹配歧义 / 天线待决 | 不失败,进报告;`preflight` 退出码非 0 |
| 单 pair 运行时异常 | 记入 `batch_state.json`,继续下一个(`continue_on_error`) |
| 适配器未实现 | `NotImplementedError`,信息中指明应接哪个现有函数 |
