<!-- ABOUTME: 按章写作改造的过程性 spec：问题诊断、章节层结构、生成语义、API 与 GenOffice 融合。 -->
<!-- ABOUTME: 按 CLAUDE.md 的文档纪律，实施并入 architecture.md / schema-contract.md 后删除本文件。 -->

# 按章写作改造（过程性 spec）

> 状态：待评审。本文件只在改造实施期间存在；并入 `docs/architecture.md` 与
> `docs/schema-contract.md` 后删除。已实现部分见 §9 的「已落地」列。

## 1. 为什么现有实现不支持按章写

现有实现**已经有「整节生成」**，但把「节」同时当成了**生成单位**和**工作单位**，
于是真实写作节奏（一次写一章，章内多节，可能写一半停下）没有对应物：

| 断点 | 事实 | 位置 |
| --- | --- | --- |
| 一次请求只能跑一节 | 服务端按 `section_key` 编排，响应就是这一节的正文 | `api/app.py` 的 `generate_section`（第 695 行） |
| 失败是「整节全或无」 | 任一可生成块未 ready 即 422「本页未能完整生成，报告未发生修改」 | `api/app.py`（第 878 行附近）→ `unsuccessful_generation_results` |
| 没有跨节编排 | 生成单位只有节/块，没有「这一章的 8 节按序跑」的对象 | `llm/generate_all.py` 的 `generate_all`（第 277 行）只接受 `section_key` |
| 额度按节计、无章视图 | 重写额度以 `(report_id, section_key)` 记账 | `persistence/section_generations.py` 的 `rewrite_allowance`（第 578 行） |
| 新鲜度只在节上 | 只有 `/sections/{key}/generation-freshness` | `api/app.py`（第 914 行） |
| 编辑面是整本文档流 | 一页渲染全报告 + 目录锚点滚动，没有「章」的边界、状态与交接 | `report-document-page.tsx`（第 71 行起） |
| 交付物只有整本 | 只有整本 Word + 审阅版 | `export/docx_renderer.py` 的 `render_final_docx`（第 1746 行） |

结论：缺的不是「再包一层循环」，而是**章节作为一等对象**——它需要有身份、状态、进度、
额度和交接面；同时**不能**把正文真相从 `Report` 挪走。

## 2. 结构决定：章是编排单位，不是正文单位

三条不变量（改造全程不得破坏）：

1. **写回仍是整节原子提交**。章批次逐节调用既有的 `reserve_batch` / `complete_batch`，
   不新增第二条正文写回通道。`Report`（`report_states`）仍是唯一正文 SSOT。
2. **章不复制正文**。章节台账只存身份、序位、标题、生命周期与「最近一次成功批次的指纹」。
3. **失败语义从「整章全或无」改为「逐节推进 + 章级裁决」**。一节失败不再回滚整章，
   但也不会被伪装成成功：批次终态是 succeeded / partial / failed / cancelled，逐节 reason code 进账本。

单位三层：

```
章 Chapter（批次编排、进度、额度、交接）
  └─ 节 Section（原子提交单位：一次生成、一次 write-back、一次 state_seq 递增）
       └─ 块 Block（并发单位：结论块先落定，其余按支柱分阶段并发）
```

## 3. 流程

```
用户在章台账点「生成本章」
        │  POST /chapters/{key}/generations（mode=missing|all）
        ▼
enqueue_chapter_generation
  · 以库内 state 重建 Report（不信任客户端 Report）
  · 派生章节与节清单：missing=仍有未落定块；all=全部可生成节
  · ensure_chapter_ledger（台账行）+ 建 run（一个活动批次/章）
        │  worker 领取（FOR UPDATE SKIP LOCKED，租约 30 分钟）
        ▼
run_chapter_generation
  for 每节（文档顺序）：
      取消位？ → 停，剩余节保持 pending
      续租（失败即交还）
      按库内 state 重建 Report ← 上一节提交的正文成为下一节的已披露事实
      材料门控 fail-closed 检查 → reserve_batch（额度/幂等/state_seq）
      generate_all(section_key) → complete_batch（原子写回 + state_seq+1）
      record_step（ready/failed/skipped + reason code + state_seq）
  收尾：章指纹 = 各节输入指纹的稳定聚合；章级裁决；report_events 记一笔
```

关键设计点：

- **每节开始前重建 Report**，而不是整章共用开跑快照。这样「先写的节」自然成为
  「后写的节」的 `prior_disclosures`，章内不会重复叙述。
- **每节一个确定性幂等键**（`uuid5(run_id, section_key)`），批次内重试命中同一行，
  跨节互不冲突；整节批次的「同键同范围才重放」语义保持不变。
- **取消不是硬中断**：当前节照常落库，下一节开始前收手。用户可理解，账本可核对。
- **租约过期不自动重排队**：判 failed 并把控制权交回用户，避免一个总超时的章无限烧额度。

## 4. 数据模型

两张表，加入 `supabase/migrations` 的唯一基线（仓库只有一份基线，见
`backend/tests/test_schema_baseline.py`）：

- `report_chapters`：`(report_id, chapter_key)` 主键；`ordinal` / `title` / `module_id`
  随骨架同步；`lifecycle`（draft/reviewing/locked）与 `last_generated_*`、`input_fingerprint`
  **不被骨架同步覆盖**（否则改一次标题就把「已锁定」重置成草稿）。
- `chapter_generation_runs`：批次信封。`expected_section_keys`、`steps`（逐节进度 JSONB）、
  `current_section_key`、租约三列、`cancel_requested`、`failure_code`。
  两个关键索引：
  - 幂等：`(account_id, idempotency_key)` 唯一；
  - 单活动批次：`(report_id, chapter_key) WHERE status IN ('queued','running')` 唯一。

章节身份沿用 `Section.key`（顶层 H1 的 key），因此**存量报告零迁移**：台账首次读取时
按骨架补行即可。

## 5. API 合同

| 方法 | 路径 | 语义 | 主要错误 |
| --- | --- | --- | --- |
| GET | `/api/reports/{id}/chapters` | 章台账：完成度、新鲜度、生命周期、下一步动作、汇总、活动批次 | 403/404 |
| POST | `/api/reports/{id}/chapters/{key}/generations` | 入队一次章批次，返回 202 + 进度投影 | 403 chapter_locked / 409 chapter_busy·state_conflict / 422 chapter_empty |
| GET | `/api/reports/{id}/chapters/{key}/generations/{run_id}` | 轮询进度 | 404 |
| POST | `.../generations/{run_id}/cancel` | 请求停止 | 404 |
| PATCH | `/api/reports/{id}/chapters/{key}` | 推进/回退生命周期 | 404 |
| GET | `/api/reports/{id}/chapters/{key}/workcopy` | 章工作稿 Markdown + 块锚点清单 | 403/404 |
| GET | `/api/reports/{id}/chapters/{key}/workcopy.docx` | 章工作稿 .docx（GenOffice 生成） | 503/502 |
| POST | `.../workcopy/revisions?state_seq=` | 回读改过的 .docx，返回块级变更提议 | 409/422/503 |

错误响应只承载稳定 code 与代码侧文案（沿用 `docs/schema-contract.md` 的既有契约）。

## 6. 前端改造（尚未实现，方案如下）

- 新增**章节台账页**：一行一章，列为 完成度条 / 状态（未开始·部分·已完成·固定·过期）/
  下一步动作按钮 / 生命周期 / 最近生成时间。顶部汇总「还剩 N 章、M 个待写块」。
  数据只取 `GET /chapters`，不在前端累加。
- 新增**章工作区**：左侧章内节列表（含每节状态），中间复用现有块编辑组件只渲染本章，
  右侧显示本章材料与检查项。章内「生成剩余小节」调 POST 批次，轮询同一 run。
- **报告正文页保留**：它是「整本通读与整本导出」的出口，章工作区是「写作与交接」的出口；
  两者共用同一份 `Report` 与块编辑动作，不引入第二套编辑状态。
- 界面上「整节重写」保留为局部修补动作；批量写作入口收敛到章批次，避免两个并存的主路径。

## 7. 与 GenOffice 的融合

融合点只有一句话：**把「一章」变成可以离开网页、在 Word 里被非技术人员修改的协作单元，
再把修改收回到块级**。三层，逐层加深，均可独立启用：

### 7.1 章工作稿（已实现）

- `build_workcopy` 把一章投影成 Markdown：标题层级、字段引用解析与正式导出共用
  `inline_text` 同一套实现；受控省略块不出现。
- `render_workcopy_docx` 调 `genoffice create --type docx --from chapter.md --json`，
  得到原生 OOXML 的 .docx；清单（块 id + 文本指纹 + state_seq）随导出落盘为快照。
- `read_workcopy` 调 `genoffice docs read --json --full --comments --revisions` 取回
  段落、批注与修订；`diff_workcopy` 用序列对齐产出 modified / added / removed 提议，
  中间插入一段不会让后续段落全部变成「已修改」。
- **边界**：工作稿不是交付物。正式 Word 仍走 `render_final_docx` 与确定性闸门；
  工作稿不做结构级回读（表格、图片、标题在 v1 只读）。

### 7.2 审阅批注与修订（已实现基础能力）

`attach_review_comments` 已能按段落序号写入批注（`add_comment`），供「系统发现的待确认项」
落到 Word。下一步把诊断结果（导出闸的非阻断项）按块映射成批注，形成「带问题的审阅稿」；
修订回读已有 `--revisions` 的 blockIndex，可映射回块级提议。

### 7.3 版式与可视化 QA（未实现，建议）

`genoffice render --grid` 出章级 PNG 校样、`docs check` 检查空标题/域失配/占位文本。
定位是**附加**检查：确定性闸门仍由代码判定（架构约束二），GenOffice 不参与通过/否决。

### 7.4 与 GenOffice 的职责边界

| 能力 | 归属 | 理由 |
| --- | --- | --- |
| 正文生成 | 本仓（Agent 选材 + 逐块生成 + 守卫） | GenOffice 的 AI 改写会绕开证据门控与红字元指令边界 |
| Word 渲染（交付） | 本仓 `render_final_docx` | 交付保真是独立合同，不能被开放式编辑器能力替代 |
| Word 渲染（工作稿） | GenOffice | 只需「可被 Word 打开、可批注」，成本远低于自建 |
| 块级结构读取 | GenOffice | `docs read` 已给 blockIndex + 文本 + 批注 + 修订 |
| 正文写回 | 本仓 | 唯一写回通道是整节原子提交 |

已在本机验证的事实（GenOffice 0.11.829）：
`create --type docx --from` 输出 `{"status":"ok"}`；`docs apply` 的 `add_comment`
按 `blockIndex` 锚定并可从 `docs read --comments` 取回；`--track` 产生的插入/删除
带 `blockIndex`，可从 `--revisions` 取回；书签（`insertBookmark`）可写但**不**出现在
`docs read` 的输出里——所以对齐不能依赖书签，只能依赖「快照 + 序列对齐」。

## 8. 覆盖度与验收

新增测试（均已通过）：

- `backend/tests/test_chapter_plan.py`：章边界、节清单、完成度、续写口径、指纹聚合。
- `backend/tests/test_chapter_workcopy.py`：工作稿切片、块清单、三类变更提议、
  以及一条真实 GenOffice 往返（无 CLI 时自动跳过）验证「未改动不产生假变更」。

手工验收（需本地栈 + worker）：

1. `make supabase-up` → `make dev-backend` → `make dev-worker`；
2. 建报、填资料、处理完文件；
3. GET 章台账，确认章数 = 顶层 H1 数、待写块数 = 可生成块数；
4. 对「环境」章入队 mode=missing，观察 steps 逐节 ready 与 `state_seq` 递增；
5. 中途 cancel，确认当前节落库、剩余节仍 pending、终态 cancelled；
6. 再次入队 mode=missing，确认只跑剩余节（不重复消耗已完成节的额度）；
7. 导出章工作稿 docx，改一段并在中间插一段，回读后确认只剩 1 modified + 1 added。

## 9. 改动点清单

已落地：

| 文件 | 改动 |
| --- | --- |
| `backend/src/sustainability_desk/contract/chapter_plan.py` | 新增：`derive_chapters` / `draft_facts` / `pending_section_keys` / `chapter_fingerprint` / `following_action` / `is_generatable_block` |
| `backend/src/sustainability_desk/contract/chapter_generation.py` | 新增：章台账、批次进度、工作稿清单与变更提议的严格合同 |
| `backend/src/sustainability_desk/persistence/chapter_generations.py` | 新增：台账 upsert、入队/领取/续租/进度/取消/终结/过期恢复 |
| `backend/src/sustainability_desk/chapter_generation.py` | 新增：`enqueue_chapter_generation` / `run_chapter_generation` / `process_one_chapter_generation` |
| `backend/src/sustainability_desk/export/chapter_workcopy.py` | 新增：`build_workcopy` / `render_workcopy_docx` / `read_workcopy` / `diff_workcopy` / `attach_review_comments` |
| `backend/src/sustainability_desk/api/app.py` | 新增 8 个章级端点；`generate_section` / `section_generation_freshness` 保持原语义 |
| `backend/src/sustainability_desk/llm/generate_all.py` | 可生成块判据改为委托 `is_generatable_block`（单一口径） |
| `backend/src/sustainability_desk/lightweight_report_generation.py` | `current_generation_scope` 公开，供章批次复用同一授权口径 |
| `backend/src/sustainability_desk/export/docx_renderer.py` | 新增 `inline_text` 公开入口，工作稿与正式导出共用引用解析 |
| `backend/src/sustainability_desk/material/agent_worker.py` | 新增 job kind `chapter_generation`，过期租约恢复纳入 |
| `supabase/migrations/20260904000000_local_single_user_baseline.sql` | 新增两张表 + 三个索引 + service_role 授权 |
| `backend/tests/test_schema_baseline.py` | 受检表集合纳入两张新表 |

待实现（按优先级）：

1. 前端章节台账页与章工作区（§6）；
2. 诊断结果 → Word 批注的映射（§7.2 下半）；
3. 工作稿表格的结构化回读（需要行列级 diff）；
4. 报告级生成改为「按章循环」，让一键整本与按章写作共用一条编排（当前两条并存，
   但都落在同一批原语上，不存在第二套写回）。

## 10. 调试记录（本次实施中发现并修正）

以下问题都不是「代码写错一个字母」，而是**只有跑到真实链路上才会暴露**的结构性缺陷，
按发现顺序记录，便于后续改动时不再踩：

1. **表格单元格不能用 Plate 骨架取文本。** `GsTableCell.children` 的元素是裸 dict，
   交给 `inline_text` 会在第一个单元格处抛 `AttributeError`；受控值在 `value`。
   已改为只读 `value`（多选值按包语言的分隔符连接），并补了覆盖表格的用例。
2. **`base_state_seq` 必须与生成所用的 Report 同源。** 先按旧快照建 Report、再单独读
   「当前」序号去预留，会在两者之间发生编辑时以新序号写回旧正文——一次静默的丢失更新。
   现在 `_current_report` 同一快照返回 `(Report, state_seq)`。
3. **排队中被取消必须直接终结。** 只置取消位会让该行永久停在 `queued`，占住
   「每章单活动批次」的唯一索引，之后任何重发都被判 `chapter_busy`，用户没有自助恢复路径。
   现在取消是一条语句内把 `queued → cancelled` 并写 `finished_at`。
4. **worker 链路的观测运行需要 `contract_version`。** 单测走的是 API 路径，永远碰不到
   这个签名差异；真实批次一跑就 `TypeError`，并被外层收成 `orchestration_error`，
   看起来像编排坏了。现在从该报告钉住的契约版本读取，而不是用包的当前版本。
5. **公司业务摘要派生失败不应拖垮整批。** 摘要缺失会让正文退化成通用表述，但它不是前置条件；
   派生异常按「无摘要」继续，让每一节各自失败并留下稳定原因码。
6. **失败原因码要中性。** 块级阻断可能来自守卫、可见性或模型不可用，
   章节层只报 `blocked` / `generation_failed`，准确归因留在块级 trace。
7. **并行章批次会互踩 `state_seq`。** 另一章刚提交会把序号推高；现在遇到
   `state_conflict` 会重建 Report 与基准序号重试一次，再冲突才算失败。
8. **「续写已写完的章」不是错误。** 现在返回 `chapter_up_to_date`（409），
   与「这一章本来没有可写内容」（`chapter_empty`，422）分开。

配套的数据库集成测试在 `backend/tests/test_chapter_generation_ledger.py`：
台账同步不冲掉人工生命周期、单活动批次、幂等重放、取消后立刻可重发、领取与逐节记账、
锁章拒绝、过期租约恢复。注意 `claim_run` 是**全局领取**，单测必须按 `report_id` 限定，
否则会领到别的残留批次——那正是「静默失败伪装成运行中」的典型形态。

## 11. 浏览器实测（agent-browser 驱动真实 Chromium）

登录 → 遍历 `intake/info·scoring·metrics·questions·input-path`、`materials/processing`、
`reports`、`reports/generation`、`reports/document`，逐页核对控制台与页面错误。

发现并修复两个**既有前端缺陷**（与章节改造无关，但在本次部署中暴露）：

1. **登录页水合不匹配（dev 面板常驻「1 Issue」）。** 根因：SSR 恒以 en 渲染
   （`resolveInitialLocale` 在服务端拿不到 localStorage/navigator），客户端首帧脚本
   按浏览器语言判成 zh-Hans，整棵取词树水合即分叉（React 按客户端重画，表现为
   Recoverable Error + 首帧语言闪烁）。修法：语言偏好**双写 cookie**（`writeLocaleCookie`，
   选语言与首帧脚本都会写），新增 `lib/i18n/locale-server.ts` 的 `resolveServerLocale()`
   （cookie → Accept-Language，与首帧脚本同序），根布局改为 async 并把结果传给
   `LocaleProvider serverLocale` 作为初值——SSR 与客户端首帧严格一致；本机显式选择
   在挂载后一次性校正。清空 cookie/localStorage 后实测登录页 0 错误。
2. **Next 16 对组件内裸 `<script>` 的警告。** 根布局的首帧脚本改用官方替代
   `next/script` 的 `strategy="beforeInteractive"`（内联内容随初始 HTML 同步执行；
   依据为安装包内 Next 自带文档的 Script 组件参考）。

修复后整站实测：8 个路由 0 页面错误、0 控制台错误，dev 面板问题数 2 → 0。

流程层面的观察（未改动，记录备查）：

- 「生成报告」按钮的就绪门禁在客户端正确生效：未答议题问题时点击不会入队，
  就绪清单给出「议题信息 尚未完成 / 去填写」。
- 自动保存链路稳定：基本信息 7/7 逐项填写过程中无一次保存冲突或报错。
- 自动化教训：agent-browser 的元素 ref 在重渲染后会过期，必须**每次交互前重取快照**；
  自定义下拉需要先点击展开再 `select`，直接对收起状态的 combobox `select` 会静默无效。

### 11.1 第二轮浏览器实测（用户截图驱动的两处修复）

1. **§4.0.1「同屏 2 个 primary」守卫误报。** 用户截图来自 dev 控制台。排查结论分两层：
   - 守卫按「offsetParent 过滤」实现「对话框打开时页面主按钮不构成并列主次」，
     但顶层 `<dialog>` 并不改变被遮元素的 offsetParent——「列表页 + 打开的确认对话框」
     会被误报。修法：有 `dialog[open]` 时只数对话框内的 primary（实现守卫注释本就
     声称的语义）。已实测：新建/删除两个确认流均 0 告警。
   - 一处真实违规：生成交付页每个交付物的「下载」按钮用了 primary——成功生成后
     与「生成/更新报告」主按钮同屏构成 2–3 个 primary。下载是行级动作，改为 secondary。
2. **「Failed to fetch」浏览器原文上屏。** 报告列表加载失败时，`accountErrorMessage`
   的通用回退把 `TypeError.message`（浏览器原文）直接呈现，违反本仓「用户可见文本
   只承载稳定 code 与代码侧文案」的约束。修法：`lib/api-error.ts` 新增
   `isNetworkFailure` / `NETWORK_UNREACHABLE_MESSAGE`，`account-error-message`、
   `account-context`、`use-structured-autosave` 三处边界统一收敛为
   「无法连接本机后端服务，请确认后端已启动后刷新重试」。

另记录：用户截图中的「还没有报告 + Failed to fetch」是**预览 iframe 场景**——
WorkBuddy 内置预览以 iframe 承载 localhost:3000，跨源 fetch 被浏览器拦截属预期行为；
在真实浏览器标签页里同一页面正常（`/api/reports` 200）。
