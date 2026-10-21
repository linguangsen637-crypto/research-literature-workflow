#!/usr/bin/env python3
"""Create local configuration from examples without replacing existing files."""
import argparse
from research_os.core import atomic, checked
from research_os.settings import WORKSPACE


def initialize():
    created, retained = [], []
    for source in sorted((WORKSPACE / 'config').glob('*.example.yaml')):
        target = checked(source.with_name(source.name.replace('.example.yaml', '.yaml')), WORKSPACE)
        if target.exists():
            retained.append(target.name)
            continue
        atomic(target, source.read_bytes(), create_only=True)
        created.append(target.name)
    return {'created': created, 'retained': retained}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(initialize())
    print('Edit local configuration before audit; Zotero and Vault were not modified.')


if __name__ == '__main__':
    main()
