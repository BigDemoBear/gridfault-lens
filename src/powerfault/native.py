"""Template table selections retain native block and field provenance."""
from pathlib import PurePosixPath
import re
from .core import companion_fields, pointer
from .model import CaseError

ROLES = [('故障相电压', 'voltage'), ('故障相电流', 'current'),
         ('最大零序电流', 'maximum_reported_zero_sequence_current'),
         ('最大差动电流', 'maximum_reported_differential_current'),
         ('故障测距', 'fault_distance'), ('故障相别', 'fault_phase')]


def grouped_fields(materials, record):
    groups = {}
    for field in companion_fields(materials, record):
        if field['field'] in {'native_measurement', 'fault_distance', 'fault_phase', 'protection_event'}:
            scope = field['source_scope'] or field['source'] + ':' + field['scope']
            # A scope can appear in two companion files. Never conflate sources.
            key = (field['source'], scope)
            groups.setdefault(key, []).append(field)
    sources = {s['id']: s['path'] for s in materials['sources']}
    result = []
    for (source, scope), fields in groups.items():
        group = fields[0]['evidence'].get('group', {})
        label = PurePosixPath(sources[source]).name
        if group:
            occurrence = re.search(r'\[(\d+)\]$', group['path'])
            label += ' · 原生第' + (occurrence[1] if occurrence else '1') + '段'
            if group.get('time_literal'):
                label += ' · ' + group['time_literal']
        result.append({'scope': scope, 'label': label, 'fields': fields})
    return result


def select_groups(end, materials, kind):
    record = next(r for r in materials['records'] if r['id'] == end['record'])
    groups = grouped_fields(materials, record)
    wanted = end.get('native_scopes', {}).get(kind)
    if wanted is not None:
        available = {g['scope'] for g in groups}
        if not wanted or len(wanted) != len(set(wanted)) or not set(wanted) <= available:
            raise CaseError('native_scopes must select distinct existing native blocks')
        groups = [g for scope in wanted for g in groups if g['scope'] == scope]
    return groups


def parameter_candidates(fields, role):
    phases = {f['value'] for f in fields if f['field'] == 'fault_phase'}
    result = []
    for field in fields:
        value = field['value']
        if role in {'fault_distance', 'fault_phase'} and field['field'] == role:
            result.append(field)
        elif field['field'] == 'native_measurement':
            if role in {'voltage', 'current'} and value['quantity'] == role and value['stage'] == 'fault':
                if value['phase'] and any(value['phase'] in phase for phase in phases):
                    result.append(field)
            elif role == 'maximum_reported_' + value['quantity'] and value['statistic'] == 'maximum_reported':
                result.append(field)
    return result


def protection_fields(end, group):
    return [f for f in group['fields'] if f['field'] == 'protection_event'
            and end['protection_line'] in (f['value'].get('name') or '')]


def event_reference_groups(entries, materials, cat):
    """Deduplicate source references and sort only explicitly comparable clocks."""
    fields = {'f:' + f['id']: f for f in materials['fields']}
    paths = {s['id']: s['path'] for s in materials['sources']}
    groups, seen = {}, set()
    for item in entries:
        identity = (item['name'], item.get('time'))
        if identity in seen:
            continue
        seen.add(identity)
        eid = item.get('time', item['name']).partition('#')[0]
        field = fields.get(eid)
        source = field['source'] if field else eid[2:]
        native = field['value'] if field and isinstance(field['value'], dict) else {}
        basis = item.get('time_basis', native.get('time_basis'))
        if basis in {None, '', 'source_relative_unspecified', 'NOT_STATED'}:
            basis = None
        scope = (field['source_scope'] or field['scope']) if field and 'time_basis' not in item else ''
        raw = pointer(cat[eid], item['time'].partition('#')[2]) if 'time' in item else None
        unit = ''
        if 'unit' in item:
            uid, _, upath = item['unit'].partition('#')
            unit = str(pointer(cat[uid], upath)).strip()
        match = re.fullmatch(r'\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*(ms|s|us|μs|µs)?\s*', str(raw))
        number = None
        if match:
            if match[2] and unit and match[2] != unit:
                raise CaseError('Event time literal and declared unit disagree')
            unit = unit or match[2] or ''
            if basis and unit in {'ms', 's', 'us', 'μs', 'µs'}:
                number = float(match[1])
        key = (source, scope, basis, unit)
        group = groups.setdefault(key, {'label': paths[source] + ' · ' + (basis or '时基未声明，保持原文顺序'),
                                        'items': []})
        group['items'].append((number, item))
    result = []
    for group in groups.values():
        rows = group['items']
        if all(number is not None for number, _ in rows):
            rows = sorted(rows, key=lambda row: row[0])
        result.append((group['label'], [item for _, item in rows]))
    return result
