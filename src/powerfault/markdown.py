"""Deterministic Markdown report assembly for the review stage.

The case plan supplies the line, windows, selected channels and narrative.
This module owns all repeated table, figure, link and review-file work so a
case run does not need a private assembly script.
"""
from pathlib import Path
import html
import json
import math
import re
import shutil
from datetime import datetime

from .core import (CaseError, calculate, catalog, companion_fields, fresh, load,
                   pointer, pointer_context, sha, text_parts, validate_plan, method_catalog)
from .model import read_json, write_json
from . import figures
from .native import ROLES, select_groups, parameter_candidates, protection_fields, event_reference_groups

REPORT = '故障线路报告.md'
SECTIONS = ['## 1 故障信息', '## 2 录波图解析', '## 3 综合总结']
TEXT_LABELS = [('voltage', '电压'), ('current', '电流'), ('clearing', '切除'),
               ('reclose', '重合'), ('phasor', '相量')]
# A post-draft review record is short but must cover these five items, each with
# a factual sentence and a link to the evidence it was checked against.
REVIEW_ITEMS = ('数值与对象', '时间与过程', '表述与比较', '判断依据', '完整与可读')
REVIEW_REVISION = '修订与复查'
LINK = re.compile(r'\]\(([^)]+)\)')
# Display only: the raw native code stays in the evidence entry, so a Chinese
# body sentence does not lose traceability. Unknown codes are shown unchanged.
NATIVE_CODE_DISPLAY = {
    'device_result': {'UNRECLOSED': '未重合', 'RECLOSED': '已重合'},
    'reclose_status': {'success': '重合成功', 'failure': '重合不成功',
                       'not_operated': '重合闸未动作', 'blocked': '重合闸闭锁'}}
# A record that still names an unresolved or product-level defect cannot be passed.
UNRESOLVED_PREFIXES = ('未解决', '产品问题', '待产品修复', '未修复')


def _escape(value):
    return str(value).replace('|', r'\|').replace('\n', ' ')


def _table(headers, rows):
    lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |']
    lines += ['| ' + ' | '.join(_escape(cell) for cell in row) + ' |' for row in rows]
    return '\n'.join(lines)


def _field(materials, eid):
    return next(f for f in materials['fields'] if 'f:' + f['id'] == eid)


def _display(reference, raw, materials):
    eid, _, path = reference.partition('#')
    if isinstance(raw, bool):
        return '是' if raw else '否'
    if isinstance(raw, str):
        if eid.startswith('f:'):
            mapped = NATIVE_CODE_DISPLAY.get(_field(materials, eid)['field'], {}).get(raw)
            if mapped:
                return mapped
        return raw
    if not isinstance(raw, float):
        return str(raw)
    if eid.startswith('m:'):
        if path.endswith('/relative_angle_deg'):
            return f'{raw:.1f}'
        if path.endswith('/fundamental_rms'):
            return f'{raw:.4g}'
    if eid.startswith('r:') and '/time_record_interval_s/' in path:
        return f'{raw:.4f}'
    if eid.startswith('r:') and path.endswith('/time_trigger_ms'):
        return f'{raw:.1f}'
    if eid.startswith('f:'):
        f = _field(materials, eid)
        if f['field'] == 'native_measurement' and path.endswith('/magnitude'):
            return str(f['value'].get('raw_value', format(raw, '.6g')))
    return format(raw, '.6g')


def _evidence(reference, display, cat, materials, proof):
    eid, _, path = reference.partition('#')
    raw = pointer(cat[eid], path)
    item = {'anchor': f'ref-{len(proof) + 1}', 'reference': reference,
            'display': display, 'value': raw}
    if eid.startswith('f:'):
        f = _field(materials, eid)
        item.update(kind='原生字段', field=f['field'], source=f['source'],
                    evidence=f['evidence'], unit=f['unit'], status=f['status'],
                    native_scope=f['source_scope'] or f['scope'])
    elif eid.startswith('r:'):
        item.update(kind='录波原生记录', record=cat[eid]['display_name'],
                    cfg_source=cat[eid]['cfg_source'], dat_source=cat[eid]['dat_source'])
    elif eid.startswith('s:'):
        item.update(kind='原文直接选取', source=eid[2:],
                    evidence={'kind': 'native_text', 'pointer': path, 'quote': raw},
                    context=pointer_context(cat[eid], path))
    elif eid.startswith('a:'):
        item.update(kind='已登记补充计算', method=eid[2:], result=cat[eid],
                    file='methods/' + eid[2:] + '/result.json')
    else:
        mm = cat[eid]
        rr = next(r for r in materials['records'] if r['id'] == mm['record'])
        item.update(kind='窗口基波计算', record=mm['record_name'],
                    measurements=mm, samples=mm['samples'], native_samples=mm['native_samples'],
                    time_record_s=mm['time_record_s'], reference_channel=mm['reference'],
                    frequency_hz=mm['frequency_hz'],
                    cfg_source=rr['cfg_source'], dat_source=rr['dat_source'])
    proof.append(item)
    return item['anchor']


def _render(text, cat, materials, proof):
    out = []
    for part, token in text_parts(text, cat):
        if token is None:
            out.append(part)
            continue
        eid, _, path = token.partition('#')
        raw = pointer(cat[eid], path)
        display = _display(token, raw, materials)
        anchor = _evidence(token, display, cat, materials, proof)
        out.append(f'[{display}](review.html#{anchor})')
    return ''.join(out)


def _link(reference, cat, materials, proof):
    eid, _, path = reference.partition('#')
    raw = pointer(cat[eid], path)
    if raw is None or isinstance(raw, (dict, list)) or isinstance(raw, float) and not math.isfinite(raw):
        raise CaseError('Native table reference must resolve to an available scalar: ' + reference)
    display = _display(reference, raw, materials)
    anchor = _evidence(reference, display, cat, materials, proof)
    return f'[{display}](review.html#{anchor})'


def _source_href(raw):
    return 'inputs/' + Path(raw).as_posix()


def _protection_rows(end, materials, cat, proof):
    if 'protection_values' in end:
        entries = end['protection_values']
    else:
        entries = []
        for group in select_groups(end, materials, 'protection'):
            for f in protection_fields(end, group):
                value, ref = f['value'], 'f:' + f['id'] + '#/'
                item = {'name': ref + 'name'}
                if value.get('time') is not None:
                    item['time'] = ref + 'time'
                elif value.get('raw_time'):
                    item['time'] = ref + 'raw_time'
                for key in ('unit', 'phase', 'state'):
                    if value.get(key) is not None:
                        item[key] = ref + key
                entries.append(item)
        entries += end.get('semantic_events', [])
    rows = []
    groups = event_reference_groups(entries, materials, cat)
    for label, events in groups:
        if len(groups) > 1:
            rows.append(['—', '—', label, '—', '—'])
        for item in events:
            values = {key: _link(ref, cat, materials, proof) for key, ref in item.items()
                      if key != 'time_basis'}
            rows.append([str(sum(row[0] != '—' for row in rows) + 1),
                         values.get('time', '未声明') + (' ' + values['unit'] if 'unit' in values else '（单位未声明）'),
                         values['name'], values.get('phase', '—'), values.get('state', '未声明')])
    return rows or [['—', '—', '未取得可核对的本线路原生保护报文', '—', '—']]


def _digital_rows(end, materials, cat, proof):
    r = cat['r:' + end['record']]
    selected = {c['number']: c for c in r['digital_channels']}
    order = {n: i for i, n in enumerate(end['digital_channels'])}
    events = []
    empty = []
    for number in end['digital_channels']:
        evs = [(i, e) for i, e in enumerate(r['events']) if e['channel'] == number]
        if not evs:
            empty.append(selected[number])
        events += [(e['time_record_s'], order[number], i, e) for i, e in evs]
    events.sort(key=lambda x: (x[0], x[1]))
    rows = []
    for _, _, i, e in events:
        ch = selected[e['channel']]
        a, b = e['time_record_interval_s']
        ref = f'r:{r["id"]}#/events/{i}'
        interval = f"({_link(ref + '/time_record_interval_s/0', cat, materials, proof)}, " \
                   f"{_link(ref + '/time_record_interval_s/1', cat, materials, proof)}] s"
        if not e['edge_observed']:
            interval += '；缺口两端状态差，非观测动作沿'
        trigger = _link(ref + '/time_trigger_ms', cat, materials, proof) + ' ms'
        rows.append([ch['name'], f"{e['from']}→{e['to']}", interval, trigger])
    for ch in empty:
        rows.append([ch['name'], f"记录内未变位（起始状态 {ch.get('initial_state', '未声明')}）", '—', '—'])
    return rows or [['—', '—', '未取得可核对的数字量变位', '—']]


def _parameters_rows(end, materials, cat, proof, roles=ROLES):
    if end.get('semantic_status') and 'parameter_values' not in end:
        base = dict(end)
        del base['semantic_status']
        rows = _parameters_rows(base, materials, cat, proof, roles)
        for label, role in roles:
            state = end['semantic_status'].get(role, {})
            replacement = {'not_provided': '原文未提供（首轮已补读）', 'unresolved': '原文含义尚未确定（首轮已补读）'}.get(state.get('status'))
            if replacement:
                for row in rows:
                    if row[1] == label and row[2] == '自动未取得，待原文核查':
                        row[2] = replacement
        return rows
    if end.get('semantic_values') and 'parameter_values' not in end:
        base = dict(end)
        del base['semantic_values']
        rows = _parameters_rows(base, materials, cat, proof, roles)
        for label, role in roles:
            additions = end['semantic_values'].get(role, [])
            if not additions:
                continue
            values = [v['label'] + '：' + _link(v['value'], cat, materials, proof) +
                      (' ' + _link(v['unit'], cat, materials, proof) if 'unit' in v else '') for v in additions]
            row = next((r for r in rows if r[1] == label and r[2] == '自动未取得，待原文核查'), None)
            if row is None:
                rows.append(['—', label + '（语义补充）', '；'.join(values), '见原文值'])
            else:
                row[2], row[3] = '；'.join(values), '见原文值'
        return rows
    if 'parameter_values' in end:
        rows = []
        for i, (label, role) in enumerate(roles, 1):
            values = []
            for item in end['parameter_values'].get(role, []):
                value = _link(item['value'], cat, materials, proof)
                if 'unit' in item:
                    unit = _link(item['unit'], cat, materials, proof)
                    if unit not in value:
                        value += ' ' + unit
                values.append(item['label'] + '：' + value)
            rows.append([str(i), label, '；'.join(values) or '自动未取得，待原文核查', '见原文值' if values else '—'])
        return rows
    groups = [g for g in select_groups(end, materials, 'parameters')
              if any(f['field'] != 'protection_event' for f in g['fields'])]
    if not groups:
        return [[str(i), label, '自动未取得，待原文核查', '—'] for i, (label, _) in enumerate(roles, 1)]
    rows = []
    for group in groups:
        if len(groups) > 1:
            rows.append(['—', group['label'], '—', '—'])
        for i, (label, role) in enumerate(roles, 1):
            candidates = parameter_candidates(group['fields'], role)
            values = []
            units = {f['unit'] or ('—' if f['field'] == 'fault_phase' else '未声明') for f in candidates}
            for f in candidates:
                path = '#/magnitude' if f['field'] == 'native_measurement' else '#'
                value = _link('f:' + f['id'] + path, cat, materials, proof)
                if len(candidates) > 1:
                    prefix = f['value'].get('phase') if f['field'] == 'native_measurement' else None
                    window = f['value'].get('window') if f['field'] == 'native_measurement' else None
                    description = prefix + '相' if prefix else ''
                    if window and window != 'NOT_STATED':
                        description += '（' + window + '）'
                    value = (description + '：' if description else '') + value
                    if len(units) > 1:
                        value += ' ' + (f['unit'] or '单位未声明')
                values.append(value)
            rows.append([str(i), label, '；'.join(values) or '自动未取得，待原文核查',
                         next(iter(units)) if len(units) == 1 else '见值' if units else '—'])
    return rows


def _write_review(output, materials, proof, figures_index, omitted):
    ss = {s['id']: s for s in materials['sources']}
    pages = ['<!doctype html><html lang="zh"><meta charset="utf-8">'
             '<title>参数来源与计算复核</title>'
             '<style>body{font:16px/1.7 sans-serif;max-width:1100px;margin:30px auto;padding:0 20px}'
             'section{border-top:1px solid #aaa;margin:24px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}'
             'img{max-width:100%}a{color:#174da0}</style>'
             '<h1>参数来源与计算复核</h1>'
             '<p><a href="故障线路报告.md">报告</a> · <a href="计算复核.md">计算说明</a> · '
             '<a href="measurements.json">全部窗口结果</a> · '
             '<a href="inputs/materials.json">解析材料</a></p>']
    for item in proof:
        pages.append(f'<section id="{item["anchor"]}"><h2>{html.escape(item["display"])}'
                     f' <small>{html.escape(item["reference"])}</small></h2>')
        for key in ('source', 'cfg_source', 'dat_source'):
            sid = item.get(key)
            if sid in ss:
                s = ss[sid]
                pages.append(f'<p><a href="{html.escape(_source_href(s["raw_path"]), quote=True)}">'
                             f'{html.escape(s["path"])}</a></p>')
        if item.get('file'):
            pages.append(f'<p><a href="{html.escape(item["file"], quote=True)}">已登记补充方法结果</a></p>')
        pages.append('<pre>' + html.escape(json.dumps(item, ensure_ascii=False, indent=2)) + '</pre></section>')
    if figures_index:
        pages.append('<h2>图像索引</h2>')
        for f in figures_index:
            pages.append(f'<p><a href="{html.escape(f["file"], quote=True)}">{html.escape(f["caption"])}</a></p>')
    if omitted:
        pages.append('<h2>未生成的图位</h2><ul>')
        pages += ['<li>' + html.escape(x) + '</li>' for x in omitted]
        pages.append('</ul>')
    (output / 'review.html').write_text('\n'.join(pages), encoding='utf-8')


def _full(value):
    if value is None:
        return 'null'
    if isinstance(value, float):
        return format(value, '.12g')
    return str(value)


def _time(value):
    return f'{value:.6f}'.rstrip('0').rstrip('.')


def _write_calculation_review(output, materials, plan, measures, figures_index, omitted):
    lines = ['# 计算复核与参数来源', '',
             '本文记录窗口、通道、来源和计算值，不替代对电气过程的判断。', '',
             '## 记录与测点']
    for end in plan['ends']:
        if end['record'] is None:
            lines += [f"### {end['name']}", '未提供可用原生录波，未生成该端计算。', '']
            continue
        r = next(x for x in materials['records'] if x['id'] == end['record'])
        lines += [f"### {end['name']}：{r['identity']['cfg_file']}",
                  f"站名、线路名取 CFG/HDR 文本；目录只用于定位。来源：`{r['identity']['source_path']}`。",
                  f"记录起点 {r['recording_start_time']}；触发 {r['trigger_time']}，偏移 {r['trigger_offset_s']} s；"
                  f"时轴 {r['time_basis']}，覆盖 {r['time_range_s']} s，样本 {r['samples']}，缺口 {len(r['time_gaps'])}。",
                  f"采样率段 `{r['sample_rates']}`。", '']
        chosen = [c for c in r['channels'] if c['number'] in sum(end['channels'].values(), [])]
        lines += [_table(['通道号', '名称', '单位/侧别', '一次/二次额定比', 'skew μs'],
                         [[c['number'], c['name'], c['unit'] + '/' + str(c['side']),
                           str(c['primary']) + '/' + str(c['secondary']), c['skew_us']] for c in chosen]), '']
        digital = [c for c in r['digital_channels'] if c['number'] in end['digital_channels']]
        lines += ['所选开关量：' + '；'.join(f"{c['number']} {c['name']}" for c in digital) + '。', '']
    lines += ['## 计算方法', '',
              '基波量由产品 `core.calculate` 调用固定频率 cos/sin/DC 最小二乘计算，输出基波有效值；'
              '幅值保留 CFG 声明的二次侧单位，不隐式换算一次侧。相角相对同端同窗所选参考通道。'
              '窗口使用原时戳并检查跨缺口；采样率不足一个实际周期的窗口不计算。', '']
    rows = []
    for key, value in measures.items():
        if value.get('angle_note'):
            lines += [value['angle_note'], '']
        lines += ['相角策略：' + value.get('angle_policy', 'metadata_checked') + '；完整DC、残差、采样RMS及逐量可用性见measurements.json。', '']
        a, b = value['time_record_s']
        rows.append([value['record_name'], key, value['samples'], value['native_samples'],
                     f'[{_time(a)}, {_time(b)}]'])
    lines += [_table(['记录', '测量键', '一基文件行范围（含端点）', '原生样本号', '本记录起点后秒范围'], rows), '']
    lines += ['## 窗口结果', '']
    for key, value in measures.items():
        lines += [f"### {key}", _table(['通道', '名称', '单位', '侧别', '基波有效值', '相角°', '相对角°', '提示'],
                                       [[r['channel'], r['name'], r['unit'], r['side'], _full(r['fundamental_rms']),
                                         _full(r['angle_deg']), _full(r['relative_angle_deg']),
                                         '；'.join(r['reasons']) or '—'] for r in value['results']]), '']
    lines += ['## 各窗口幅值对照', '', '按同一站端、记录、通道、单位和侧别分组；列名对应计划中的窗口名。', '']
    for index, end in enumerate(plan['ends']):
        windows = [(key, value) for key, value in measures.items() if key.startswith(f'm:e{index}-')]
        grouped = {}
        for key, value in windows:
            for row in value['results']:
                group = (value['record_name'], row['channel'], row['name'], row['unit'], row['side'])
                grouped.setdefault(group, {})[key] = row['fundamental_rms']
        if grouped:
            lines += [f"### {end['name']}", _table(['记录', '通道', '单位', '侧别'] + [key for key, _ in windows],
                      [[record, name, unit, side] + [_full(values.get(key)) for key, _ in windows]
                       for (record, channel, name, unit, side), values in grouped.items()]), '']
    lines += ['## 图位与未生成项', '']
    lines += [f"- {f['caption']}：`{f['file']}`；记录 {f['record']}；范围 {f['range']}。" for f in figures_index] or ['- 无。']
    lines += [f'- 未生成：{x}' for x in omitted] or ['- 无。']
    if materials['issues']:
        lines += ['', '## 解析问题', ''] + [f"- {x.get('code')}：{x.get('message')}" for x in materials['issues']]
    lines += ['', '## 时间口径', '',
              '报文表“原文时间”逐项取装置 HDR/XML 的原记载及其单位；工具不把它换算成本记录起点后时间，也不据此改动 DAT 变位。',
              '开关量表的“本记录起点后观测区间”由 DAT 时间戳减本记录起点得到；末列“本端触发后采样沿”为该时间减本端 CFG 触发偏移，单位毫秒。',
              '两端记录起点与触发时刻不同，数值相近不能作为同一时基的依据；正文并列时间时须分别说明采用哪种口径。', '']
    basis_rows = []
    for end in plan['ends']:
        r = next((x for x in materials['records'] if x['id'] == end['record']), None) if end['record'] else None
        if r is None:
            basis_rows.append([end['name'], '未提供可用原生录波', '—', '—', '—', '—'])
        else:
            basis_rows.append([end['name'], r['identity']['cfg_file'], r['recording_start_time'],
                               r['trigger_time'], _time(r['trigger_offset_s']), r['time_basis']])
    lines += [_table(['站端', '原生记录', '记录起点', '触发时刻', '记录起点到触发的偏移 s', 'DAT 时轴来源'], basis_rows), '',
              '## 原生参数与窗口计算', '',
              '原生参数表项保留装置原记载单位和原文值；窗口计算值单独列示，不并入原生表。',
              '装置未声明原生参数的幅值定义、侧别和计算窗，与窗口值比较时需另核。']
    (output / '计算复核.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def render(work, plan_path, output, selected_variables=None):
    work = Path(work).resolve()
    plan_path = Path(plan_path).resolve()
    output = Path(output).resolve()
    materials, sources = load(work)
    plan = read_json(plan_path)
    validate_plan(plan, materials, sources)
    selected = set(selected_variables) if selected_variables is not None else None
    roles = ROLES if selected is None else [(label, role) for label, role in ROLES if role in selected]
    if all(end['record'] is None for end in plan['ends']):
        raise CaseError('At least one readable end is required')
    output = fresh(output, [work, plan_path])
    shutil.copytree(work, output / 'inputs')
    shutil.copy2(plan_path, output / 'report-plan.json')
    for method in plan.get('methods', []):
        path = Path(method['path'])
        if not path.is_absolute():
            path = plan_path.parent / path
        method_catalog({'methods': [{**method, 'path': str(path)}]})
        shutil.copytree(path, output / 'methods' / method['id'])
        method['path'] = 'methods/' + method['id']
    write_json(output / 'report-plan.json', plan)
    materials, sources = load(output / 'inputs')
    measures = calculate(output / 'inputs', plan, materials, sources)
    write_json(output / 'measurements.json', measures)
    cat = catalog(materials, measures, sources)
    cat.update(method_catalog(plan, output))
    proof = []
    figdir = output / 'figures'
    figdir.mkdir()
    figure_index = []
    omitted = []
    tables = 0
    figure_no = [0]

    def text(value):
        return _render(value, cat, materials, proof)

    def table(caption, headers, rows):
        nonlocal tables
        tables += 1
        return f'表{tables} {caption}\n\n' + _table(headers, rows)

    def figure(slot, end, kind, stage):
        span = end['digital_ranges' if kind == 'digital' else 'figures'].get(stage) if kind != 'phasor' else None
        if kind != 'phasor' and span is None:
            omitted.append(f"{plan['line']} {end['name']} {kind}-{stage}：计划未提供观测覆盖，未生成。")
            return None
        if kind == 'digital' and not end['digital_channels']:
            omitted.append(f"{plan['line']} {end['name']} {kind}-{stage}：该记录没有可标识的开关量通道，未生成。")
            return None
        suffix = {'digital': '开关量变位', 'waveform': '电压电流波形', 'phasor': '基波相量'}[kind]
        stage_label = '首轮故障' if stage == 'fault' else '重合过程'
        label = f"{plan['line']} {end['name']} {stage_label}{suffix}"
        if kind == 'phasor' and measures.get(f'm:e{slot}-fault', {}).get('angle_policy') == 'uncompensated':
            label += '（未补偿通道偏斜）'
        path = figdir / f"e{slot}-{kind}-{stage}.png"
        if kind == 'digital':
            used = figures.digital(output / 'inputs', end['record'], span, path, label, materials, sources, end['digital_channels'])
            meta = {'channels': used}
        elif kind == 'waveform':
            figures.waveform(output / 'inputs', end['record'], end['channels'], span, path, label, materials, sources)
            meta = {}
        else:
            if f'm:e{slot}-fault' not in measures or not figures.phasor(measures[f'm:e{slot}-fault'], end['channels'], path, label):
                omitted.append(f'{label}：相角条件不足，未生成。')
                return None
            meta = {'measurement': f'm:e{slot}-fault'}
        figure_no[0] += 1
        caption = f'图{figure_no[0]} {label}'
        figure_index.append({'file': f'figures/{path.name}', 'caption': caption, 'record': end['record'],
                             'kind': kind, 'stage': stage, 'range': span, **meta})
        return f'![{caption}](figures/{path.name})\n\n{caption}'

    active = [(slot, end) for slot, end in enumerate(plan['ends']) if end['record'] is not None]
    for slot, end in active:
        figures.selected_csv(output / 'inputs', end['record'], end['channels'],
                             output / f'selected-samples-e{slot}.csv', materials, sources)

    parts = ['# 故障线路报告', '', '## 1 故障信息', '']
    for paragraph in plan['information']:
        parts += [text(paragraph), '']
    parts += ['## 2 录波图解析', '']
    missing = [end['name'] for end in plan['ends'] if end['record'] is None]
    if missing:
        parts += [f"以下仅分析{'、'.join(end['name'] for _, end in active)}；另一端未提供可用原生录波。", '']

    messages_selected = selected is None or 'protection_operation' in selected
    if messages_selected:
        parts += ['### 2.1 保护动作报文', '']
    for number, (slot, end) in enumerate(active if messages_selected else [], 1):
        parts += [f'#### 2.1.{number} {end["name"]}', '',
                  table(f"{plan['line']} {end['name']}保护动作简报（装置原记载）",
                        ['序号', '原文时间', '报文名称', '相别', '状态'],
                        _protection_rows(end, materials, cat, proof)), '',
                  text(end['text']['messages']), '']

    digital_selected = selected is None or bool(selected.intersection(
        {'protection_operation', 'breaker_operation', 'fault_duration', 'reclose_operation', 'recovery_state'}))
    if digital_selected:
        parts += ['### 2.2 开关量变位', '']
    for number, (slot, end) in enumerate(active if digital_selected else [], 1):
        parts += [f'#### 2.2.{number} {end["name"]}', '']
        for stage in ('fault', 'reclose'):
            value = figure(slot, end, 'digital', stage)
            if value:
                parts += [value, '']
        parts += [table(f"{plan['line']} {end['name']}开关量变位时间顺序",
                        ['通道', '状态变化', '本记录起点后观测区间', '本端触发后采样沿'],
                        _digital_rows(end, materials, cat, proof)), '',
                  text(end['text']['digital']), '']

    parts += ['### 2.3 波形特征', '']
    slot_variables = {'voltage': {'voltage', 'zero_sequence_voltage'},
                      'current': {'current', 'zero_sequence_current'},
                      'clearing': {'breaker_operation', 'fault_duration'},
                      'reclose': {'reclose_operation', 'recovery_state'},
                      'phasor': {'phasor_relation'}}
    for number, (slot, end) in enumerate(active, 1):
        parts += [f'#### 2.3.{number} {end["name"]}', '']
        paragraph_no = 0
        for key, label in TEXT_LABELS:
            if selected is not None and not selected.intersection(slot_variables[key]):
                continue
            paragraph_no += 1
            parts += [f'（{paragraph_no}）{label}：{text(end["text"][key])}', '']
        for stage in ('fault', 'reclose'):
            value = figure(slot, end, 'waveform', stage)
            if value:
                parts += [value, '']
        value = figure(slot, end, 'phasor', 'fault') if selected is None or 'phasor_relation' in selected else None
        if value:
            parts += [value, '']

    parts += ['## 3 综合总结', '']
    if plan['summary']['waveforms'].strip():
        parts += [text(plan['summary']['waveforms']), '']
    for slot, end in active:
        if roles:
            parts += [table(f"{plan['line']} {end['name']}保护故障信息及测距",
                        ['序号', '参数', '装置原记载值', '单位'],
                        _parameters_rows(end, materials, cat, proof, roles)), '']
    for key in ('protection', 'conclusion'):
        if plan['summary'][key].strip():
            parts += [text(plan['summary'][key]), '']
    (output / REPORT).write_text('\n'.join(parts).rstrip() + '\n', encoding='utf-8')

    _write_review(output, materials, proof, figure_index, omitted)
    _write_calculation_review(output, materials, plan, measures, figure_index, omitted)
    write_json(output / 'review-references.json', proof)
    write_json(output / 'figure-index.json', figure_index)
    write_json(output / 'report-meta.json', {'line': plan['line'], 'tables': tables, 'figures': len(figure_index),
                                             'analysis_variables': selected_variables,
                                             'references': len(proof), 'active_ends': len(active),
                                             'selected_digital_channels': {end['name']: end['digital_channels'] for _, end in active}})
    verification = {'ok': True, 'report': REPORT, 'tables': tables, 'figures': len(figure_index),
                    'references': len(proof), 'active_ends': len(active),
                    'inputs_replay': True, 'docx_or_pdf_generated': False,
                    'electrical_reasoning': 'Not reviewed; AI must review the finished report against evidence',
                    'finished_at': datetime.now().astimezone().isoformat()}
    write_json(output / 'verification.json', verification)
    checks = {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob('*'))
              if p.is_file() and p.name != 'checksums.json'}
    write_json(output / 'checksums.json', checks)
    checked = verify_report(output)
    if not checked['ok']:
        raise CaseError('Generated Markdown report failed verification: ' + str(checked))
    return {'report': str(output / REPORT), 'report_sha256': sha(output / REPORT), 'figures': len(figure_index), 'tables': tables,
            'references': len(proof), 'verification': checked}


def verify_report(output, replay=True):
    try:
        output = Path(output).resolve()
        checks = read_json(output / 'checksums.json')
        actual = {p.relative_to(output).as_posix() for p in output.rglob('*')
                  if p.is_file() and p.name != 'checksums.json'}
        if actual != set(checks):
            raise CaseError('Report file set differs')
        for rel, digest in checks.items():
            p = (output / rel).resolve()
            if not p.is_relative_to(output) or not p.is_file() or sha(p) != digest:
                raise CaseError('Report file changed: ' + rel)
        materials, sources = load(output / 'inputs', replay=replay)
        plan = read_json(output / 'report-plan.json')
        validate_plan(plan, materials, sources)
        measures = read_json(output / 'measurements.json')
        if replay and calculate(output / 'inputs', plan, materials, sources) != measures:
            raise CaseError('Phasor replay differs')
        cat = catalog(materials, measures, sources)
        cat.update(method_catalog(plan, output))
        for item in read_json(output / 'review-references.json'):
            eid, _, path = item['reference'].partition('#')
            if pointer(cat[eid], path) != item['value']:
                raise CaseError('Source reference changed: ' + item['reference'])
        meta = read_json(output / 'report-meta.json')
        md = (output / REPORT).read_text(encoding='utf-8')
        if [x for x in md.splitlines() if x.startswith('## ')] != SECTIONS:
            raise CaseError('Report must keep exactly the three template sections')
        if '{{' in md or '〈' in md or '〉' in md or 'TODO' in md:
            raise CaseError('Placeholder remains in report')
        if len(re.findall(r'^表\d+ ', md, re.M)) != meta['tables']:
            raise CaseError('Table count differs from report metadata')
        if len(re.findall(r'^图\d+ ', md, re.M)) != meta['figures']:
            raise CaseError('Figure count differs from report metadata')
        review = (output / 'review.html').read_text(encoding='utf-8')
        references = read_json(output / 'review-references.json')
        reference_ids = {x['anchor'] for x in references}
        if len(references) != meta['references']:
            raise CaseError('Reference count differs from report metadata')
        for anchor in reference_ids:
            if f'id="{anchor}"' not in review:
                raise CaseError('Review anchor missing: ' + anchor)
        for target in re.findall(r'\]\(([^)]+)\)', md):
            filename, _, anchor = target.partition('#')
            path = output / filename
            if not path.is_file():
                raise CaseError('Local link target missing: ' + target)
            if anchor and f'id="{anchor}"' not in path.read_text(encoding='utf-8'):
                raise CaseError('Local link anchor missing: ' + target)
        for target in re.findall(r'href="([^"]+)"', review):
            if target.startswith(('#', 'http://', 'https://', 'mailto:')):
                continue
            filename = target.partition('#')[0]
            if filename and not (output / filename).is_file():
                raise CaseError('Review source link missing: ' + target)
        index = read_json(output / 'figure-index.json')
        if len(index) != meta['figures']:
            raise CaseError('Figure index differs from report metadata')
        for item in index:
            if not (output / item['file']).is_file():
                raise CaseError('Indexed figure missing: ' + item['file'])
            if item['kind'] == 'digital':
                end = next((x for x in plan['ends'] if x['record'] == item['record']), None)
                if end is None or item.get('channels') != end['digital_channels']:
                    raise CaseError('Digital figure channels differ from plan: ' + item['file'])
        if list(output.rglob('*.docx')) or list(output.rglob('*.pdf')):
            raise CaseError('Markdown stage must not generate Word or PDF')
        content_review = 'not_recorded'
        if (output / 'content-review.json').is_file():
            recorded = read_json(output / 'content-review.json')
            if recorded['report_sha256'] != sha(output / REPORT) or recorded['audit_sha256'] != sha(output / '核对记录.md'):
                raise CaseError('AI review does not match the finished report')
            content_review = recorded['result']
        return {'ok': True, 'files_checked': len(checks), 'references': len(reference_ids),
                'figures': meta['figures'], 'tables': meta['tables'],
                'report_sha256': sha(output / REPORT),
                'ai_content_review': content_review,
                'meaning': 'Source/window/numeric replay, links and file set; not electrical reasoning'}
    except (OSError, CaseError, ValueError, KeyError, TypeError, IndexError) as exc:
        return {'ok': False, 'error': str(exc)}


def _target_key(target):
    value = target.replace('\\', '/')
    return value[2:] if value.startswith('./') else value


def _audit_section(text, title):
    """Body of the first section whose heading contains title, sub-headings included."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = re.match(r'^(#{1,6})\s+(.*)$', line)
        if heading and title in heading.group(2):
            level = len(heading.group(1))
            body = []
            for following in lines[index + 1:]:
                deeper = re.match(r'^(#{1,6})\s', following)
                if deeper and len(deeper.group(1)) <= level:
                    break
                body.append(following)
            return '\n'.join(body).strip()
    return None


def _check_audit(audit, digest, result, counts):
    """Require the short fixed review record; it cannot certify the reasoning.

    Only structure and traceability are checked here: the five items, one
    evidence link each, the required evidence classes and the revision chain.
    How much substance a sentence carries is left to human and AI review.
    """
    if re.search(r'TODO|〈|〉|【待补', audit):
        raise CaseError('Review record still contains a placeholder')
    if result == 'passed':
        for line in audit.splitlines():
            text = line.strip().lstrip('-#*+ ').strip()
            for prefix in UNRESOLVED_PREFIXES:
                if text.startswith(prefix):
                    remainder = text[len(prefix):].lstrip('：: ').strip()
                    if remainder and remainder not in {'无', '没有', 'none', 'None'}:
                        raise CaseError('Review record still lists an unresolved item; fix it or record needs_revision: ' + text[:80])
    targets = [x for x in LINK.findall(audit) if x.strip()]
    missing = []
    for item in REVIEW_ITEMS:
        body = _audit_section(audit, item)
        if body is None:
            missing.append(item)
            continue
        if not [x for x in LINK.findall(body) if x.strip()]:
            raise CaseError('Review item needs a concrete evidence link: ' + item)
    if missing:
        raise CaseError('Review record is missing required items, see writing.md#成文后核对: ' + '、'.join(missing))
    revision = _audit_section(audit, REVIEW_REVISION) or _audit_section(audit, '修订')
    if revision is None:
        raise CaseError('Review record needs a 修订与复查 section')
    cited = re.findall(r'\b[0-9a-f]{64}\b', revision)
    if digest in cited:
        raise CaseError('修订与复查 must not cite the current report SHA256 as the previous revision')
    previous = [x for x in cited if x != digest]
    if result == 'passed' and not previous and not re.search(r'无修订|未修改|初稿', revision):
        raise CaseError('修订与复查 must cite the previous report SHA256 or state that no change was made')
    # Evidence classes are required only when the report actually produced them.
    keys = [_target_key(x) for x in targets]
    if counts.get('references') and not any(x.startswith('review.html#ref-') for x in keys):
        raise CaseError('Review record must cite the numeric/object evidence it checked (review.html#ref-N)')
    if not any(x.partition('#')[0] == '计算复核.md' for x in keys):
        raise CaseError('Review record must cite 计算复核.md for the window comparison')
    if counts.get('figures') and not any(x.partition('#')[0].startswith('figures/') for x in keys):
        raise CaseError('Review record must cite at least one figure it actually inspected')
    evidence = {'review_anchors': sum(x.startswith('review.html#ref-') for x in keys),
                'calculation': sum(x.partition('#')[0] == '计算复核.md' for x in keys),
                'figures': sum(x.partition('#')[0].startswith('figures/') for x in keys)}
    return (previous[-1] if previous else None), evidence


def record_review(output, audit_path, result):
    """Attach an AI-authored post-draft review to this exact finished report.

    Recording a review does not perform it. The AI reads the report and evidence,
    writes the audit, then uses this entry point to bind the two artifacts. The
    record must cover the five review items with evidence links and state what
    the revision changed; the tool checks that form and the binding only.
    """
    output = Path(output).resolve()
    audit_path = Path(audit_path).resolve()
    checked = verify_report(output)
    if not checked['ok']:
        raise CaseError('Review requires an intact finished report: ' + str(checked))
    if result not in {'passed', 'needs_revision'}:
        raise CaseError('Review result must be passed or needs_revision')
    if (output / 'content-review.json').exists():
        raise CaseError('This report already has a review; keep it and render a new revision')
    audit = audit_path.read_text(encoding='utf-8-sig')
    digest = sha(output / REPORT)
    if digest not in audit:
        raise CaseError('Audit must identify the finished report SHA256')
    previous, evidence = _check_audit(audit, digest, result,
                                     {'references': checked.get('references', 0),
                                      'figures': checked.get('figures', 0)})
    for target in LINK.findall(audit):
        filename, _, anchor = target.partition('#')
        path = (output / filename).resolve()
        if not path.is_relative_to(output) or not path.is_file():
            raise CaseError('Audit link must resolve within this report: ' + target)
        if anchor and f'id="{anchor}"' not in path.read_text(encoding='utf-8'):
            raise CaseError('Audit evidence anchor missing: ' + target)
    (output / '核对记录.md').write_text(audit, encoding='utf-8')
    write_json(output / 'content-review.json', {
        'report_sha256': digest, 'result': result, 'audit_sha256': sha(output / '核对记录.md'),
        'author': 'AI', 'check_items': list(REVIEW_ITEMS), 'evidence': evidence,
        'previous_report_sha256': previous,
        'meaning': 'AI post-draft review recorded; tool checks record form and binding only, not electrical reasoning'})
    verification = read_json(output / 'verification.json')
    verification.update(ai_content_review=result, report_sha256=digest,
                        electrical_reasoning='AI post-draft review recorded; see 核对记录.md')
    write_json(output / 'verification.json', verification)
    with (output / 'review.html').open('a', encoding='utf-8') as page:
        page.write('\n<p><a href="核对记录.md">AI成文后核对记录</a>：' + result + '</p>\n')
    checks = {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob('*'))
              if p.is_file() and p.name != 'checksums.json'}
    write_json(output / 'checksums.json', checks)
    return verify_report(output)
