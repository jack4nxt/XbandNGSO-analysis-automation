# 03 协调列表

## 1. 现状事实

以 `satellite network/<网络>/final.xlsx` 为例(每个目录对应一个待评估网络及其协调对象):

- `List` 页每行是"频段 × 运营商 × 若干受影响网络";`Satellite Network` 列**一格内用换行放多个网络名**;
- 名称带 `(NGSO)`、`(105°E)` 等后缀,与数据库 `sat_name` 不一致;
- `Notice ID`、`Status`、`Date of Exceed`、`Number of Satellites`、`Station of CHN` 等列**为空**(未校验);
- 另有 `受影响原因`(如"频率重叠")。

## 2. 标准协调表

无论输入来源,统一成平面表,**一行一个受扰网络**(`coordination/models.py: CoordinationRow`):

| 字段 | 含义 |
|---|---|
| `evaluated_network` | 待评估网络(批次内固定) |
| `raw_name` | 清单中的原始写法 |
| `source` | 来源文件/页签/行号(可追溯) |
| `matched_name` / `ntc_id` / `adm` | 库匹配结果 |
| `orbit_type` | NGSO / GSO / UNKNOWN |
| `overlap_bands` | 与待评估网络重叠的 X 频段(来自 `bands` 配置) |
| `category` | `READY_CO_DIR` / `DEFERRED_REV_DIR` / `SKIP_GSO` / `SKIP_NO_CHN_ES` / `NEEDS_REVIEW` / `UNMATCHED` |
| `n_chn_stations` | 国内地球站数 |
| `review_note` | 需人工确认的原因 |

## 3. 处理管线

```
parse(格式相关) → normalize(拆格/去后缀/大小写,规则来自配置)
   → match(CatalogAdapter) → classify(频段+方向,配置驱动) → 标准表 + 校验报告
```

### 3.1 解析器(可插拔)

| kind | 说明 | 额外依赖 |
|---|---|---|
| `legacy_excel` | 存量 `final.xlsx` 的 `List` 页;列名映射来自 `coordination_rules.yaml` | openpyxl |
| `user_excel` | 用户自编 Excel;同样用列映射 | openpyxl |
| `word` | 提取表格 | python-docx |
| `pdf` | 提取表格 | pdfplumber |
| `standard` | 已是标准表,仅校验 | pandas |

新增格式 = 新增一个 `Parser` 子类并 `@register`,不改编排代码。

### 3.2 规范化规则(全部配置化)

`coordination_rules.yaml` 提供:单元格分隔符、需剥离的后缀正则、是否忽略大小写、需过滤的"受影响原因"等。**代码中不含任何网络名或后缀常量。**

### 3.3 匹配与歧义

- 先精确匹配,再宽松匹配(去非字母数字);策略与顺序来自配置。
- 候选多于 1(多通知单/多主管部门)或为 0 时**不猜测**,标记 `NEEDS_REVIEW`/`UNMATCHED`,在报告里列出候选供你裁定;裁定写入 `coordination_decisions.yaml`(与天线决策同机制)。

### 3.4 分类

由待评估网络与受扰网络在各 X 频段的载波方向决定:
- 同频段、同方向 → `READY_CO_DIR`;
- 同频段、反方向 → `DEFERRED_REV_DIR`;
- GSO 或无国内地球站 → `SKIP_*`,并写明原因。

频段列表(含名称、上下限、适用方向)来自 `batch.yaml: bands`,改频段不改代码。

## 4. 校验报告

`validation_report.md`:匹配率、各类别计数、`NEEDS_REVIEW`/`UNMATCHED` 明细(带候选)、与清单中 `Station of CHN` 等已有列的不一致项。这份报告同时是"摸底"的产出,用来估算后续待决量。
