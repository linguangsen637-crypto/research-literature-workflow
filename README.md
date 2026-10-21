# Research Literature Workflow

**一个可配置的科研文献与知识管理工作流。** 从文献收集、证据阅读、跨论文综合，到安全保存研究笔记，适用于不同学科与课题。

Zotero 管理文献身份与元数据，Obsidian 保存知识与个人思考；当前 AI 会话执行分析，Python 脚本处理来源核验、查重、缓存、变更计划和安全写入。

领域名称、研究问题、Zotero 库、笔记目录、PDF 来源目录和标签由配置决定。工具没有内置某个课题的标签或论文库。它目前覆盖文献与知识工作，不宣称自动完成实验执行、统计推断、模型训练或所有学科数据库的检索。

## 工作过程

1. **收集 paper-collector**：输入 DOI、arXiv 或本地 PDF，核对来源与版本，查重并生成导入预览。
2. **分析 paper-analyzer**：按页提取全文，围绕问题阅读，分别记录作者报告、分析推导与研究建议。
3. **保存 obsidian-writer**：先生成预览，再通过已审阅计划安全写入，保护个人内容。
4. **综合 research-connector**：围绕研究问题检索候选，比较证据、方法与适用条件。
5. **维护 research-librarian**：只读报告缺失、重复、版本变化和笔记冲突。

技能源位于 `workflows/skills/`。这些文件随仓库分发，不会自动安装到个人技能目录；使用自己的代理工具按其发现机制安装或引用。技能执行需要能够读取本机仓库并运行 Python 的环境。

## 开始使用

当前支持 Linux/POSIX，Python 3.10+；安全写入依赖 fcntl 和 POSIX 文件系统语义。Windows 原生环境尚不支持。PDF 文字提取与页面检查需要系统 Poppler（pdftotext、pdftoppm）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -B scripts/init_workspace.py
```

初始化仅创建本地配置，不访问 Zotero、不改 Vault，重复执行保留已有配置。编辑 `config/` 下生成的文件：

- `research.yaml`：项目名称、学科、研究问题、允许读取的外部 PDF 目录及可选官方下载域名。
- `zotero.yaml`：真实本地服务身份、user/group Library 与 Collection 身份。
- `obsidian.yaml`：已有 Vault 的绝对路径、Vault 内专用研究子目录、独立备份目录。
- `taxonomy.yaml`：自己的标签与别名；默认空，不预设学科分类。
- `models.yaml`：分析方式；默认交给当前会话，不预设模型、不触发独立 API 费用。

配置路径必须明确；不自动猜测文献库、扩大可读目录或移动现有数据。`project.domain` 和 `project.question` 是给分析会话读取的项目上下文，不会自动执行领域推断或文献检索。

完成配置后，从仓库根目录运行：

```bash
.venv/bin/python -B scripts/research.py audit
.venv/bin/python -B scripts/research.py audit --zotero
.venv/bin/python -B scripts/research3.py --help
```

第一次审计只检查本地配置与文件；加 `--zotero` 才检查本地 API。配置示例中的占位值不能直接使用。仓库可以放在任意受允许的目录，运行时工作区固定为这个仓库根目录。

## 使用示例

- “收集这个 DOI 对应的论文，先给我导入预览。”
- “围绕我的研究问题精读这篇论文，标明已读范围和证据缺口。”
- “比较这些研究的设计、样本、方法与结论边界。”
- “将已审阅的分析保存到研究笔记，保留 My Notes。”
- “只读检查我的文献库与笔记是否存在缺失或冲突。”

操作入口与数据流见 [工作流说明](docs/workflow.md)。学科内容由研究问题和原文决定：实证论文关注样本、数据和实验协议，理论论文关注假设、定义与推导，定性研究关注材料、取样与解释依据。未报告或不适用的内容明确标出。

## 笔记与证据

默认模板是 `templates/paper-analysis-bilingual.md`：六章中文分析、原文证据与中文对译。它是可调整的默认模板，用户可以指定其他语言或结构。模板里的公式和实验栏不是要求所有研究必须有公式或实验。

论文笔记以完整原文题名保存于配置的研究根目录；文件名的非法字符做最小替换并记录，正文标题和元数据保留原题。同名不同身份停止并报告，不覆盖。已有文件通过稳定身份查找，重命名和迁移需要独立审阅，不自动移动旧笔记。综合文档使用 Topics、Methods 或 Research 子目录。

论文身份为 `library type + library ID + item key`；PDF 另绑定 SHA256、版本和页码。引用位置检查只验证原文定位，不证明研究结论正确；模型结果在独立复核前保留 Unverified。

## 写入边界

默认只读，后台自动同步关闭。写入需要保存计划、完整批准哈希、版本前置条件、备份和回读。已有笔记需要 reviewed_update 与编辑器静默确认；My Notes 不覆盖，机器区块被修改也报告冲突。文件锁无法协调普通编辑器的全部写入竞争。

Zotero 仅使用 `http://127.0.0.1:23119/api/`，拒绝重定向，不读取 SQLite。首次写入走官方授权对话框。重复条目仅生成审阅包，实际合并使用 Zotero 原生功能并逐案确认。不得删除来模拟合并。

更多约束见 [安全与恢复](docs/safety.md)。仓库不会安装沙盒权限、修改系统安全配置、添加登录自启动或创建定时任务。

## 验证与限制

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

测试只使用临时目录和模拟接口。配置可迁移性另有离线端到端检查；这些都不代表新机器上真实 Zotero 写入、Vault 更新或恢复已验收。

目前元数据解析支持 DOI/Crossref 与 arXiv，本地 PDF 通过 Poppler 解析；其他来源、无文字层扫描件、OCR 和复杂公式需要另行处理。新增官方下载域名须在 sources.https_hosts 显式配置；HTTPS、域名与公网地址检查仍生效，不绕过访问限制。元数据下载使用标准代理环境变量，不预设个人代理端口。跨学科可配置不代表覆盖了所有出版商与数据库。

独立 analyze-api 是未实测的可选接口，需显式配置模型、凭据及调用授权；默认不调用。analysis-note-plan 当前仍输出简略证据段，不能直接当作完整六章最终分析。后台同步与定时维护不自动启用。

仓库不包含论文全文、私人笔记、真实库标识、凭据、缓存或个人验收记录。本地配置由 `.gitignore` 排除，上传前仍应审阅实际提交清单。
