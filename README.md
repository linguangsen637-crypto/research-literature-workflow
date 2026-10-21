# Research Literature Workflow

**English** | [简体中文](README.zh-CN.md)

**A configurable workflow for managing research literature and knowledge.** Collect papers, read with traceable evidence, synthesize findings across studies, and safely organize research notes across disciplines and projects.

Zotero manages paper identities and metadata. Obsidian stores knowledge and personal notes. The current AI session performs analysis, while Python scripts handle source checks, duplicate detection, caching, change plans, and safe writes.

The research domain, questions, Zotero library, note directory, PDF source directories, and tags are configurable. No topic-specific tags or personal paper library are bundled. The current scope is literature and knowledge management; it does not automatically run experiments, perform statistical inference, train models, or search every disciplinary database.

## Workflow

1. **Collect — paper-collector:** Resolve a DOI, arXiv identifier, or local PDF; check sources and versions, detect duplicates, and prepare an import preview.
2. **Analyze — paper-analyzer:** Extract PDF text by page, read around a research question, and distinguish author-reported findings, interpretations, and research suggestions.
3. **Save — obsidian-writer:** Preview changes, then apply a reviewed plan through SafeWriter while protecting personal content.
4. **Synthesize — research-connector:** Retrieve relevant papers and notes, then compare evidence, methods, and conditions of applicability.
5. **Maintain — research-librarian:** Report missing content, duplicates, version changes, and note conflicts in read-only mode.

Skill sources are in `workflows/skills/`. They are distributed with this repository but are not automatically installed into your personal skill directory. Install or reference them through your agent tool's skill discovery mechanism. Running these skills requires an environment that can read the local repository and execute Python.

## Getting started

The current implementation supports Linux/POSIX with Python 3.10+. Safe writes depend on `fcntl` and POSIX filesystem semantics; native Windows is not supported. PDF extraction and page inspection require the system Poppler tools `pdftotext` and `pdftoppm`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -B scripts/init_workspace.py
```

Initialization only creates local configuration files. It does not access Zotero or modify the Vault, and repeated execution preserves existing configuration. Edit the generated files under `config/`:

- `research.yaml`: Project name, discipline, research question, explicitly allowed external PDF directories, and optional official download hosts.
- `zotero.yaml`: The actual local server identity, user/group library identity, and collection identity.
- `obsidian.yaml`: The absolute path to an existing Vault, a dedicated research subdirectory within it, and an independent backup directory.
- `taxonomy.yaml`: Your own tags and aliases. Empty by default, with no predefined disciplinary taxonomy.
- `models.yaml`: Analysis settings. The default uses the current session without a preset model or an independent API call.

Paths must be explicit. The workflow does not guess libraries, expand the configured read scope, or move existing data. `project.domain` and `project.question` provide context for the analysis session; they do not automatically trigger domain inference or literature searches.

After configuration, run these commands from the repository root:

```bash
.venv/bin/python -B scripts/research.py audit
.venv/bin/python -B scripts/research.py audit --zotero
.venv/bin/python -B scripts/research3.py --help
```

The first audit checks local configuration and files. Adding `--zotero` also checks the local API. Replace the example configuration placeholders before use. You may place the repository in any permitted directory; its root remains the fixed workspace for that installation.

## Example requests

- "Collect the paper for this DOI and show me an import preview first."
- "Read this paper closely around my research question, stating the reading scope and evidence gaps."
- "Compare these studies' designs, samples, methods, and limits of inference."
- "Save the reviewed analysis to my research notes and preserve My Notes."
- "Check my library and notes for missing content or conflicts without changing anything."

See the [workflow guide](docs/workflow.md) (Chinese) for command entry points and data flow. The research question and source material determine what to examine: samples, data, and protocols for empirical work; assumptions, definitions, and derivations for theoretical work; and materials, sampling, and interpretive evidence for qualitative work. Mark unreported or inapplicable items explicitly.

## Notes and evidence

The default template, `templates/paper-analysis-bilingual.md`, uses six sections of Chinese analysis with original-language evidence and Chinese translations. This is a customizable default; users may specify another language or structure. Formula and experiment fields do not require every study to contain formulas or experiments.

Paper notes use the complete original title and are stored directly in the configured research root. Invalid filename characters are minimally replaced and the adjustment is recorded; the body heading and metadata retain the original title. A filename collision between different paper identities stops the operation rather than overwriting a note. Existing notes are located by stable identity. Renaming and migration require separate review; old notes are not moved automatically. Synthesis documents use the `Topics`, `Methods`, or `Research` subdirectories.

A paper's stable identity is `library type + library ID + item key`. PDF evidence is additionally bound to a SHA256 hash, version, and page number. Quote-location checks establish where evidence appears, not whether a study's conclusions are correct. Model outputs remain `Unverified` until independently reviewed.

## Write boundaries

The default is read-only, with background synchronization disabled. Writes require a saved plan, its complete approval hash, version preconditions, backups, and read-back verification. Updating an existing note requires `reviewed_update` and confirmation that the editor is quiescent. `My Notes` is preserved, and edits to managed blocks are reported as conflicts. File locks cannot coordinate all concurrent writes from ordinary editors.

Zotero access is restricted to `http://127.0.0.1:23119/api/`. Redirects are rejected, and SQLite is not accessed. Initial write authorization uses the official dialog. Duplicate handling produces a review package; actual merging uses Zotero's native feature with confirmation for each case. Deletion must not be used to simulate a merge.

See [safety and recovery](docs/safety.md) (Chinese) for further constraints. The repository does not install sandbox permissions, alter system security settings, add login startup entries, or create scheduled tasks.

## Validation and limitations

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use temporary directories and mocked interfaces. Configuration portability also has an offline end-to-end check. These checks do not establish that real Zotero writes, Vault updates, or recovery have been validated on a new machine.

Metadata resolution currently supports DOI/Crossref and arXiv. Local PDFs are parsed with Poppler. Other sources, scanned documents without a text layer, OCR, and complex formulas need separate handling. Additional official download hosts must be explicitly configured in `sources.https_hosts`; HTTPS, hostname, and public-address checks remain enforced. Access restrictions are not bypassed. Metadata downloads use standard proxy environment variables without a preset personal proxy port. Cross-disciplinary configuration does not imply support for every publisher or database.

The optional `analyze-api` interface has not been live-tested. It requires explicit model configuration, credentials, and permission to make a call; it is not used by default. `analysis-note-plan` currently renders a brief evidence section and must not be treated as a complete six-section final analysis. Background synchronization and scheduled maintenance are not enabled automatically.

The repository excludes paper full text, private notes, real library identifiers, credentials, caches, and personal acceptance logs. Local configuration files are excluded by `.gitignore`; review the actual staged file list before uploading.
