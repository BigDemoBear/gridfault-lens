"""First-review reading tasks and source-bound AI additions, without reparsing originals."""
from pathlib import Path
import copy

from .core import load, catalog, pointer, sha, validate_plan
from .model import CaseError, read_json, write_json
from .native import ROLES, select_groups, parameter_candidates

STATE = 'first-review.json'


def first_review(run, report, findings=None):
    from .workflow import load_run, _open_round
    from .markdown import REPORT, verify_report
    run, report = Path(run), Path(report).resolve()
    state = load_run(run)
    _open_round(state)
    if state['rounds']:
        raise CaseError('Semantic supplementation belongs to the first review')
    if (run / STATE).exists():
        raise CaseError('First review already opened; read first-review.json and resolve its tasks')
    if not verify_report(report)['ok']:
        raise CaseError('First review needs an intact initial candidate')
    if read_json(report / 'report-meta.json')['analysis_variables'] != state['variables']:
        raise CaseError('Candidate variables differ from RUN')
    materials, sources = load(report / 'inputs')
    plan = read_json(report / 'report-plan.json')
    tasks = []

    def add(record, variable, reason):
        if variable not in state['variables']:
            raise CaseError('Review only selected variables')
        end = next((e for e in plan['ends'] if e['record'] == record), None)
        if end is None or record is None:
            raise CaseError('Finding needs a selected readable record')
        existing = next((t for t in tasks if t['record'] == record and t['variable'] == variable), None)
        if existing is not None:
            if reason not in existing['reason'].split('\n'):
                existing['reason'] += '\n' + reason
            return
        rec = next(r for r in materials['records'] if r['id'] == record)
        cfg = next(s for s in materials['sources'] if s['id'] == rec['cfg_source'])
        stem = Path(cfg['path']).with_suffix('').as_posix().casefold()
        related = [s for s in sources if s.text is not None and s.kind in {'hdr', 'inf', 'xml'}
                   and Path(s.path).with_suffix('').as_posix().casefold() == stem]
        tasks.append({'id': 't' + str(len(tasks) + 1), 'record': record,
                      'variable': variable, 'object': end['name'], 'reason': reason,
                      'sources': [{'id': s.id, 'path': s.path,
                                   'text': str(report / 'inputs' / 'texts' / (s.id + '.txt'))}
                                  for s in related]})

    for end in plan['ends']:
        if end['record'] is None:
            continue
        groups = select_groups(end, materials, 'parameters')
        for label, role in ROLES:
            if role not in state['variables']:
                continue
            if 'parameter_values' in end:
                found = bool(end['parameter_values'].get(role))
            else:
                found = any(parameter_candidates(g['fields'], role) for g in groups)
            found = found or bool(end.get('semantic_values', {}).get(role))
            if not found:
                add(end['record'], role, label + '未由自动参数表取得；定向查看原文后决定补取或留缺')
    if findings is not None:
        for f in read_json(findings)['findings']:
            if not isinstance(f.get('reason'), str) or not f['reason'].strip():
                raise CaseError('Finding needs an actual review reason')
            add(f['record'], f['variable'], f['reason'])
    result = {'initial_report': str(report), 'initial_report_sha256': sha(report / REPORT),
              'tasks': tasks, 'results': [],
              'instruction': '先审查初稿；对这些缺项及疑点定向读取来源，再用semantic-fill补充。'
                             '无相关原文也记录not_provided或unresolved及原因；不另做全量抽取。'}
    write_json(run / STATE, result)
    return result


def fill(run, response, output):
    from .workflow import load_run, _open_round
    run, output = Path(run), Path(output)
    state = load_run(run)
    _open_round(state)
    if state['rounds']:
        raise CaseError('First-round supplementation is already closed')
    review = read_json(run / STATE)
    report = Path(review['initial_report'])
    materials, sources = load(report / 'inputs')
    cat = catalog(materials, {}, sources)
    plan = read_json(report / 'report-plan.json')
    data = read_json(response)
    results = data['results']
    tasks = {t['id']: t for t in review['tasks']}
    if len(results) != len(tasks) or {r['task'] for r in results} != set(tasks):
        raise CaseError('Resolve each first-review reading task once')
    for result in results:
        task = tasks[result['task']]
        if result['status'] not in {'filled', 'not_provided', 'unresolved'} or not result.get('note', '').strip():
            raise CaseError('Reading result needs status and case-specific note')
        end = next(e for e in plan['ends'] if e['record'] == task['record'])
        end.setdefault('semantic_status', {})[task['variable']] = {'status': result['status'], 'note': result['note']}

        def native(ref):
            if not isinstance(ref, str) or not ref.startswith(('s:', 'f:')) or '#' not in ref:
                raise CaseError('Semantic reading needs a native source reference')
            eid, _, path = ref.partition('#')
            try:
                value = pointer(cat[eid], path)
            except (KeyError, ValueError, IndexError, TypeError) as exc:
                raise CaseError('Native reading reference does not exist: ' + ref) from exc
            if value is None or isinstance(value, (dict, list)):
                raise CaseError('Native reading reference must select an original scalar')

        for ref in result.get('evidence', []):
            native(ref)
        values = result.get('values', [])
        events = result.get('events', [])
        text = result.get('text', {})
        if result['status'] == 'filled' and not (values or events or text):
            raise CaseError('Filled reading must contribute values, events or corrected text')
        if result['status'] != 'filled' and (values or events or text):
            raise CaseError('Only a filled result contributes report content')
        for value in values:
            if task['variable'] not in dict((r, l) for l, r in ROLES):
                raise CaseError('This variable has no native parameter-table role; use text/events')
            for name in ('label', 'object', 'stage', 'time_basis'):
                if not isinstance(value.get(name), str) or not value[name].strip():
                    raise CaseError('Semantic value needs ' + name)
            native(value['value'])
            if 'unit' in value:
                native(value['unit'])
            observation = {**value, 'task': task['id'], 'method': 'ai_semantic', 'note': result['note']}
            end.setdefault('semantic_values', {}).setdefault(task['variable'], []).append(observation)
        if events:
            for event in events:
                if not {'time', 'name'} <= set(event):
                    raise CaseError('Read event needs time and name references')
                for key, ref in event.items():
                    if key != 'time_basis':
                        native(ref)
            end.setdefault('semantic_events', []).extend(copy.deepcopy(events))
        if set(text) - set(end['text']):
            raise CaseError('Use the existing narrative slots for semantic corrections')
        end['text'].update(text)
    validate_plan(plan, materials, sources)
    if output.exists():
        raise CaseError('Use a new supplemented plan path')
    write_json(output, plan)
    review['results'] = results
    review['supplemented_plan'] = str(output.resolve())
    write_json(run / STATE, review)
    return {'plan': str(output.resolve()), 'tasks_resolved': len(results),
            'filled': sum(r['status'] == 'filled' for r in results),
            'next': '生成本轮修订稿，回看改动后record-round；这仍属于第一轮。'}


def completed(run):
    path = Path(run) / STATE
    if not path.is_file():
        return None
    review = read_json(path)
    if {r['task'] for r in review['results']} != {t['id'] for t in review['tasks']}:
        raise CaseError('Read and resolve the first-review missing/suspect items before completing round one')
    return review
