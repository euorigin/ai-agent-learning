# 旅行攻略 Agent 助手 · v2

大模型驱动的旅行攻略助手：**任意目的地**（不限云南/成都这类预置样例）、真实天气、真实路网、区间化预算。
云南只是其中一个示例输入，系统本身不内置任何城市数据集。

- 后端：FastAPI + Pydantic（仅 3 个依赖，其余走标准库）
- 前端：单文件静态页，零 CDN、零构建
- 数据源：全部免费公开接口，除大模型外无需任何 Key
- 规模：39 个文件、约 7100 行

---

## 1. 快速开始

```bash
# 1) 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2) 填大模型 API Key
#    打开项目根目录的 .env，填这三行（默认已配好 DeepSeek）：
#      TRAVEL_LLM_BASE_URL=https://api.deepseek.com/v1
#      TRAVEL_LLM_MODEL=deepseek-chat
#      TRAVEL_LLM_API_KEY=sk-你的key

# 3) 启动
python run.py

# 4) 浏览器打开
#    http://127.0.0.1:8000
```

其他常用命令：

```bash
python run.py --check            # 环境自检（依赖 / 配置 / 外网连通 / 工具装载）
python run.py --port 8060        # 换端口
python run.py --reload           # 开发热重载
python run.py --engine demo      # 离线演示脑（不填 Key 也能看完整效果）
python run.py --engine rules     # 纯规则引擎，完全不调大模型

python tests/selfcheck.py            # 离线自检：82 项断言，不需要网络和大模型
python tests/selfcheck.py --online   # 追加联网体检：地理编码精度 / 天气 / 路网
python tests/test_extract_json.py    # 模型输出解析回归（思维链 / fence / 多个对象）
python tests/e2e_llm.py              # 真模型端到端：一句话需求 → 4 天行程（需配 Key，约 5~7 分钟）
python tests/llm_probe.py            # 只探活大模型（判断「引擎是 llm 还是悄悄退了 rules」）
```

> **接推理模型（MiniMax M2 / DeepSeek-R1 等）必读**：这类模型会先输出 `<think>…</think>`
> 再给正文，思考同样消耗 `max_tokens`。若按正文长度给预算，模型会在思考阶段就被
> `finish_reason=length` 截断，**正文一个 token 都拿不到**，表现为「候选地点获取失败 /
> 城市画像失败」→ 行程全空。本工程已处理三处：
> 1. `TRAVEL_LLM_OUTPUT_RATIO`（默认 `3.0`）为所有 JSON 调用放大 token 预算，并同步放宽超时；
> 2. 解析层剥掉思维链后再取 JSON（`app/llm.py:strip_reasoning`），narration/QA 的回复也会剥离；
> 3. POI 改为**分批索取 + 失败重试**（单次 12 个）：实测同一参数成功率约 4/5，
>    失败那次耗时 ~110s 属网关抖动，必须靠重试兜住。
>
> 换成非推理模型（`gpt-4o-mini`、`qwen-plus` 等）时把 `TRAVEL_LLM_OUTPUT_RATIO` 设回 `1.0` 即可。

> `.env` 里**未加引号的值支持行内注释**（`TRAVEL_LLM_TIMEOUT=90  # 说明`）。
> 解析器会剥掉 `#` 之后的内容；注意 `#` 前必须有空白，否则会被当成值的一部分。

---

## 2. 没有 API Key 也能先跑

| 模式 | 命令 | 效果 |
| --- | --- | --- |
| `demo` | `python run.py --engine demo` | 离线演示脑：不联网不花钱，**地点是合成数据**、坐标为未知，用来看完整前后端效果 |
| `rules` | `python run.py --engine rules` | 只做需求解析与数值计算，拿不到地点数据；配了 `AMAP_KEY` 才可用 |
| `auto`（默认） | `python run.py` | 有 Key 走大模型；没有 Key 时退回规则引擎 |

> ⚠️ **没有 Key 时的默认路径会产出空行程**：`auto` 在没有 Key 时退回 `rules`，
> 而 `rules` 没有地点数据源，行程里的安排数会是 **0** —— 这不是故障，是上表写明的行为。
> 想离线看到「每天几点去哪、怎么衔接、花多少钱」的完整效果，请显式用 `--engine demo`。
> 启动时会打印一行提醒，`python run.py --check` 也会点出当前引擎的实际影响。

演示脑会在行程里插入醒目提示，UI 顶部也会挂橙色横幅，不会被误当成真实数据。

---

## 3. 架构

```
web/                     单页前端（无依赖）
  index.html  style.css  app.js  favicon.svg
app/
  main.py                FastAPI 组装 + 启动横幅
  api/routes.py          8 个 REST 接口 + Markdown 导出
  agent/
    nlu.py               意图/槽位抽取（大模型优先，失败退正则规则）
    planner.py           行程装配：编排数据流、记录证据、汇总告警
    rules.py             确定性内核：时间轴、六维打分、预算、冲突检测
    orchestrator.py      会话编排：plan / refine / ask / chitchat
  tools/                 9 个工具（大模型可调用，也可被代码直接调用）
    places.py            地名解析、POI 发现、城市画像
    environment.py       天气窗口、两点交通
    planning.py          开放校验、候选打分、预算、行程自检
  providers/             供应商适配（换源不影响上层）
    composite.py         多源地理编码合并 + 可信度打分
    photon.py            Photon（OSM，免费）—— 中文地名主力
    open_meteo.py        Open-Meteo —— 天气 + 备用地理编码
    osrm.py              OSRM 真实路网 + 城际交通估算
    amap.py              高德（可选，有 Key 时自动启用）
  llm.py                 OpenAI 兼容客户端（原生 Tool Calling / JSON / 提示词三形态）
  demo_brain.py          离线演示脑（同时充当测试替身）
  prompts.py             所有提示词集中管理
  models.py              全量数据模型（外部字段都带来源/精度/置信度）
  store.py               行程存储（内存 + 轻量 JSON 落盘）
  settings.py            .env 解析 + 全局常量
tests/selfcheck.py       82 项离线断言 + 可选联网体检
```

依赖方向是单向的：`api → agent → tools → providers`，所以换数据源、换模型都不会牵动业务逻辑。

---

## 4. 九个工具

| 工具 | 作用 | 数据来源 |
| --- | --- | --- |
| `resolve_place` | 地名 → 坐标 | Photon + Open-Meteo 双源合并，与大模型交叉验证 |
| `discover_pois` | 目的地候选清单 | 高德（有 Key）/ 大模型知识库 |
| `city_profile` | 城市住宿餐饮价位与在地提示 | 大模型（区间估算） |
| `city_weather` | 逐日天气（温度/降雨/风/空气） | Open-Meteo |
| `route_between` | 两点交通时长与费用 | OSRM 真实路网 / 球面估算 |
| `opening_check` | 开放时间与预约校验 | 本地星期规则 + 大模型 |
| `score_candidates` | 六维加权打分排序 | 本地规则引擎 |
| `estimate_budget` | 预算汇总与省钱建议 | 本地规则引擎 |
| `verify_trip` | 交付前自检（冲突/证据/强度） | 本地规则引擎 |

`GET /api/tools` 返回全部工具的 JSON Schema，UI 右上角「工具清单」可直接查看。

---

## 5. 为什么数字可信

这套系统把「语言」和「算术」严格分开：

- **大模型只负责**：理解用户意图、提供目的地 POI 知识、写文案
- **代码负责全部数值**：球面距离（Haversine）、路网时长、时间轴、门票与预算汇总
- **每个外部字段都带 `source / precision / confidence`**，行程每个环节都能展开「数据来源」
- **不确定就明说**：
  - 天气预报只有 15 天窗口，超出会回退为**去年同期实测**并标 `basis=climatology`
  - 景点坐标来自模型估算时标 `precision=approximate`
  - 免费地理编码可能命中同名村庄 —— 用「双源合并打分 + 大模型交叉验证」兜底，
    两源分歧超过 120km 就采信大模型并把精度降为近似，同时给出提示
  - 门票价格不确定时留空（计 0），而不是编一个数字

---

## 6. 对话能做什么

| 说法 | 行为 |
| --- | --- |
| 「从北京出发，下周五去成都玩4天，预算6000，喜欢美食和自然风光」 | 生成完整行程 |
| 「带父母去西安3天，节奏轻松点」 | 自动识别为行动不便，降低每日步行上限 |
| 「我想去云南大理丽江7天」 | 连写地名自动切分，多城市按天数均分 |
| 「第3天加一个博物馆」 | 新增 POI 并按就近顺序重排当日时间轴 |
| 「把第2天的湿地换成古城」 | 替换后重排，预算同步刷新 |
| 「删掉第4天的集市」 | 删除；找不到会明确说「没找到」而不是静默失败 |
| 「这几天怎么穿衣服？」 | 基于行程真实天气回答 |
| 「你好呀」 | 寒暄，不触发规划 |

行程支持版本号与变更历史，可导出 Markdown 攻略（`GET /api/trips/{id}/export`）。

---

## 7. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 引擎、模型、数据源、工具清单 |
| POST | `/api/reload` | 改完 `.env` 后热重载配置 |
| POST | `/api/plan` | 结构化生成行程（跳过 NLU） |
| POST | `/api/chat` | 自然语言对话规划 |
| GET | `/api/trips` | 行程列表 |
| GET | `/api/trips/{id}` | 行程详情 + 纯文本 |
| GET | `/api/trips/{id}/export` | 导出 Markdown |
| DELETE | `/api/trips/{id}` | 删除行程 |
| GET | `/api/tools` | 工具 Schema |

在线文档：`http://127.0.0.1:8000/docs`

---

## 8. 已知限制

1. **境外城市的中文名**（如「东京」「纽约」）在免费地理编码里命中率低，靠大模型交叉验证矫正；
   未配置大模型时这类坐标不可靠（会在行程里告警）。
2. **景点级坐标**在无高德 Key 时来自大模型估算，只保证量级正确（几公里误差），
   不适合导航；要精确导航请填 `AMAP_KEY`。
3. **机票/高铁票价**是按里程估算的区间，不是实时票价。
4. **开放时间与预约政策**变化快，系统会提示「出行前请核实」。
5. Photon 与 OSRM 都是公益服务，请勿高频调用；生产环境请自建或换商业源。

---

## 9. 测试

```bash
python tests/selfcheck.py --online
# 82 项断言覆盖：NLU 解析、跨年日期、打分排序、时间轴无重叠、
# 预算汇总、端到端规划、增删改微调、降级路径、真实 HTTP 客户端、数据源体检
```
