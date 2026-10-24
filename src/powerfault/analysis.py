"""Record/channel tools, independent of the two-end report template."""
from pathlib import Path
import csv
import math
import re

from . import __version__, figures
from .core import calculate, decoded, fresh, load, sha
from .model import CaseError, read_json, write_json
from .waveform import decoding_session


def checksums(folder):
    return {p.relative_to(folder).as_posix(): sha(p) for p in sorted(folder.rglob('*'))
            if p.is_file() and p.name != 'checksums.json'}


def check_files(folder):
    if checksums(folder) != read_json(folder / 'checksums.json'):
        raise CaseError('Artifact file set or hashes changed: ' + str(folder))


def _record(materials, rid):
    record = next((r for r in materials['records'] if r['id'] == rid and 'error' not in r), None)
    if record is None:
        raise CaseError('Unknown or unreadable record: ' + str(rid))
    return record


def _channels(record, groups):
    if not isinstance(groups, dict) or set(groups) != {'U', 'I'}:
        raise CaseError('channels requires U and I lists; either may be empty')
    numbers = []
    available = {c['number']: c for c in record['channels']}
    for key, units in [('U', {'V', 'kV', 'mV'}), ('I', {'A', 'kA', 'mA'})]:
        selected = groups[key]
        if not isinstance(selected, list) or any(type(n) is not int or n not in available for n in selected):
            raise CaseError('Select existing integer analog channel numbers')
        rows = [available[n] for n in selected]
        if rows and (any(c['unit'] not in units for c in rows)
                     or len({c['unit'] for c in rows}) != 1 or len({c['side'] for c in rows}) != 1):
            raise CaseError('Each U/I group must retain matching native units and side')
        numbers += selected
    if not numbers or len(numbers) != len(set(numbers)):
        raise CaseError('Select distinct analog channels')
    return numbers


def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9-]*', value):
        raise CaseError('Use an id beginning with a lowercase letter, then letters/digits/hyphens')


def measurements(work, requests, materials, sources):
    result = {}
    for item in requests:
        required = {'id', 'record', 'channels', 'samples', 'reference'}
        if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {'angle_policy', 'angle_note'}:
            raise CaseError('Measurement requires id/record/channels/samples/reference; optional angle_policy/angle_note')
        _id(item['id'])
        if 'm:' + item['id'] in result:
            raise CaseError('Measurement id repeated')
        record = _record(materials, item['record'])
        numbers = _channels(record, item['channels'])
        window = item['samples']
        if (not isinstance(window, list) or len(window) != 2 or any(type(n) is not int for n in window)
                or not 1 <= window[0] < window[1] <= record['samples']):
            raise CaseError('samples must be inclusive 1-based file rows within the record')
        if type(item['reference']) is not int or item['reference'] not in numbers:
            raise CaseError('reference must be a selected analog channel')
        policy = item.get('angle_policy', 'metadata_checked')
        if policy not in {'metadata_checked', 'uncompensated'}:
            raise CaseError('Unknown angle_policy')
        if policy == 'uncompensated' and not str(item.get('angle_note', '')).strip():
            raise CaseError('Explain use and limits of uncompensated angles')
        request = {k: item[k] for k in ('id', 'samples', 'reference', 'angle_policy', 'angle_note') if k in item}
        end = {'record': item['record'], 'channels': item['channels'], 'measurements': [request]}
        value = calculate(work, {'ends': [end]}, materials, sources)['m:e0-' + item['id']]
        result['m:' + item['id']] = value
    return result


def _samples(work, materials, sources, requests, folder):
    selected = {}
    for item in requests:
        if 'record' not in item:
            continue
        groups = item.get('channels', {})
        if isinstance(groups, dict):
            analog, digital = sum(groups.values(), []), []
        else:
            analog, digital = [], groups
        entry = selected.setdefault(item['record'], {'analog': set(), 'digital': set()})
        entry['analog'].update(analog)
        entry['digital'].update(digital)
    folder.mkdir()
    for rid, selection in selected.items():
        record, (_, times, values, states, _, _) = decoded(work, rid, materials, sources)
        analog = [i for i, c in enumerate(record['channels']) if c['number'] in selection['analog']]
        digital = [i for i, c in enumerate(record['digital_channels']) if c['number'] in selection['digital']]
        with (folder / (rid + '.csv')).open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['file_row', 'native_sample', 'time_record_s'] +
                            [record['channels'][i]['name'] + ' [' + record['channels'][i]['unit'] + ']' for i in analog] +
                            [record['digital_channels'][i]['name'] + ' [0/1]' for i in digital])
            for row, time in enumerate(times):
                writer.writerow([row + 1, record['native_sample_numbers'][row], time] +
                                [values[i][row] if math.isfinite(values[i][row]) else '' for i in analog] +
                                [states[i][row] for i in digital])


def run(work, request_path, output):
    work, request_path = Path(work).resolve(), Path(request_path).resolve()
    with decoding_session():
        materials, sources = load(work, replay=False)
        request = read_json(request_path)
        if not isinstance(request, dict) or set(request) - {'measurements', 'figures'}:
            raise CaseError('Analysis request supports measurements and figures')
        measures = measurements(work, request.get('measurements', []), materials, sources)
        output = fresh(output, [work, request_path])
        write_json(output / 'measurements.json', measures)
        (output / 'figures').mkdir()
        index = []
        for item in request.get('figures', []):
            _id(item['id'])
            if item['id'] in {f['id'] for f in index}:
                raise CaseError('Figure id repeated')
            if not isinstance(item.get('caption'), str) or not item['caption'].strip():
                raise CaseError('Figure caption required')
            kind = item['kind']
            path = output / 'figures' / (item['id'] + '.png')
            if kind == 'phasor':
                key = 'm:' + item['measurement']
                if key not in measures:
                    raise CaseError('Phasor figure requires a measurement in this request')
                groups = next(r['channels'] for r in request['measurements'] if r['id'] == item['measurement'])
                if not figures.phasor(measures[key], groups, path, item['caption']):
                    raise CaseError('No available phasor angles; inspect measurement reasons')
            else:
                record = _record(materials, item['record'])
                span = item['range']
                if (not isinstance(span, list) or len(span) != 2 or
                        any(type(v) not in (int, float) or not math.isfinite(v) for v in span) or
                        not record['time_range_s'][0] <= span[0] < span[1] <= record['time_range_s'][1]):
                    raise CaseError('Figure range must be within actual record coverage')
                if kind == 'waveform':
                    _channels(record, item['channels'])
                    figures.waveform(work, item['record'], item['channels'], span, path, item['caption'], materials, sources)
                elif kind == 'digital':
                    channels = item['channels']
                    if not isinstance(channels, list) or any(type(n) is not int for n in channels) or len(set(channels)) != len(channels):
                        raise CaseError('Select distinct integer digital channels')
                    figures.digital(work, item['record'], span, path, item['caption'], materials, sources, channels)
                else:
                    raise CaseError('Figure kind must be waveform, digital or phasor')
            index.append({**item, 'file': 'figures/' + path.name})
        _samples(work, materials, sources, request.get('measurements', []) + request.get('figures', []), output / 'samples')
        write_json(output / 'analysis.json', {'version': __version__, 'request': request,
                   'materials_sha256': sha(work / 'materials.json'), 'manifest_sha256': sha(work / 'manifest.json'), 'figures': index})
        write_json(output / 'checksums.json', checksums(output))
        return {'output': str(output), 'measurements': list(measures), 'figures': index,
                'note': 'Conditions and source units retained; electrical interpretations belong to the calling model'}


def verify(work, folder, materials, sources, replay=False):
    folder, work = Path(folder).resolve(), Path(work).resolve()
    check_files(folder)
    record = read_json(folder / 'analysis.json')
    if record['version'] != __version__ or record['materials_sha256'] != sha(work / 'materials.json') or record['manifest_sha256'] != sha(work / 'manifest.json'):
        raise CaseError('Analysis does not match these native materials or product version')
    stored = read_json(folder / 'measurements.json')
    if replay and measurements(work, record['request'].get('measurements', []), materials, sources) != stored:
        raise CaseError('Window calculation replay differs')
    return stored
