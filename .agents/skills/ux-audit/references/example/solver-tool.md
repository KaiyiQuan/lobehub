# Worked example — Constraint Solver 工具结果卡（chat 内 builtin tool Render / Inspector）surface audit

对 `lobe-solver` builtin tool 在聊天流里的呈现做审计：Inspector 一行摘要 + solve 结果卡（可行 / 限时 / 不可行 / 规格错误）+ verify 校验卡。2026-09-16。

**Layers run:**

- L1 (static / code) ✅：`packages/builtin-tool-solver/src/client/**`、`src/types.ts`、`packages/locales/src/default/plugin.ts:460-492`。
- L2 (visual) ✅，但只覆盖部分状态：用的是验收 ab8a252e 的 5 张真实聊天截图，暗色、1280×577 桌面宽。已覆盖的状态：最优解、不可行、verify 全通过。没截到的：`feasible_timeout`、规格错误、多个候选方案、亮色、窄屏。
- L3 (dynamic) ⏳ 未跑。

Surface = 一次求解在聊天里留下的全部痕迹：solve 的 Inspector 行和 Render 卡，模型的深度思考块，verify 的 Inspector 行和 Render 卡，以及模型的最终文字回答。L2 看到的就是这几块依次排列（截图 `000da0`、`7bc4e5`、`4c5d4b`）。

## 0 — Surface class：同类产品的这张卡通常具备什么

同类参照：

- ChatGPT / Claude 的工具步骤：默认折叠成「Searched 5 sites」这类一行摘要，答案写在正文里。
- Google Flights / Kayak 的方案卡：用「最佳 / 最便宜 / 最快」标签，按差异点对比几个方案。
- Timefold 这类求解器产品：展示硬约束 / 软约束分别满足了多少，以及约束匹配分析。

据此列出这类 surface 应有的能力，再逐条对照代码和截图：

| 期望能力                                                                 | 现状                                                                                 |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| 工具步骤默认折叠，答案在正文里，卡片只作证据                             | ⚠️ Inspector 做到了；Render 默认展开，并且和模型的回答内容重复（gap ④）              |
| 展示「我理解的需求」，而且用户能纠正（从自然语言抽取需求本身就可能出错） | — 缺失。只有一组从 spec 推出来的「满足的约束」子集（gap ①）                          |
| 多个方案按取舍差异对比，并标出推荐项                                     | — 缺失。候选方案竖着排列，彼此只差一个价格（gap ⑥）                                  |
| 无解时由用户选择放宽哪一条                                               | — 缺失。放宽建议只是纯文本，由模型自己选定并在事后告知（gap ②）                      |
| 用一个标记说明结果的保证强度（最优 / 已校验），细节按需展开              | ⚠️ 最优和限时的区分做得好；「已校验」却是另一张独立的卡，把 13 行全部列出来（gap ⑤） |
| 用人能读懂、已本地化的文案                                               | — 缺失。JSON 路径、snake\_case 键名、pack id、服务端英文原文都直接露给用户（gap ③）  |

## 1 — Patterns in use

| Pattern (family)                         | Where                                                                                              | Rating | Note                                                                                                                  |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------- |
| Loading Indicator (feedback)             | `Inspector/Solve.tsx:26-34,41`：参数流式期间和执行中都显示 `shinyText`                             | ✅     | 进行中有反馈，不是一片空白                                                                                            |
| Overview + Detail (data)                 | Inspector 一行摘要 `Inspector/Solve.tsx:40-68` → 展开后是 Render 卡                                | ✅     | **亮点**，见 §2                                                                                                       |
| Status honesty (feedback)                | `Render/Solve/index.tsx:36-43` 显示 info Alert，`CandidateList.tsx:121-123` 显示 time-limited 徽章 | ✅     | **亮点**，见 §2                                                                                                       |
| Same-page error (feedback)               | `Render/Solve/index.tsx:25-27,60-69`、`Render/Verify.tsx:34-36`                                    | ⚠️     | 有 error 分支，但文案是原始字符串（gap ③）                                                                            |
| Card (layout)                            | `CandidateList.tsx:106-201`                                                                        | ⚠️     | 没有 `Block` 容器，12px 灰字直接铺在聊天背景上（L2 `000da0`）；同类工具如 web-browsing、image-generation 都用 `Block` |
| Accordion / Collapsible (layout)         | `CandidateList.tsx:177-183` 逐日行程开关                                                           | ⚠️     | 手写 `<span role='button'>`，用 ▸/▾ 文本字符，不可用键盘操作（gap ⑦）                                                 |
| Datatips / progressive disclosure (data) | `Render/Verify.tsx:65-67` 只有失败项才显示 detail                                                  | ⚠️     | 方向对，但通过的 13 行仍全部列出（gap ⑤）                                                                             |
| Titled Sections (layout)                 | `CandidateList.tsx:127-173` 路线 / 交通 / 住宿 / 花费 / 约束                                       | ⚠️     | 用标签加冒号拼成一行字，事实之间没有层级（L2 `000da0`）                                                               |
| Preview / Button Groups (commands)       | 不可行时的放宽选项                                                                                 | —      | 缺失，本应存在（gap ②）                                                                                               |
| Grid of Equals / comparison (data)       | 多候选方案                                                                                         | —      | 缺失，本应存在（gap ⑥）                                                                                               |

## 2 — Strengths / good cases（don't regress）

- **✅ 亮点：限时结果不会冒充最优解。** `feasible_timeout` 分支渲染 info Alert「在 N ms 时间限制内找到的最佳方案，未证明全局最优」（`Render/Solve/index.tsx:36-43`），每个候选方案还额外带一个 time-limited 徽章（`CandidateList.tsx:121-123`）。Inspector 用的也是单独一档状态文案「限时内最优」（`Inspector/Solve.tsx:14-19`，locale `inspector.status.feasibleTimeout`）。求解器给出的保证有强弱之分，这里把强弱如实交给了用户，没有把「找到一个」包装成「找到最好」。这条值得保留下来作为 ✅ 范例：**状态文案必须如实表达保证强度**（已回灌到 ux Read §1.14）。
- **✅ 亮点：Inspector 一行摘要可以单独读懂结果。** 执行中是 shimmer；完成后显示「状态 + 最关键的数字」：最优解带价格、不可行带冲突数、校验带 13/13（`Inspector/Solve.tsx:50-67`、`Inspector/Verify.tsx:37-50`）。L2 截图 `1ee126` 显示「不可行 (1 个冲突)」，`000da0` 显示「最优解 ($306)」，`7bc4e5` 显示「通过 (13/13)」。用户不展开卡片也知道结果，这正是聊天里工具步骤应有的折叠形态。卡片重设计时，这一行应该升级为**默认可见的主体**。
- **✅ 不可行是正常结果，不是报错。** `infeasible` 用 warning Alert 加结构化冲突列表展示（`Render/Solve/index.tsx:52-58`），和 `error` 分支区分开。求解器说「没有方案」是有价值的答案，不是故障。
- **✅ 预算对比放在价格旁边。** 「$306 / $1,400 预算内」（`CandidateList.tsx:110-120`，L2 `000da0`），超预算时有专门文案。用户看一眼就能判断方案是否在预算内。

## 3 — Experience gaps（ranked）

### ① 🔴 「满足的约束」只是 spec 的一个子集，却以「已满足」的口吻展示；而最主要的失败模式（需求抽取错误）用户完全看不到。违反 Read §1.12（标签必须对每个成员都成立）和 Certainty

- **证据 L1**：`CandidateList.tsx:97-104` 只从 spec 里挑出 budget、cuisines、roomType、houseRule、transportation 五项来拼「满足的约束」标签，出发地、目的地、日期、人数、城市数、软偏好都不显示。
- **证据 L2**：`000da0` 这条请求里包含路线、日期、人数，卡片上只显示了 `budget ≤ $1,400`。
- **为什么是 🔴**：formulation 实验里，oracle-free 设置下有 5/30 失败，全部是抽取错误（「必须吃到的菜系」写进了 soft.preferredCuisines，roomType 丢失）。模型抽错时，求解器照样返回最优解、verify 照样 13/13 通过，卡片还会给抽错的约束贴上「满足」。结果这张卡替一个错误方案做了担保，用户却无从发现。
- **修复**：卡片最上方放「我理解的需求」，列出全部抽取字段，用人能读懂的方式写明哪些是硬约束、哪些是偏好，并给出纠正入口（例如点某个字段直接发一句修正）。「满足」这个说法，只用在经过独立校验的条目上。

### ② 🟠 不可行时没有给用户决策入口：放宽建议只是文字，模型自己挑一条放宽并在事后说明。违反 Act §3.1 和 Surface contract

- **证据 L1**：`InfeasibleResult.tsx:41-52` 把 `suggestedRelaxations` 渲染成 `· 文本` 列表，没有任何动作。
- **证据 L2**：`1ee126` 里冲突卡下方没有按钮；`4465f0` 里模型的回答写着「我改动了什么（明确告知）… 预算从 $100 放宽到 $306」。
- **为什么是问题**：预算是用户明确给出的硬约束，放宽到 3 倍是替用户做了取舍。事后告知，不等于让用户做了选择。话题 tpc\_Go6nOgWVtToC 里认定这个方向最有产品价值的一环，正是「松哪个由用户谈判」。
- **修复**：每条放宽建议渲染成可点击的选项（例如「预算放宽到 $306」「减少一个城市」）；点击后以用户身份发出选择，或者走 user-interaction 的询问。提示词同步改为：放宽用户给出的硬约束之前先询问，除非用户事先授权。

### ③ 🟠 机器标识和未本地化的服务端英文直接出现在用户可见的界面上。违反 Feedback §4.5（本次审计把它扩展到非错误状态）

- **JSON 路径**：`$.budget`（`InfeasibleResult.tsx:34-38`，L2 `1ee126`）。
- **snake\_case 校验键名**：`is_reasonable_visiting_city`、`is_valid_restaurants` 按原样用等宽字体显示（`Render/Verify.tsx:62-64`，L2 `7bc4e5`）。
- **pack id 直接拼进句子**：「已完成 travelplanner 求解」（`Inspector/Solve.tsx:47` + locale `apiName.solve.completed: 'Solved {{pack}}'`，L2 `1ee126`），中文语序别扭，还暴露了内部 id。
- **服务端英文原文**：冲突说明「budget 100.00 is below the minimum feasible plan cost 306.00 …」和放宽建议「raise $.budget to at least …」直接渲染（`InfeasibleResult.tsx:40,47`，L2 `1ee126`），出现在中文界面里。
- **前端写死的英文**：`budget ≤`、`cuisine:`、`room type:`、`house rule:`（`CandidateList.tsx:99-104`），以及交通枚举 `self-driving`（`CandidateList.tsx:37-48`，L2 `000da0`）。
- **规格错误原样输出**：`pluginState.error` 直接作为 Alert 的 extra 显示（`Render/Solve/index.tsx:63-66`），这是写给模型的修复信号，不是写给人看的。
- **修复**：约束键、校验项、pack 都用 i18n 映射成人读的名称，未知键再回退到原值；服务端返回结构化的 `{ kind, params }`，由前端本地化句子；规格错误对用户只显示一句「正在修正需求」，原始违规明细放到 Debug / 检查入口里。

### ④ 🟠 结果卡没有明确分工，而且和模型的回答重复，信息量还不如回答。违反 Surface contract（chat）和新增的 Read §1.14

- **证据 L2**：`000da0` 和 `4c5d4b` 里，卡片用 12px 灰字列出路线、交通、住宿、花费，紧接着模型的 markdown 回答又写了一遍：「3-Day Tucson Itinerary — Oakland → Tucson」「Constraints honored ✅ …」，层级、可读性都比卡片好。用户读到同样的信息两遍，而更好的一遍在后面。
- **修复**：先定下卡片的职责，二选一：

  - (a) 卡片是证据：默认折叠成 Inspector 那一行，展开后只放正文里没有的东西，比如需求理解、保证强度、候选对比。
  - (b) 卡片是方案本身：结构化的行程视图作为主产物，提示词要求模型不再复述行程，只补充说明。

  两种都行，但不能两边都讲一遍。

### ⑤ 🟡 一次求解被拆成三块，verify 把 13 个全通过项逐行列出

- **证据 L2**：`7bc4e5` 里依次是求解步骤、深度思考、「方案校验：通过 (13/13)」加绿色 Alert，下面还有 13 行 snake\_case。
- **证据 L1**：`Render/Verify.tsx:52-71` 无论通过与否都渲染全部行。
- **修复**：独立校验本质上是给结果加的一个信任标记，应该并进 solve 卡，显示为「已独立校验 13/13 ✓」；只有失败项默认展开，通过项放进展开区。

### ⑥ 🟡 多个候选方案竖向堆叠，只有价格不同；没有差异对比，也没有推荐标记。违反 Grid of Equals 和同类产品惯例

- **证据 L1**：`CandidateList.tsx:212-228` 按顺序渲染，候选之间只用一条 divider 分隔；`maxCandidates` 默认 3（`manifest.ts`）。
- **L2 待确认**：本轮截图都只有 1 个候选。
- **修复**：每个候选标出和其他方案的差异（更便宜 / 不用自驾 / 住宿评分更高），第一个标为「推荐」。

### ⑦ 🟡 手写的折叠开关和卡片外壳，没有复用标准组件。违反「Compose the canonical surface component」（ux SKILL.md）和 Read §1.10

- **证据 L1**：`CandidateList.tsx:177-183` 是 `<span role='button' onClick>`，用 `▸`/`▾` 文本字符，没有 `tabIndex` / `onKeyDown`，键盘用户打不开逐日行程。整张卡没有 `Block` 容器，间距是写死的 px（`gap={6}`、`fontSize: 12`）。同一目录下的兄弟工具（web-browsing、image-generation、cloud-sandbox 的 Render）都用 `Block`。
- **修复**：换成标准 Accordion / Collapse 加 `Block`，与其他工具卡保持一致。

### ⑧ 🟡 展示层的推导和格式化有隐患

- **路线推导**：`cities = plan.filter((day) => day.days % 2 === 0)`（`CandidateList.tsx:79-81`）假设偶数天就是所在城市。对 5 天 / 7 天、多城市的行程可能推错，导致路线行误导用户（待 L3 用多城市查询确认）。
- **币种写死**：`formatCost` 固定 `$` 和 `en-US`（`client/utils.ts:1-2`），其他领域包或其他币种会显示错。
- **时间单位**：限时提示把原始毫秒数插进句子（`Render/Solve/index.tsx:39-41`，locale `feasibleTimeoutNotice: '… {{ms}} ms …'`），应换成人读的秒数（Read §1.5）。
- **空结果**：`optimal` 但 `candidates` 为空时渲染空白（`Render/Solve/index.tsx:44-48`）。

## 4 — Skill feedback（回灌 ux）

新增的、可推广的规则：

- **Read §1.14（新增）：聊天里的 agent 工具结果卡要有明确职责，它是证据，不是第二份回答。** 默认折叠成可以单独读懂的一行；和正文不重复；结果的独立校验并进被校验的结果里；需求抽取可能出错时，展示「agent 理解的需求」；状态文案如实表达保证强度。本次 gap ① ④ ⑤ 作为 ❌，`feasible_timeout` 的处理作为 ✅。
- **Feedback §4.5（扩展）**：「不暴露内部 id、不输出未本地化的服务端文案」从错误态扩展到所有渲染状态，包括 JSON 路径、snake\_case 键名、枚举原值和 pack id。本次 gap ③ 作为 ❌。
- **Act §3.1（扩展）：agent 替用户在其明确约束之间做取舍（放宽预算、删掉条件）时，要先把选择交给用户，不能改完再告知。** 本次 gap ② 作为 ❌。

复现的已有规则：Read §1.10（手写折叠开关、缺少 `Block`，gap ⑦）、Read §1.5（原始毫秒数，gap ⑧）。

## 5 — Layers pending

- **L2**：`feasible_timeout`、规格错误、多个候选方案这几个状态的渲染；亮色主题；窄屏或移动端下标签是否换行拥挤。
- **L3**：用 5 天 / 7 天多城市查询验证路线推导（gap ⑧）；用键盘操作逐日行程开关（gap ⑦）；重设计后验证点击放宽选项能否把选择交还给 agent 并完成重新求解（gap ②）。
