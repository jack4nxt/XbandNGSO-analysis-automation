# 07 配置参考

所有字段**必填**(无隐式默认值);可空字段显式写 `null`。示例文件见 `configs/*.example.yaml`。

## paths.yaml

| 字段 | 含义 |
|---|---|
| `data_root_env` | 存放数据根目录的**环境变量名**(代码不含具体路径) |
| `dirs.outputs` / `dirs.cache` / `dirs.raw_inputs` | 数据根下的子目录名 |
| `layout.batch_dir` | 批次目录模板,变量 `{batch_id}` |
| `layout.pair_dir` | pair 目录模板,变量 `{idx}`、`{interferer}`、`{victim}` |
| `layout.post_dir` | 后处理目录模板,变量 `{post_name}`、`{ts}` |
| `layout.idx_width` | 序号位宽 |
| `layout.name_sanitize_regex` | 目录名中需替换的字符(正则) |
| `layout.timestamp_format` | 时间戳格式 |

## batch.yaml

| 字段 | 含义 |
|---|---|
| `batch_id` | 批次标识 |
| `evaluated_network` | 待评估网络名 |
| `evaluated_role` | `interferer` / `victim` / `both`(决定每个协调对象生成几个 pair) |
| `bands[]` | `name`、`f_low_mhz`、`f_high_mhz`、`directions`(每个频段适用方向) |
| `coordination.inputs[]` | `path`、`kind`(`legacy_excel`/`user_excel`/`word`/`pdf`/`standard`) |
| `coordination.rules` | `coordination_rules.yaml` 路径 |
| `coordination.decisions` | 匹配裁定文件路径(可 `null`) |
| `time.start_utc` | **必须为具体 ISO 时刻,禁止 `TODAY`** |
| `time.duration_days` / `time.coarse_step_s` | 仿真时长与步长 |
| `overrides` | 覆盖文件路径(可 `null`) |
| `antenna.policy` / `antenna.decisions` | 天线策略与决策文件 |
| `engine.adapter` | `module:Class` 字符串 |
| `engine.options` | 透传给适配器的字典(引擎模板路径等) |
| `runner.max_workers` / `resume` / `continue_on_error` | 执行行为 |

## coordination_rules.yaml

| 字段 | 含义 |
|---|---|
| `legacy_excel.sheet` | 清单页签名 |
| `legacy_excel.columns` | 逻辑列 → 实际列名映射 |
| `normalize.cell_separators` | 单元格拆分分隔符列表 |
| `normalize.strip_suffix_patterns` | 需剥离的后缀正则列表 |
| `normalize.case_insensitive` | 是否忽略大小写 |
| `match.strategies` | 匹配策略顺序(`exact`、`alnum_insensitive` …) |
| `filter.exclude_reasons` | 需过滤的"受影响原因" |

## antenna_policy.yaml

| 字段 | 含义 |
|---|---|
| `source_order[]` | 来源优先级 |
| `required_phi_max_deg` / `phi_step_deg` | 曲线应覆盖范围与步进 |
| `peak_tol_db` / `beamwidth_tol_frac` | 校验阈值 |
| `itu_model.required_params` | 各模型必需参数(缺则待决) |
| `earth_station.model_map` | `ant_type` → 模型的**显式**映射;未列出的不回退 |

## antenna_decisions.yaml

条目:`key`(network/beam/emi_rcp/role,支持 `*`)、`issue`(问题码或 `*`)、`action`、`params`、`by`、`date`、`reason`。

## overrides.yaml / post.yaml

见 [05](05-overrides.md) 与 [06](06-batch-layout-and-post.md)。`post.yaml` 的 `engine_post` 段原样透传引擎统计配置;`summary.columns` 控制汇总表列。
