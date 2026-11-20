# 04 天线策略

这是整套方案最关键的部分,对应你的 3.3.a / 3.3.b。

## 1. 铁律

1. **只允许从"来源"取曲线**;来源有固定优先级(配置于 `antenna_policy.yaml: source_order`)。
2. **任一来源失败 → 只能上报,不得换成程序自行估算/默认模型**。
3. 任何改变曲线的操作(平移、补尾、外推)**只在存在你的显式决定时才执行**,且记录到 provenance。
4. 你的决定持久化到 `antenna_decisions.yaml`;之后所有批次遇到同一波束一律沿用 → 全局一致。

## 2. 来源与优先级(默认顺序,可配置)

| 序 | 来源名 | 含义 |
|---|---|---|
| 1 | `user_decision` | 你批准过的方案(含你提供的 CSV) |
| 2 | `gims_vector` | 数据库 GIMS 矢量点序列 |
| 3 | `sheet5_discrete` | 参数表 `sheet5` 离散方向图 |
| 4 | `gims_figure_verified` | GIMS 位图读图,且已通过核验 |
| 5 | `itu_model` | 申报的 ITU 标准模型,**且所需参数齐全** |
| – | (无) | 进入《待决清单》 |

> 顺序是否合理(例如 3 与 4 谁在前、2 与 5 谁在前)见 [08 待裁定问题](08-roadmap-and-open-questions.md)。

`itu_model` 源额外规则:公式需要的口径/波束宽度/`beamlet` 等字段为空时,**不反推**(如不得用 `20lg(D/λ)=Gmax−7.7`),直接待决。

## 3. 校验(`antenna/checks.py`)

对每个候选曲线执行,阈值全部来自策略文件:

| 代码 | 条件 | 含义 |
|---|---|---|
| `PHI_RANGE_SHORT` | 曲线 `phi_max < required_phi_max_deg` | 角度范围不足(如只画到 30°) |
| `PEAK_MISMATCH` | `|曲线峰值 − 申报增益| > peak_tol_db` | 图文不一致 |
| `BEAMWIDTH_MISMATCH` | 曲线 −3 dB 宽度与申报相差超 `beamwidth_tol_frac` | 主瓣不一致 |
| `NON_FINITE` | 含 NaN/Inf | 数据损坏 |
| `NOT_MONOTONIC` | `phi` 非严格递增 | 数据损坏 |
| `MISSING_PARAMS` | 标准模型缺必需参数 | 无法按模型生成 |
| `NO_SOURCE` | 所有来源均无结果 | 无数据 |

没有触发任何代码 → 曲线直接通过(`resolved`)。

## 4. 处理动作(`antenna/transforms.py`)

仅当 `antenna_decisions.yaml` 对该问题给出了动作,才会应用:

| 动作 | 作用 | 适用的问题码 |
|---|---|---|
| `shift_to_declared_peak` | 整条曲线平移,使峰值=申报增益 | `PEAK_MISMATCH` |
| `trust_curve` | 保留曲线,仅记录偏差 | `PEAK_MISMATCH`、`BEAMWIDTH_MISMATCH` |
| `hold_last` | 超出范围按最后一点保持 | `PHI_RANGE_SHORT` |
| `constant_floor` | 超出范围取固定电平(`value_dbi`) | `PHI_RANGE_SHORT` |
| `use_model` | 指定使用某 ITU 模型(需参数齐全) | `NO_SOURCE`、`MISSING_PARAMS` |
| `use_csv` | 使用你提供的 CSV | 任意 |
| `exclude` | 该波束/网络本批次排除(并记录) | 任意 |

动作集合是**注册表**,新增动作只需实现函数并注册。

## 5. 决策文件(`antenna_decisions.yaml`)

按 `(network, beam, emi_rcp, role)` 为键;可用通配符网络名或"按问题码的全局规则"覆盖批量情形。示例见 `configs/antenna_decisions.example.yaml`。每条含:动作、参数、裁定人、日期、理由。

## 6. 预检门禁(`preflight`)

对批次内全部 pair 涉及的**空间波束与地球站**逐个调用 `AntennaResolver`:

- 全部 `resolved` → 通过;
- 否则输出 `pending_antennas.csv` + `pending_antennas.md`(含网络、波束、方向、申报增益、轨道高度、已尝试来源、触发的问题码、原图路径)并以**非零退出码**结束;
- 你把决定写入 `antenna_decisions.yaml` 后重跑 `preflight`,直到通过。

地球站同样适用:模型映射必须**显式**,没有回退;`REC-465-6` 不得被当作 `465-5`,除非决策文件写明。

## 7. 一致性保证

- 同一波束在不同批次 → 同一来源/同一决策 → 同一曲线;
- provenance 记录每条曲线的来源、触发的问题码、所应用的动作及决策条目 id;
- 冻结包内保存最终曲线 CSV,仿真不再重新解析天线。

## 8. 两套天线实现的合并要求

现有引擎里,仿真用的地球站曲线(`antenna_models.yaml`)与数字化库用的公式(`reference_pattern.py`)是**两套实现**,公式分支并不完全一致。落地时必须收敛为**一个**实现作为 `itu_model` 来源,仿真读取冻结包里的曲线 CSV,从而不再依赖第二套。
