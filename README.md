# X-band-Analysis-automation (Sonnet solution)

X 频段 NGSO 系统间干扰分析的**批量自动化编排层**(方案 + 代码脚手架)。

> 状态:**脚手架**。接口、配置 schema、流程骨架和文档已就位;各 `TODO` 处等待按 `docs/` 中的设计填充实现。
> 底层 1-on-1 仿真/统计引擎沿用现有仓库 `X-band-NGSO`,本项目通过 *适配器* 调用它,不复制其代码。

## 一句话原则

**先把所有输入"冻结并审批",再无人值守地跑。** 一致性来自冻结,不来自事后检查;天线数字化严禁静默计算,拿不准的一律进《待决清单》问人。

## 流水线

```
batch.yaml ─▶ ① 协调列表解析/校验 ─▶ ② 预检(参数+天线) ─▶ ③ 冻结包 ─▶ ④ 批量仿真(raw)
                                          │  ▲                                  │
                                          ▼  │                                  ▼
                                   待决清单 ─▶ 你裁定 ─▶ antenna_decisions   ⑤ 批量后处理(独立、可反复)
```

## 目录结构

```
Sonnet solution/
├── README.md                    本文件(入口)
├── pyproject.toml               包与 CLI 入口 (xband-batch)
├── requirements.txt
├── .gitignore
├── docs/                        方案文档(从 00-index.md 开始读)
│   ├── 00-index.md              文档目录
│   ├── 01-scheme.md             方案总览:目标、原则、流水线、对旧方案的修正
│   ├── 02-architecture.md       模块划分、数据流、适配器
│   ├── 03-coordination-list.md  协调列表:双源输入、标准表、校验分流
│   ├── 04-antenna-policy.md     天线策略:优先级、门禁、决策文件
│   ├── 05-overrides.md          参数覆盖机制
│   ├── 06-batch-layout-and-post.md  目录布局、仿真/后处理解耦
│   ├── 07-configuration-reference.md 全部配置项说明
│   ├── 08-roadmap-and-open-questions.md 实施路线与待你裁定的问题
│   └── 09-migration-guide.md    迁移到其他机器/项目的做法
├── configs/                     全部可调参数(无硬编码)
│   ├── paths.example.yaml
│   ├── batch.example.yaml
│   ├── coordination_rules.example.yaml
│   ├── antenna_policy.example.yaml
│   ├── antenna_decisions.example.yaml
│   ├── overrides.example.yaml
│   └── post.example.yaml
├── src/xband_batch/             代码
│   ├── cli.py                   命令行入口
│   ├── config/                  schema + 加载(环境变量展开、拒绝 TODAY)
│   ├── paths.py                 数据根目录与布局模板
│   ├── coordination/            协调列表解析、规范化、库匹配、分类
│   ├── antenna/                 天线解析器、来源插件、校验、决策、变换
│   ├── overrides/               参数覆盖 schema 与应用
│   ├── preflight/               预检与待决清单
│   ├── freeze/                  冻结包生成
│   ├── runner/                  批量仿真执行、状态、断点续跑
│   ├── post/                    批量后处理与跨 pair 汇总
│   └── adapters/                与 X-band-NGSO 引擎/数据库的适配层
└── tests/                       脚手架测试(不连库、不依赖外置盘)
```

## 快速开始

```bash
cd "Sonnet solution"
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

cp configs/paths.example.yaml configs/paths.yaml          # 填数据根环境变量名等
cp configs/batch.example.yaml configs/batch_demo.yaml     # 一个批次一份

xband-batch parse-list  --batch configs/batch_demo.yaml   # ① 解析/校验协调列表
xband-batch preflight   --batch configs/batch_demo.yaml   # ② 预检;有待决项则退出码非 0
xband-batch freeze      --batch configs/batch_demo.yaml   # ③ 生成冻结包
xband-batch run         --batch configs/batch_demo.yaml   # ④ 批量仿真
xband-batch post        --batch configs/batch_demo.yaml --post configs/post.yaml   # ⑤ 后处理
xband-batch status      --batch configs/batch_demo.yaml
```

## 约定

- 代码里不写业务数值/路径/名称规则;一律来自 `configs/*.yaml`,缺字段直接报错。
- 与引擎、数据库的耦合只发生在 `adapters/`,迁移时只需换适配器和配置。
- 原始数据(raw)只写不改;后处理只读 raw。
