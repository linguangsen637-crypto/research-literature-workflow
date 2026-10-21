# 工作流操作说明

在仓库根目录运行命令。`state/` 存放已保存的计划和交接，`cache/` 存放可重建缓存，`logs/` 保存执行报告；它们不替代 Zotero 元数据或 Obsidian 笔记，也不随 Git 上传。

## 收集

`scripts/research3.py resolve --input <DOI或arXiv编号> --out <工作区内JSON路径>` 获取来源元数据。

`import-plan --resolved <元数据JSON> --out <计划JSON>` 查询真实 Library 并生成变更预览。导入需要再执行 zotero-apply，提供计划与完整 approval-hash，以及显式 allow-write。原文附件另走 attach-plan/attach-apply，人工核对标题、作者和版本；用户提供 PDF 不等于论文身份已验证。

## 阅读

`parse --pdf <PDF绝对路径> --out <解析JSON>` 接受工作区或 research.yaml 中显式允许的来源目录，保留页码和文字坐标。

`handoff --parsed <解析JSON> --item-key <真实条目KEY> --question <研究问题> --mode deep --out <交接JSON>` 生成当前会话的结构化阅读输入。Quick 只选部分页面；Deep 也不能冒充代码复现或完整补充材料核验。

会话先读取 research.yaml 与用户问题，再基于交接证据分析，保存符合 templates/research-handoff.schema.json 的 JSON。`validate-analysis --request <交接JSON> --analysis <分析JSON>` 检查结构和引文定位。缺失证据保留缺口，不把推测升级成作者报告。

## 综合与保存

`search --question <关键词>` 搜索目标 Collection 元数据和候选笔记。`compare --bundle <配对清单JSON> --question <问题> --out <综合输入JSON>` 校验至少两组真实 request/analysis 后生成综合输入；它本身不执行模型推理。

元数据占位预览使用 scripts/research.py note-preview；简略证据预览使用 analysis-note-plan。完整阅读笔记需要按模板整理并审阅成 Plan，再交 note-apply。现有简略渲染器不能声称已经完成双语深度笔记。

note-apply 需要保存的 Plan、完整批准哈希和 allow-write。更新已有笔记还需 editor-quiescent；该标志表达实际确认，不能代替编辑器检查。任何路径、内容、来源或前置版本变化都需要重新计划。

## 维护

`maintenance --out <报告JSON>` 实时只读检查；`maintenance-offline` 使用缓存，明确标记快照时间。维护不自动修复、导入或合并。定时运行需用户在自己的环境中另行配置和观察，手动成功不能作为定时触发验收。
