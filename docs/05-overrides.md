# 05 参数覆盖

## 1. 作用点

覆盖发生在 **提取网络之后、生成 INPUT 之前**(由 `EngineAdapter.build_input` 之前调用 `overrides.apply_overrides`)。覆盖对象通过选择器指定,既可作用于待评估网络,也可作用于任一协调对象。

## 2. 模式

每个覆盖项是下列之一:

- `set`:绝对值替换;
- `delta`:在申报值上相加(dB 或单位同字段)。

同一字段不得同时写 `set` 与 `delta`。

## 3. 可覆盖字段(第一期最小集)

| 类别 | 字段 | 说明 |
|---|---|---|
| `carrier` | `f_low_mhz`、`f_high_mhz`、`bandwidth_hz` | 频率与带宽 |
| `carrier` | `power_dbw`、`psd_dbw_hz` | 功率/谱密度;只改其一时按配置的一致性规则补另一项 |
| `carrier` | `sat_gmax_dbi` | 星上增益,与曲线联动方式见下 |
| `earth_station` | `gmax_dbi`、`min_elev_deg`、`rx_noise_temp_k` | 地球站 |
| `orbit` | `altitude_km`、`inc_deg`、`node_deg`、`n_sats` 等 | 轨道面参数(按面选择) |
| `stations` | `include` / `exclude` / `add` | 站点列表 |

可覆盖字段白名单在 `overrides.yaml` 的 schema 中声明;**新增字段 = 改 schema + 适配器映射,不改应用逻辑**。

## 4. 选择器

```yaml
- target: {network: "<名称或*>", role: "interferer|victim|*"}
  select: {kind: "carrier", beam: "<波束或*>", direction: "DL|UL|*"}
  set:   {psd_dbw_hz: -52.0}
  delta: {sat_gmax_dbi: -3.0}      # 与 set 不同字段才可共存
  reason: "用户说明"
```

## 5. 与天线曲线的联动

覆盖 `sat_gmax_dbi` 时,曲线如何处理**不由代码默认**,而由覆盖项显式指定 `curve_policy`:

- `shift`:整条曲线平移相同 dB;
- `keep`:曲线不变(仅改标称值);
- `reject`:要求你提供新曲线。

缺省则报错,不做假设。

## 6. 可追溯

每个被覆盖的字段在 provenance 中记录 `declared → effective`、覆盖项 id、`reason`。`overrides.yaml` 本身随批次复制进冻结包。
