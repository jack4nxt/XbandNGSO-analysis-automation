# 09 迁移指南

目标:换机器、换频段、换引擎或换数据库时,**只改配置与适配器**,不改编排代码。

## 1. 换机器

1. 安装:`pip install -e .`;
2. 复制 `configs/*.example.yaml` 为真实配置;
3. 在 `paths.yaml` 里确认 `data_root_env`,并在本机设置该环境变量指向数据盘;
4. 数据盘未挂载时,程序明确报错,不回落到仓库内。

## 2. 换频段 / 换业务

- 改 `batch.yaml: bands`(名称、频率、方向);
- 准则、判定逻辑属于引擎侧(如 `criteria_library.yaml`),不在本项目内。

## 3. 换协调列表格式

- 列名不同:改 `coordination_rules.yaml: legacy_excel.columns`;
- 后缀/分隔规则不同:改 `normalize.*`;
- 全新格式:新增 `Parser` 子类并注册(见 `coordination/parsers/`)。

## 4. 换引擎或数据库

- 实现新的 `EngineAdapter` / `CatalogAdapter`(见 `adapters/base.py` 的方法契约);
- `batch.yaml: engine.adapter` 改为新类路径;
- 契约测试 `tests/test_adapter_contract.py` 用假适配器验证编排逻辑,新适配器应通过同一组断言。

## 5. 新增天线来源 / 变换动作

- 来源:继承 `AntennaSource`,`@register_source("name")`,在 `antenna_policy.yaml: source_order` 中加入;
- 动作:实现函数,`@register_transform("name")`,即可在决策文件中使用。

## 6. 不要做的事

- 不要在代码里加默认路径、默认阈值、默认模型;
- 不要让预检以外的模块"顺手"补全缺失天线;
- 不要把审批结果写回数据库。
