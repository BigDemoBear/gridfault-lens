"""Reader-facing identity from native declarations, with separate file location hints."""
from pathlib import PurePosixPath
from .parsers import scope_for


def identify(record, sources, claims):
    source = next(s for s in sources if s.id == record['cfg_source'])
    related = [c for c in claims if c.source_scope == scope_for(source) or c.scope == scope_for(source)]
    lines = list(dict.fromkeys(str(c.value) for c in related if c.field == 'line' and c.value))
    names = [str(record[k]).strip() for k in ('station', 'device') if record.get(k) not in (None, '', '0')]
    names += [line for line in lines if line not in names]
    path = source.path.replace('\\', '/')
    filename = PurePosixPath(path).name
    # Directories help locate records; they do not establish electrical identity or fault truth.
    label = '／'.join(dict.fromkeys(names)) or '原生站线身份未声明'
    return {'display_name': label + '〔' + filename + '〕', 'native_names': names,
            'cfg_file': filename, 'source_path': source.path,
            'location_hint': str(PurePosixPath(path).parent),
            'meaning': 'Native identity and original file location; directory labels are not verified topology or fault conclusions'}
