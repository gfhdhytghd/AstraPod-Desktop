"""Validate and discover API 1 plugin manifests."""
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import sys

from .sdk import API, encode


LOG = logging.getLogger(__name__)
ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
PERMISSIONS = frozenset(('usb', 'serial', 'ep0', 'keys', 'voice'))
TYPES = {'string': (str,), 'boolean': (bool,), 'integer': (int,), 'number': (int, float)}


def settings_record(plugin_id, values):
    return dict(type='settings', api=API, source=plugin_id, settings=values)


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    description: str
    entry: Path
    permissions: tuple[str, ...]
    settings_schema: dict

    def command(self):
        return [sys.executable, str(self.entry)] if self.entry.suffix == '.py' else [str(self.entry)]

    def settings(self, overrides):
        schema = self.settings_schema
        if not isinstance(overrides, dict) or overrides.keys() - schema.keys():
            raise ValueError(f'{self.id}: unknown settings or invalid settings object')
        values = {key: field['default'] for key, field in schema.items()}
        values.update(overrides)
        for key, value in values.items():
            types = TYPES[schema[key]['type']]
            if type(value) not in types or (type(value) is float and not math.isfinite(value)):
                raise ValueError(f'{self.id}: wrong type for setting {key}')
            if 'enum' in schema[key] and value not in schema[key]['enum']:
                raise ValueError(f'{self.id}: {key} must be one of its enum values')
        encode(settings_record(self.id, values))
        return values


def load_manifest(path):
    path = Path(path)
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('expected a manifest object')
    for field in ('id', 'name', 'version', 'entry', 'description'):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f'{field} must be a nonempty string')
    plugin_id = raw['id']
    if len(plugin_id) > 64 or not ID.fullmatch(plugin_id) or plugin_id != path.parent.name:
        raise ValueError('plugin ID must match its directory name')
    if raw.get('kind') != 'input' or type(raw.get('api')) is not int or raw['api'] != API:
        raise ValueError('expected an API 1 input plugin')
    root = path.parent.resolve()
    relative = Path(raw['entry'])
    entry = (root / relative).resolve()
    if relative.is_absolute() or not entry.is_relative_to(root) or not entry.is_file():
        raise ValueError('entry must name a file inside the plugin directory')
    if entry.suffix != '.py' and not os.access(entry, os.X_OK):
        raise ValueError('non-Python entries must be executable')
    permissions = raw.get('permissions')
    if (not isinstance(permissions, list)
            or any(not isinstance(item, str) or item not in PERMISSIONS for item in permissions)
            or len(set(permissions)) != len(permissions)):
        raise ValueError('invalid permissions')
    schema = raw.get('settings')
    if not isinstance(schema, dict):
        raise ValueError('expected a settings schema object')
    for key, field in schema.items():
        if (not ID.fullmatch(key.replace('_', '-')) or not isinstance(field, dict)
                or not isinstance(field.get('type'), str) or field['type'] not in TYPES
                or 'default' not in field
                or not isinstance(field.get('enum', []), list)):
            raise ValueError(f'invalid settings field {key!r}')
    manifest = Manifest(plugin_id, raw['name'], raw['version'], raw['description'],
                        entry, tuple(permissions), schema)
    manifest.settings({})
    return manifest


def discover(*roots):
    plugins = {}
    for root in roots:
        for path in sorted(Path(root).glob('*/plugin.json')):
            try:
                manifest = load_manifest(path)
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.warning('%s: %s', path, exc)
                continue
            if manifest.id in plugins:
                LOG.warning('%s: duplicate plugin ID %s', path, manifest.id)
                continue
            plugins[manifest.id] = manifest
    return plugins
