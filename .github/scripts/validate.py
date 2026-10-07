"""Validate repository artifacts without executing applications or connecting to services."""
import ast
import json
import pathlib
import subprocess
import xml.etree.ElementTree as ET
import yaml

root = pathlib.Path('.')
files = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
checked = 0
for name in filter(None, files):
    path = root / name
    if not path.is_file():
        continue
    suffix = path.suffix.lower()
    if suffix == '.py':
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=name)
    elif suffix in {'.json', '.ipynb'}:
        document = json.loads(path.read_text(encoding='utf-8-sig'))
        if suffix == '.ipynb':
            assert isinstance(document.get('cells'), list), f'{name}: invalid notebook cells'
            assert document.get('nbformat') == 4, f'{name}: unsupported notebook format'
    elif suffix in {'.yaml', '.yml'}:
        list(yaml.safe_load_all(path.read_text(encoding='utf-8-sig')))
    elif suffix in {'.xml', '.svg'}:
        ET.parse(path)
    elif suffix == '.sql':
        import sqlglot
        sqlglot.parse(path.read_text(encoding='utf-8-sig'), read='postgres', error_level=sqlglot.ErrorLevel.RAISE)
    else:
        continue
    print(f'Validated: {name}')
    checked += 1
print(f'{checked} structured/source files validated; no application, database, or paid AI calls executed.')
