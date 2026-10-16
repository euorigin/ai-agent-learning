# ai-agent-learning

四个独立练手项目的集合仓库，围绕「大模型 + 工程化」这个主线：从 **Agent 编排**到 **数据源聚合**，再到 **小程序**与 **浏览器小游戏**。

每个子目录都是可独立运行、有独立自检脚本的完整项目。

---

## 项目索引

| 目录 | 项目 | 技术栈 | 一句话说明 |
| --- | --- | --- | --- |
| `游戏角色npc agent/` | 游戏角色 NPC Agent | Python · FastAPI · LLM | 游戏 NPC 对话 Agent：理解玩家意图 → 提出决策 → 调工具 → 验证世界状态 → 组织台词 |
| `旅行攻略Agent/` | 旅行攻略 Agent 助手 v2 | Python · FastAPI · LLM | 任意目的地的行程规划：真实天气 / 真实路网 / 区间化预算，大模型只负责理解与知识 |
| `红果追剧小程序/` | 红果追剧助手 | 微信小程序 · 原生 | 短剧订阅 + 更新提醒 + 可解释推荐的完整小程序 |
| `英语单词闯关/` | 英语单词闯关 | 单文件 HTML · 原生 JS | 零依赖单文件单词游戏，内置 CET4 / CET6 / 考研词库 |

详细说明见各子目录自己的 `README.md`（`英语单词闯关/` 与 `游戏角色npc agent/` 见源码内注释与自检脚本）。

---

## 密钥配置（每个项目都需要，且都**不上传**）

两个 Python 项目走 OpenAI 兼容协议，只需填三行：

```bash
# 1) 复制模板（.env.example 是干净的模板，可以放心提交）
cp .env.example .env          # Windows: copy .env.example .env

# 2) 编辑 .env，填入你自己的 Key
NPC_LLM_BASE_URL=https://api.deepseek.com/v1     # 任意 OpenAI 兼容端点
NPC_LLM_MODEL=deepseek-chat
NPC_LLM_API_KEY=sk-你的key
```

- `游戏角色npc agent/` → 变量前缀 `NPC_LLM_*`，默认端口 `8080`
- `旅行攻略Agent/travel-agent/` → 变量前缀 `TRAVEL_LLM_*`，默认端口 `8000`

**两个项目都能在没有 Key 的情况下完整演示**：`NPC_ENGINE=demo` / `TRAVEL_ENGINE=demo` 会走离线演示脑，链路（理解 → 决策 → 工具 → 表现）依然完整。

> 仓库根目录的 `.gitignore` 已递归排除 `.env`、`*.key`、`.data/`、日志与构建产物，只放行 `.env.example` 模板。**请勿把填好 Key 的 `.env` 提交上来。**

---

## 目录结构

```
ai-agent-learning/
├─ .gitignore                     # 根级忽略规则，递归生效
├─ 游戏角色npc agent/             # ① NPC 对话 Agent
│   ├─ app/{agent,api,tools,world,memory,llm,evaluation,content}/
│   ├─ tests/{selfcheck.py,smoke.py}
│   └─ web/                       # 调试前端
├─ 旅行攻略Agent/travel-agent/    # ② 旅行攻略 Agent
│   ├─ app/{agent,api,tools,providers}/
│   ├─ tests/{selfcheck.py,e2e_llm.py,llm_probe.py}
│   └─ web/                       # 单文件前端
├─ 红果追剧小程序/                # ③ 微信小程序
│   ├─ miniprogram/{pages,components,services,data}/
│   └─ tools/{check-project.py,logic-test.js}
└─ 英语单词闯关/                  # ④ 单词闯关小游戏
    └─ wordgame/{index.html,data,tools}
```

---

## 工程习惯

这四个项目共用同一套约定，算是这个仓库想记录下来的部分：

- **模型只做语言理解与领域知识，数字一律由代码算**（距离、时长、预算、关系值都不交给模型）。
- **依赖单向**：`api → agent → tools/providers → 数据层`，换数据源不影响业务逻辑。
- **工具层永不抛异常**，失败返回结构化错误，由上层降级并在响应里**留痕**（不静默降级）。
- **每个项目自带自检脚本**，不依赖网络即可跑通（`tests/selfcheck.py` 等）。
- **不让模型直接改世界状态**：写操作统一过一个校验闸门。
