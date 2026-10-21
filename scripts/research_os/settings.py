"""Portable workspace settings. Paths grant no permissions by themselves."""
from pathlib import Path
import yaml
from .core import SafetyError, checked

WORKSPACE = Path(__file__).resolve().parents[2]


def config(name):
    if name not in ('research', 'obsidian', 'zotero', 'taxonomy', 'models'):
        raise SafetyError('unknown configuration name')
    path = checked(WORKSPACE / 'config' / (name + '.yaml'), WORKSPACE)
    if not path.is_file():
        raise SafetyError('missing config/' + name + '.yaml; run scripts/init_workspace.py first')
    result = yaml.safe_load(path.read_text())
    if not isinstance(result, dict):
        raise SafetyError('configuration must be a mapping')
    return result


def absolute_path(value):
    if not isinstance(value, str) or not value or value.startswith('/REPLACE_'):
        raise SafetyError('configure an explicit absolute path')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SafetyError('configuration paths must be absolute')
    return checked(path, path)


def obsidian_paths(cfg):
    vault = absolute_path(cfg['vault']['path'])
    checked(vault, vault, must_exist=True)
    relative = Path(cfg['research']['root'])
    if (relative.is_absolute() or relative == Path('.') or '..' in relative.parts
            or '.obsidian' in relative.parts):
        raise SafetyError('notes root must be a dedicated relative directory inside Vault')
    root = checked(vault / relative, vault)
    backup = absolute_path(cfg['backup']['path'])
    if any(backup.is_relative_to(p) or p.is_relative_to(backup) for p in (WORKSPACE, vault)):
        raise SafetyError('backup must be outside and independent of workspace and Vault')
    return vault, root, backup


def source_roots():
    """The workspace plus explicitly configured read-only PDF directories."""
    values = config('research').get('sources', {}).get('pdf_roots', [])
    if not isinstance(values, list):
        raise SafetyError('sources.pdf_roots must be a list')
    return (WORKSPACE, *(absolute_path(value) for value in values))


def approved_pdf_path(value):
    path = Path(value).absolute()
    for root in source_roots():
        if path.is_relative_to(root):
            return checked(path, root, must_exist=True)
    raise SafetyError('PDF outside configured source roots')
