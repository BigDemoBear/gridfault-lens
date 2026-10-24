"""Bounded AI reviews and one final report with human-review data.

The caller supplies all electrical judgments. This module enforces the selected
variable IDs, review count, evidence links and final-artifact binding only.
"""
from pathlib import Path
import re
import shutil

from . import __version__
from .core import fresh, sha
from .model import CaseError, read_json, write_json
from .markdown import REPORT, render, verify_report

STATE = 'workflow.json'
STATUSES = {'confirmed', 'unavailable', 'conflict', 'not_applicable'}
EXCLUDED = {REPORT, 'checksums.json', 'verification.json', 'content-review.json'}


def variable_catalog():
    return read_json(Path(__file__).parent / 'assets' / 'analysis-variables.json')


def start(case, output, iterations=2, variables=None):
    case = Path(case).resolve()
    if not case.is_dir():
        raise CaseError('Native case directory does not exist')
    if type(iterations) is not int or iterations < 1:
        raise CaseError('iterations must be a positive integer')
    catalog = variable_catalog()['variables']
    selected = [v['id'] for v in catalog if v['default']] if variables is None else variables
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= {v['id'] for v in catalog}:
        raise CaseError('Select distinct known variable IDs; see list-variables')
    output = fresh(output, [case])
    state = {'version': __version__, 'case': str(case), 'iterations': iterations,
             'variables': selected, 'drafts': 0, 'rounds': [], 'delivery': None}
    write_json(output / STATE, state)
    return state


def load_run(run):
    state = read_json(Path(run) / STATE)
    if state['version'] != __version__:
        raise CaseError('Current-version workflow required')
    return state


def _open_round(state):
    if state['delivery'] is not None:
        raise CaseError('This workflow is already delivered')


def draft(run, materials, plan, output):
    state = load_run(run)
    _open_round(state)
    result = render(materials, plan, output, selected_variables=state['variables'])
    state['drafts'] += 1
    write_json(Path(run) / STATE, state)
    return result


def _evidence(report, target):
    if not isinstance(target, str) or not target:
        raise CaseError('Evidence must be a relative file link')
    filename, _, anchor = target.partition('#')
    if filename in EXCLUDED:
        raise CaseError('Cite source/window evidence, not the candidate report or its integrity records')
    path = (report / filename).resolve()
    if not path.is_relative_to(report) or not path.is_file():
        raise CaseError('Evidence must resolve inside the candidate report: ' + target)
    if anchor and f'id="{anchor}"' not in path.read_text(encoding='utf-8'):
        raise CaseError('Evidence anchor missing: ' + target)


def record_round(run, report, review):
    """Commit one completed review/repair round, bound to its resulting report."""
    run, report = Path(run).resolve(), Path(report).resolve()
    state = load_run(run)
    _open_round(state)
    first = None
    if not state['rounds'] and (run / 'first-review.json').is_file():
        from .semantic import completed
        first = completed(run)
    checked = verify_report(report, replay=False)
    if not checked['ok']:
        raise CaseError('Candidate integrity check failed: ' + str(checked))
    if read_json(report / 'report-meta.json').get('analysis_variables') != state['variables']:
        raise CaseError('Render this candidate with markdown --run using the same variable selection')
    review = read_json(review)
    required = {'report_sha256', 'changes'}
    if not required <= set(review) or set(review) - required - {'items', 'evidence', 'issues', 'decision'}:
        raise CaseError('Review requires report_sha256 and changes; optional items, evidence, issues and final decision')
    if review['report_sha256'] != sha(report / REPORT):
        raise CaseError('Review must bind the current report_sha256')
    if not isinstance(review['changes'], str) or not review['changes'].strip():
        raise CaseError('State actual changes, or that this round required no changes')
    if 'evidence' in review and not isinstance(review['evidence'], list):
        raise CaseError('Review evidence must be a list')
    for target in review.get('evidence', []):
        _evidence(report, target)
    if 'issues' in review and not isinstance(review['issues'], str):
        raise CaseError('State remaining issues as text')
    final = 'decision' in review
    if final:
        _decision(review.get('decision'))
    review.setdefault('items', [])
    if not isinstance(review['items'], list):
        raise CaseError('Review items must be a list')
    seen = set()
    for item in review['items']:
        fields = {'variable', 'object', 'stage', 'value', 'unit', 'time_basis', 'status', 'note', 'evidence'}
        if not isinstance(item, dict) or set(item) != fields:
            raise CaseError('Each review item requires: ' + ', '.join(sorted(fields)))
        if item['variable'] not in state['variables'] or item['status'] not in STATUSES:
            raise CaseError('Review only selected variables with a supported status')
        for field in ('object', 'stage', 'unit', 'time_basis', 'note'):
            if not isinstance(item[field], str) or not item[field].strip():
                raise CaseError('Review item needs explicit ' + field)
        if not isinstance(item['evidence'], list):
            raise CaseError('Evidence must be a list of candidate-relative links')
        if item['status'] in {'confirmed', 'conflict'} and not item['evidence']:
            raise CaseError('Confirmed/conflicting items require evidence')
        if item['status'] == 'confirmed' and item['value'] is None:
            raise CaseError('A confirmed item must contain its observed/reported value')
        for target in item['evidence']:
            _evidence(report, target)
        seen.add(item['variable'])
    if final and review['decision']['can_report'] and seen != set(state['variables']):
        raise CaseError('Summarize every selected variable once in the final review before report delivery')
    entry = {'round': len(state['rounds']) + 1, 'report': str(report), **review}
    if first is not None:
        if any(r['status'] == 'filled' for r in first['results']) and sha(report / REPORT) == first['initial_report_sha256']:
            raise CaseError('Semantic additions require a revised candidate before completing round one')
        required = {ref for result in first['results'] if result['status'] == 'filled'
                    for ref in ([v[key] for v in result.get('values', []) for key in ('value', 'unit') if key in v]
                                + [ref for event in result.get('events', []) for key, ref in event.items()
                                   if key != 'time_basis'])}
        if required:
            cited = {p['reference'] for p in read_json(report / 'review-references.json')}
            if not required <= cited:
                raise CaseError('The revised report must actually cite the semantic additions')
        entry['semantic_reading'] = first
    state['rounds'].append(entry)
    write_json(run / STATE, state)
    return {'rounds_completed': len(state['rounds']), 'iterations': state['iterations'],
            'stop': final,
            'meaning': 'AI review recorded; this does not certify electrical correctness'}


def _copy_evidence(source, target):
    # Candidate manifests bind its original layout. They cannot certify the
    # reorganized delivery; create a fresh delivery manifest below instead.
    target.mkdir(parents=True)
    for path in source.iterdir():
        if path.name in EXCLUDED:
            continue
        destination = target / path.name
        if path.is_dir():
            shutil.copytree(path, destination)
        else:
            shutil.copy2(path, destination)


def _report_link(match):
    target = match.group(1)
    if target.startswith('#') or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target):
        return match.group(0)
    return '](data/evidence/' + target + ')'


def _decision(value):
    if not isinstance(value, dict) or set(value) != {'can_report', 'reason'} or type(value['can_report']) is not bool:
        raise CaseError('AI decision requires can_report (boolean) and reason')
    if not isinstance(value['reason'], str) or not value['reason'].strip():
        raise CaseError('Explain the evidence scope or the specific obstacle')
    return value


def deliver(run, decision, output, materials=None):
    """Package the calling AI's reportability decision, never infer it here."""
    run = Path(run).resolve()
    state = load_run(run)
    if state['delivery'] is not None:
        raise CaseError('This workflow is already delivered')
    last = state['rounds'][-1] if state['rounds'] else None
    reviewed_decision = last.get('decision') if last is not None else None
    decision = _decision(read_json(decision) if decision is not None else reviewed_decision)
    if reviewed_decision is not None and decision != reviewed_decision:
        raise CaseError('Delivery must use the decision already bound to the final review')
    if decision['can_report']:
        if reviewed_decision is None:
            raise CaseError('Bind the reportability decision to the final review; do not add new analysis at delivery')
        report = Path(last['report'])
        checked = verify_report(report)
        if not checked['ok'] or sha(report / REPORT) != last['report_sha256']:
            raise CaseError('Final candidate differs from the last recorded review')
        for item in last['items']:
            for target in item['evidence']:
                _evidence(report, target)
        source = report
    else:
        # Reportability can fail before extraction. Preserve the input even then.
        source = (Path(materials).resolve() if materials is not None else
                  Path(last['report']) / 'inputs' if last is not None else Path(state['case']))
        if not source.is_dir():
            raise CaseError('Evidence directory does not exist')
        report = None
    for folder in state.get('methods', []):
        from .attempts import status
        if status(folder)['status'] == 'active':
            raise CaseError('Finish supplementary work or retain its timeout record before delivery')
    output = fresh(output, [run, source, state['case']])
    data = output / 'data'
    data.mkdir()
    if (run / 'first-review.json').is_file():
        first = read_json(run / 'first-review.json')
        for task in first['tasks']:
            for native_source in task['sources']:
                if report is not None:
                    native_source['text'] = 'evidence/inputs/texts/' + native_source['id'] + '.txt'
                else:
                    target = data / '语义原文' / (native_source['id'] + '.txt')
                    target.parent.mkdir(exist_ok=True)
                    shutil.copy2(native_source['text'], target)
                    native_source['text'] = target.relative_to(data).as_posix()
        first.pop('initial_report', None)
        first.pop('supplemented_plan', None)
        write_json(data / '首轮语义补读.json', first)
    for index, folder in enumerate(state.get('methods', []), 1):
        shutil.copytree(folder, data / '补充方法' / str(index))
    if report is not None:
        _copy_evidence(source, data / 'evidence')
        body = (report / REPORT).read_text(encoding='utf-8')
        body = re.sub(r'\]\(([^)]+)\)', _report_link, body)
        (output / REPORT).write_text(body, encoding='utf-8')
    else:
        if (source / REPORT).is_file():
            _copy_evidence(source, data / 'evidence')
        else:
            shutil.copytree(source, data / 'evidence')
        (output / '无法出报告说明.md').write_text(
            '# 无法出报告说明\n\n' + decision['reason'].strip() +
            '\n\n原始资料与人工审查数据见 [data/人工审查.json](data/人工审查.json)。\n', encoding='utf-8')
    items = []
    if report is not None:
        for original in last['items']:
            item = dict(original)
            item['evidence'] = ['evidence/' + p for p in item['evidence']]
            items.append(item)
    # On refusal, do not reuse reviews of an earlier candidate as current facts.
    review_data = {'version': __version__, 'decision': decision,
                   'iterations': state['iterations'], 'rounds_completed': len(state['rounds']),
                   'drafts_created': state['drafts'],
                   'rounds': [{**{key: r[key] for key in ('round', 'report_sha256', 'changes')},
                               'issues': r.get('issues', '')}
                              for r in state['rounds']],
                   'variables': [v for v in variable_catalog()['variables'] if v['id'] in state['variables']],
                   'items': items, 'source_report_sha256': last['report_sha256'] if report is not None else None,
                   'stopped_review': None if report is not None or last is None else {
                       'report_sha256': last['report_sha256'], 'changes': last['changes'],
                       'items': [{key: item[key] for key in ('variable', 'object', 'stage', 'status', 'note')}
                                 for item in last['items']]},
                   'human_review_required': True,
                   'meaning': 'All selected values and conclusions require human review; confirmed means AI-checked only'}
    write_json(data / '人工审查.json', review_data)
    rows = ['# 人工审查', '', '先按所选变量核查数值、对象、阶段、单位、时间口径及证据。AI核对不替代人工采用判断。', '',
            f"核对轮数：{len(state['rounds'])}/{state['iterations']}。", '', decision['reason'], '']
    if last is not None and last.get('issues'):
        rows += ['## 剩余问题', '', last['issues'], '']
    if not items:
        rows += ['本次没有可交付报告；逐项资料缺口与出报告障碍以上述原因为准，原始资料见 evidence/。', '']
        if review_data['stopped_review'] is not None:
            rows += ['## 停止前核对摘要', '', '以下是停止前的记录，供人工定位问题。', '']
            for item in review_data['stopped_review']['items']:
                rows += [f"- {item['variable']} / {item['object']} / {item['stage']}：{item['status']}；{item['note']}"]
    names = {v['id']: v['name'] for v in review_data['variables']}
    labels = {'confirmed': 'AI已核对', 'unavailable': '缺项待人工核查',
              'conflict': '冲突待人工审查', 'not_applicable': '本案不适用'}
    for item in items:
        links = '、'.join(f'[{i + 1}]({p})' for i, p in enumerate(item['evidence'])) or '无可用证据'
        rows += [f"- {names[item['variable']]} / {item['object']} / {item['stage']}：{labels[item['status']]}；"
                 f"值={item['value']}；单位={item['unit']}；时基={item['time_basis']}。{item['note']} 依据：{links}"]
    (data / '人工审查.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    if report is not None:
        for target in re.findall(r'\]\(([^)]+)\)', body):
            if not target.startswith('#') and not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target):
                _evidence(output, target)
    checks = {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob('*')) if p.is_file()}
    write_json(data / 'delivery-checksums.json', checks)
    state['delivery'] = {'path': str(output), 'can_report': decision['can_report']}
    write_json(run / STATE, state)
    return {'output': str(output), 'can_report': decision['can_report'],
            'report': str(output / REPORT) if report is not None else None,
            'human_review': str(data / '人工审查.md')}
