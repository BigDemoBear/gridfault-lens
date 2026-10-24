"""Model-authored report sources, source-bound expansion and one final package."""
from pathlib import Path
from html.parser import HTMLParser
import html
import json
import os
import re
import shutil
from urllib.parse import unquote, urlsplit

from . import __version__
from . import analysis
from .core import TOKENS, QUANTITY, catalog, fresh, load, pointer, sha
from .markdown import REPORT, _display, _evidence
from .model import CaseError, read_json, write_json
from .waveform import decoding_session

LINK = re.compile(r'\]\(([^)]+)\)')
STATUSES = {'confirmed', 'unavailable', 'conflict', 'not_applicable'}


def variables():
    return read_json(Path(__file__).parent / 'assets' / 'analysis-variables.json')['variables']


def _context(value, materials):
    if not isinstance(value, dict) or not {'line', 'ends'} <= set(value) or set(value) - {'line', 'ends', 'variables'}:
        raise CaseError('Context requires line and exactly two ends; optional variables')
    if not isinstance(value['line'], str) or not value['line'].strip() or len(value['ends']) != 2:
        raise CaseError('Keep a line name and exactly two station ends')
    selected = value.get('variables', [v['id'] for v in variables() if v['default']])
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= {v['id'] for v in variables()}:
        raise CaseError('Select distinct existing variable ids')
    known = {r['id'] for r in materials['records']}
    seen = set()
    for end in value['ends']:
        if not isinstance(end, dict) or set(end) != {'name', 'records'} or not isinstance(end['name'], str) or not end['name'].strip():
            raise CaseError('Each station end requires name and records; records=[] means unavailable')
        if not isinstance(end['records'], list) or len(set(end['records'])) != len(end['records']) or not set(end['records']) <= known:
            raise CaseError('Each end may select any number of existing native records')
        if seen.intersection(end['records']):
            raise CaseError('One native record cannot belong to both station ends')
        seen.update(end['records'])
    return {**value, 'variables': selected}


def _catalog(folder, replay=False):
    materials, sources = load(folder / 'inputs', replay=replay)
    measures = {}
    analysis_root = folder / 'analysis'
    for item in sorted(analysis_root.iterdir()) if analysis_root.exists() else []:
        values = analysis.verify(folder / 'inputs', item, materials, sources, replay=replay)
        if set(values).intersection(measures):
            raise CaseError('Measurement ids must be unique across included analyses')
        measures.update(values)
    result = catalog(materials, measures, sources)
    method_paths = {}
    attachments = folder / 'attachments'
    for item in sorted(attachments.iterdir()) if attachments.exists() else []:
        if item.is_dir() and (item / 'registration.json').is_file():
            from .attempts import values
            name = read_json(item / 'registration.json')['id']
            key = 'a:' + name
            if key in result:
                raise CaseError('Supplementary method id repeated: ' + name)
            result[key] = values(item)
            method_paths[key] = 'attachments/' + item.name + '/result.json'
    result['_method_paths'] = method_paths
    return materials, sources, result


def _expand(source, cat, materials):
    # Keep numerical quantities bound to references, without inferring semantics.
    plain = LINK.sub(']()', TOKENS.sub('', source))
    match = QUANTITY.search(plain)
    if match:
        raise CaseError('Numerical quantity needs an original/calculated reference: ' + match[0])
    proof, ids = [], {}

    def replace(match):
        reference = match[1]
        eid, sep, path = reference.partition('#')
        if not sep:
            raise CaseError('Reference needs # and a source pointer: ' + reference)
        try:
            value = pointer(cat[eid], path)
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise CaseError('Unknown reference: ' + reference) from exc
        if value is None or isinstance(value, (dict, list)):
            raise CaseError('Cite an available original/calculated scalar: ' + reference)
        display = _display(reference, value, materials)
        if reference not in ids:
            ids[reference] = _evidence(reference, display, cat, materials, proof)
            if eid.startswith('a:'):
                proof[-1]['file'] = cat['_method_paths'][eid]
        display = display.replace('|', r'\|').replace('\n', ' ')
        return '[' + display + '](review.html#' + ids[reference] + ')'

    return TOKENS.sub(replace, source), proof


def _review_page(folder, proof, materials):
    parts = ['<!doctype html><meta charset="utf-8"><title>原文与计算依据</title>',
             '<style>body{font-family:serif;max-width:1100px;margin:2em auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%}</style>',
             '<h1>原文与计算依据</h1><p><a href="' + REPORT + '">返回报告</a> · <a href="inputs/materials.json">原生材料目录</a></p>',
             '<p>程序检查来源和计算；模型负责解释。引用存在不证明解释正确。请核对对象、阶段、单位、时间与采样条件。</p>']
    by_id = {s['id']: s for s in materials['sources']}
    for item in proof:
        parts.append('<section id="' + item['anchor'] + '"><h2>' + item['anchor'] + '</h2>')
        for key in ('source', 'cfg_source', 'dat_source'):
            if item.get(key) in by_id:
                source = by_id[item[key]]
                parts.append('<p><a href="inputs/' + html.escape(source['raw_path'], quote=True) + '">' + html.escape(source['path']) + '</a></p>')
        if item.get('file'):
            parts.append('<p><a href="' + html.escape(item['file'], quote=True) + '">补充方法结果与过程</a></p>')
        if item.get('source') in by_id and by_id[item['source']]['kind'] != 'dat':
            parts.append('<p><a href="inputs/texts/' + item['source'] + '.txt">完整冻结原文</a></p>')
        parts.append('<pre>' + html.escape(json.dumps(item, ensure_ascii=False, indent=2)) + '</pre></section>')
    (folder / 'review.html').write_text('\n'.join(parts), encoding='utf-8')


def compose(work, source, context, output, analyses=(), attachments=()):
    work, source, context = Path(work).resolve(), Path(source).resolve(), Path(context).resolve()
    materials, sources = load(work, replay=False)
    setting = _context(read_json(context), materials)
    output = fresh(output, [work, source, context, *analyses, *attachments])
    shutil.copytree(work, output / 'inputs')
    write_json(output / 'context.json', setting)
    shutil.copy2(source, output / 'report-source.md')
    for collection, name in [(analyses, 'analysis'), (attachments, 'attachments')]:
        for raw in collection:
            path = Path(raw).resolve()
            target = output / name / path.name
            if not path.exists() or target.exists():
                raise CaseError('Attachment missing or name repeated: ' + str(path))
            target.parent.mkdir(exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)
    materials, _, cat = _catalog(output)
    body, proof = _expand(source.read_text(encoding='utf-8-sig'), cat, materials)
    (output / REPORT).write_text(body, encoding='utf-8')
    write_json(output / 'review-references.json', proof)
    _review_page(output, proof, materials)
    write_json(output / 'report-meta.json', {'version': __version__, 'analysis_variables': setting['variables'],
               'references': len(proof), 'layout': 'model-authored-two-ends'})
    check_links(output)
    write_json(output / 'checksums.json', analysis.checksums(output))
    return {'report': str(output / REPORT), 'report_sha256': sha(output / REPORT),
            'references': len(proof), 'evidence': str(output / 'review.html'),
            'note': 'Inspect the actual report and figures; edit report source and compose again when needed. No candidate limit.'}


class _HTMLLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets, self.ids = [], set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {'href', 'src'} and value:
                self.targets.append(value)
            if key == 'id':
                self.ids.add(value)


def check_links(root):
    root = Path(root).resolve()
    count = 0
    for document in sorted(root.rglob('*')):
        if not document.is_file() or document.suffix.lower() not in {'.md', '.html', '.htm'} or document.name == 'report-source.md':
            continue
        body = document.read_text(encoding='utf-8-sig')
        if document.suffix.lower() == '.md':
            targets = [m[1] for m in LINK.finditer(body)]
        else:
            parser = _HTMLLinks(); parser.feed(body); targets = parser.targets
        for target in targets:
            parts = urlsplit(html.unescape(target))
            if parts.scheme:
                continue
            path = (document.parent / unquote(parts.path)).resolve() if parts.path else document
            if not path.is_relative_to(root) or not path.is_file():
                raise CaseError('Local link missing or outside package: ' + str(document.relative_to(root)) + ' -> ' + target)
            if parts.fragment:
                if path.suffix.lower() in {'.html', '.htm'}:
                    parser = _HTMLLinks(); parser.feed(path.read_text(encoding='utf-8-sig'))
                    if unquote(parts.fragment) not in parser.ids:
                        raise CaseError('HTML anchor missing: ' + target)
                else:
                    raise CaseError('Use checked HTML anchors for source citations: ' + target)
            count += 1
    return count


def _relocate(target, document, candidate, final):
    parts = urlsplit(html.unescape(target))
    if parts.scheme or not parts.path:
        return target
    old = (document.parent / unquote(parts.path)).resolve()
    if not old.is_relative_to(candidate):
        raise CaseError('Link leaves candidate: ' + target)
    relative = old.relative_to(candidate)
    new = final / REPORT if relative.as_posix() == REPORT else final / 'data' / 'evidence' / relative
    new_doc = final / REPORT if document.name == REPORT and document.parent == candidate else final / 'data' / 'evidence' / document.relative_to(candidate)
    value = Path(os.path.relpath(new, new_doc.parent)).as_posix()
    return value + ('#' + parts.fragment if parts.fragment else '')


def _rewrite(body, document, candidate, final):
    if document.suffix.lower() == '.md':
        return LINK.sub(lambda m: '](' + _relocate(m[1], document, candidate, final) + ')', body)
    return re.sub(r'((?:href|src)=")([^"]+)(")',
                  lambda m: m[1] + html.escape(_relocate(m[2], document, candidate, final), quote=True) + m[3], body)


def verify_report(folder, replay=True):
    folder = Path(folder).resolve()
    try:
        final = (folder / 'data' / 'delivery-checksums.json').is_file()
        evidence = folder / 'data' / 'evidence' if final else folder
        if final:
            actual = {p.relative_to(folder).as_posix(): sha(p) for p in sorted(folder.rglob('*'))
                      if p.is_file() and p != folder / 'data' / 'delivery-checksums.json'}
            if actual != read_json(folder / 'data' / 'delivery-checksums.json'):
                raise CaseError('Delivered files differ from final manifest')
        else:
            analysis.check_files(folder)
        with decoding_session():
            materials, _, cat = _catalog(evidence, replay=replay)
            _context(read_json(evidence / 'context.json'), materials)
            source = (evidence / 'report-source.md').read_text(encoding='utf-8-sig')
            expected, proof = _expand(source, cat, materials)
            report = folder / REPORT
            if final and report.is_file():
                expected = _rewrite(expected, evidence / REPORT, evidence, folder)
            elif final and (folder / '无法出报告说明.md').is_file():
                report = evidence / REPORT
            if report.read_text(encoding='utf-8') != expected or read_json(evidence / 'review-references.json') != proof:
                raise CaseError('Report display or source references differ from source expansion')
        return {'ok': True, 'references': len(proof), 'links_checked': check_links(folder),
                'numeric_replay': replay, 'electrical_reasoning': 'Calling model and human review; not certified by this check'}
    except (CaseError, OSError, KeyError, ValueError, TypeError) as exc:
        return {'ok': False, 'error': str(exc)}


def _review(folder, review):
    if not isinstance(review, dict) or not {'report_sha256', 'decision', 'items'} <= set(review):
        raise CaseError('Final review requires report_sha256, decision and items; optional notes/issues')
    if review['report_sha256'] != sha(folder / REPORT):
        raise CaseError('Review must bind the current rendered report')
    decision = review['decision']
    if not isinstance(decision, dict) or set(decision) != {'can_report', 'reason'} or type(decision['can_report']) is not bool or not isinstance(decision['reason'], str) or not decision['reason'].strip():
        raise CaseError('Decision requires boolean can_report and a specific reason')
    selected = set(read_json(folder / 'context.json')['variables'])
    seen = set()
    for item in review['items']:
        if not isinstance(item, dict) or not {'variable', 'status', 'note', 'evidence'} <= set(item) or item['variable'] not in selected or item['status'] not in STATUSES:
            raise CaseError('Review each selected variable with status, note and evidence list')
        if not isinstance(item['note'], str) or not item['note'].strip() or not isinstance(item['evidence'], list):
            raise CaseError('Explain the observed result, limitation or missing information')
        if item['status'] in {'confirmed', 'conflict'} and not item['evidence']:
            raise CaseError('Confirmed or conflicting claims need source/calculation evidence')
        for target in item['evidence']:
            if not isinstance(target, str) or target.partition('#')[0] in {REPORT, 'report-source.md', 'report-meta.json', 'checksums.json'}:
                raise CaseError('Cite native or calculated evidence, not the report itself')
            path = (folder / unquote(target.partition('#')[0])).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                raise CaseError('Review evidence missing: ' + target)
            if '#' in target:
                parser = _HTMLLinks(); parser.feed(path.read_text(encoding='utf-8-sig'))
                if target.partition('#')[2] not in parser.ids:
                    raise CaseError('Review evidence anchor missing: ' + target)
        seen.add(item['variable'])
    if decision['can_report'] and seen != selected:
        raise CaseError('Final review must address every selected variable; missing values may remain unavailable')
    return review


def deliver(folder, review_path, output):
    folder, review_path = Path(folder).resolve(), Path(review_path).resolve()
    review = _review(folder, read_json(review_path))
    checked = verify_report(folder, replay=True)
    if not checked['ok']:
        raise CaseError('Final verification failed: ' + checked['error'])
    output = fresh(output, [folder, review_path, folder / 'inputs'])
    evidence = output / 'data' / 'evidence'
    evidence.mkdir(parents=True)
    for path in folder.iterdir():
        if path.name in {REPORT, 'checksums.json'}:
            continue
        target = evidence / path.name
        shutil.copytree(path, target) if path.is_dir() else shutil.copy2(path, target)
    (output / REPORT).write_text(_rewrite((folder / REPORT).read_text(encoding='utf-8'), folder / REPORT, folder, output), encoding='utf-8')
    for document in list(evidence.rglob('*.md')) + list(evidence.rglob('*.html')):
        if document.name == 'report-source.md':
            continue
        original = folder / document.relative_to(evidence)
        document.write_text(_rewrite(document.read_text(encoding='utf-8-sig'), original, folder, output), encoding='utf-8')
    human = {**review, 'version': __version__, 'source_report_sha256': review['report_sha256'],
             'human_review_required': True, 'technical_check': checked,
             'meaning': 'AI checked these claims; source existence and replay do not certify electrical interpretation'}
    rows = ['# 人工审查', '', '请沿引用查看原文、样本、图和计算条件。confirmed仅表示AI已核对。', '', review['decision']['reason'], '']
    for item in human['items']:
        item['evidence'] = ['evidence/' + p for p in item['evidence']]
        links = '、'.join('[' + str(i + 1) + '](' + p + ')' for i, p in enumerate(item['evidence']))
        details = '；'.join(str(item[k]) for k in ('object', 'stage', 'value', 'unit', 'time_basis') if k in item)
        rows += ['- ' + item['variable'] + '：' + item['status'] + '；' + details + '。' + item['note'] + ' ' + links]
    rows += ['', '## 剩余问题', '', str(review.get('issues', '见逐项记录。'))]
    write_json(output / 'data' / '人工审查.json', human)
    (output / 'data' / '人工审查.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    if not review['decision']['can_report']:
        (output / REPORT).unlink()
        (output / '无法出报告说明.md').write_text('# 无法出报告说明\n\n' + review['decision']['reason'] + '\n', encoding='utf-8')
        # Preserve the candidate as evidence when a model refuses report delivery.
        (evidence / REPORT).write_text((folder / REPORT).read_text(encoding='utf-8'), encoding='utf-8')
        (evidence / 'review.html').write_text((folder / 'review.html').read_text(encoding='utf-8'), encoding='utf-8')
    links = check_links(output)
    write_json(output / 'data' / 'delivery-checksums.json', {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob('*')) if p.is_file()})
    return {'output': str(output), 'report': str(output / REPORT) if review['decision']['can_report'] else None,
            'can_report': review['decision']['can_report'], 'links_checked': links,
            'human_review': str(output / 'data' / '人工审查.md')}
