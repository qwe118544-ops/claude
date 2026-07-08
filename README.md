# wu-rule-lock — 逆向锁定 Wunderground 日最高温的计算规则

Polymarket 温度盘按 wunderground.com/history 页面的日最高温结算，但该值从 METAR
到整数 °F 的推导规则（T-group 十分位 vs 报文主体整数 °C、取整方向、日界）无官方文档。
本工具用一年的原始 METAR 重放全部候选规则，与 Wunderground 逐日比对，复现率 100%
的即为真实规则。

## 依赖

Python 3.9+，纯标准库，无第三方依赖。

## 一条命令跑通（以 KLGA / LaGuardia 为例）

```bash
python3 wu_rule_lock.py all --station LGA --wu-loc KLGA:9:US \
    --tz America/New_York --days 365 --outdir data/
```

分步等价形式、以及用自己爬的真值 CSV（`date,high` 两列）替代 api.weather.com 的用法
见 `python3 wu_rule_lock.py --help` 和脚本顶部 docstring。

## 候选规则空间（4 × 4 × 2 = 32 条）

- **温度来源**：`tgroup_pref`（RMK T-group 0.1°C，缺失回退主体）/ `body_int`（主体整数 °C）
  / `tgroup_only` / `tgroup_to_intC`（T-group 先取整到整数 °C 再换算）
- **°F 取整**：`half_up` / `half_even` / `trunc` / `floor`
- **日界**：`local`（本地墙钟，随夏令时）/ `lst`（固定标准时偏移，NWS 气候日口径）

"逐条取整再取 max" 与 "先 max 再取整" 对单调取整函数可证恒等，脚本对每条规则同时
计算两种顺序并断言相等（`daily_highs` 的 `order` 参数），即用数据实证而非假设。

所有 °C→°F 换算与取整用 `fractions.Fraction` 精确有理数运算——22.5°C 恰为 72.5°F，
浮点在这类 .5 边界上会给出错误的桶归属，而这正是需要分辨规则的样本点。

## 数据源

- **IEM ASOS 档案**（mesonet.agron.iastate.edu）：原始 METAR 文本（routine + SPECI），UTC。
- **api.weather.com v1 historical**：Wunderground 历史页自身渲染所用的后端，公开站点
  key（可用环境变量 `WU_API_KEY` 覆盖）。观测表的整数 `temp` 即页面逐行显示值，
  按本地日取 max 作为真值；也可用 `--truth-csv` 传入自己抓取的页面 "High Temp"。

## 输出

- 终端：32 条规则按复现率排名，100% 的规则单独列出。
- `daily_comparison.csv`：最优规则的逐日对照。
- `mismatches.csv`：前三名规则的失配日诊断（含当日最热的三条 METAR），用于定位
  SPECI 缺漏、6 小时极值组回填、日界边界等成因。

## 判读注意

若多条规则并列 100%，它们在这段数据上不可分（例如全年无恰好 x.5°F 的样本时
`half_up` 与 `half_even` 必然并列；高温全部出现在白天时 `local` 与 `lst` 必然并列）——
对结算而言并列规则等价，任选其一即可，或加长回溯期寻找分辨样本。

## 离线验证

```bash
python3 wu_rule_lock.py selftest
```

覆盖：T-group 正负号解析、缺失露点报文、72.5°F 精确半值在四种取整下的行为、
负数取整方向、max/取整交换律、EDT/EST 日界分桶。另有合成数据集成测试验证
"植入规则可被 100% 识别、错误规则被排除"。
