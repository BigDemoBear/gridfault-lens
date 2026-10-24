"""Template-scoped extraction, explicit phasor windows, and source-bound report text."""
from pathlib import Path
from dataclasses import asdict
import hashlib,math,re
import xml.etree.ElementTree as ET
from .model import Source,CaseError,read_json,write_json
from .sources import collect,restore
from .parsers import parse_source,scope_for
from .waveform import analyze,read_record
from .identity import identify
from .measurement import sine_fit,window_gaps
from . import __version__

TOKENS=re.compile(r'\{\{([^{}]+)\}\}')
QUANTITY=re.compile(r'(?<![A-Za-z0-9_-])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s*(?:ms|μs|µs|kV|kA|Hz|km|V|A|s|°|%|毫秒|秒|伏|安)(?![A-Za-z])')
TEXT_KEYS={'messages','digital','voltage','current','clearing','reclose','phasor'}
END_FIELDS={'name','record','channels','measurements','figures','digital_ranges','digital_channels','protection_line','text'}
# Format check for internal artifact names and tokens only. Ordinary report words
# and writing quality are left to the AI review; this says nothing about meaning.
PROSE_FORBIDDEN=('填写计划','report-plan','review.html','证据令牌')
# These template roles ask for a reported maximum. Native fault or window values
# are not a maximum; only evidence that itself declares one may fill the role.
EXTREMA_ROLES={'maximum_reported_zero_sequence_current':'zero_sequence_current',
               'maximum_reported_differential_current':'differential_current'}
MAXIMUM_WORD=re.compile(r'最大|max',re.I)

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def fresh(output,protected=()):
 output=Path(output).resolve()
 if any(output==Path(p).resolve() or Path(p).resolve() in output.parents for p in protected):raise CaseError('Output must be outside source/input directories')
 if output.exists():raise CaseError('Use a new output directory')
 output.mkdir(parents=True);return output

def build(sources,root,issues):
 fields=[];problems=list(issues);records=[]
 for s in sources:
  claims,errors=parse_source(s);fields.extend(claims);problems.extend(asdict(e) for e in errors)
 pairs={}
 for s in sources:
  if s.kind=='dat':pairs.setdefault(scope_for(s),[]).append(s)
 for i,cfg in enumerate((s for s in sources if s.kind=='cfg' and s.text is not None),1):
  rid=f'r{i:03d}';ds=pairs.get(scope_for(cfg),[])
  try:
   if len(ds)!=1 or ds[0].problem:raise CaseError('Missing or ambiguous matching DAT')
   rec,claims=analyze(cfg,ds[0],root,rid);fields.extend(claims)
   # These are not template parameters; their full source literals remain in CFG snapshots.
   rec['clock']={k:rec['clock'][k] for k in ['nominal_frequency_hz','absolute_date_state']}
  except (CaseError,ValueError,IndexError,KeyError) as exc:
   rec={'id':rid,'cfg_source':cfg.id,'error':str(exc)}
  records.append(rec)
 for i,c in enumerate(fields,1):c.id=f'c{i:06d}'
 for rec in records:
  rec['identity']=identify(rec,sources,fields);rec['display_name']=rec['identity']['display_name']
 return {'version':__version__,'records':records,'fields':[asdict(f) for f in fields], 'issues':problems,
         'sources':[{'id':s.id,'path':s.path,'raw_path':s.raw_path,'kind':s.kind,'problem':s.problem} for s in sources],
         'purpose':'Only the selected fault-line report; original snapshots are retained for review'}

def extract(case,output):
 case=Path(case);output=Path(output)
 if output.resolve()==case.resolve() or case.resolve() in output.resolve().parents:raise CaseError('Output must be outside case')
 sources,issues=collect(case,output)
 manifest={'version':__version__,'input_root':str(case.resolve()),'issues':[asdict(i) for i in issues],
           'sources':[{**asdict(s),'text':'' if s.text is not None else None,'sha256':sha(output/s.raw_path) if not s.problem else None} for s in sources]}
 write_json(output/'manifest.json',manifest)
 materials=build(sources,output,manifest['issues']);write_json(output/'materials.json',materials)
 write_json(output/'source-structure.json',source_structure(sources))
 manifest['source_structure_sha256']=sha(output/'source-structure.json')
 manifest['materials_sha256']=sha(output/'materials.json');write_json(output/'manifest.json',manifest)
 if not any('error' not in r for r in materials['records']):raise CaseError('No readable CFG/DAT pairs; inspect materials.json')
 return {'records':len(materials['records']),'readable':sum('error' not in r for r in materials['records']),
         'source_structure':str(output/'source-structure.json')}

def load(work,replay=True):
 work=Path(work);manifest=read_json(work/'manifest.json')
 if manifest['version']!=__version__:raise CaseError('Current-version materials required; re-extract original native inputs')
 clean=[]
 for item in manifest['sources']:
  if item['sha256'] is not None:
   p=(work/item['raw_path']).resolve()
   if not p.is_relative_to(work.resolve()) or not p.is_file() or sha(p)!=item['sha256']:raise CaseError('Source snapshot changed: '+item['path'])
  clean.append({k:v for k,v in item.items() if k!='sha256'})
 sources=restore(work,{'sources':clean})
 for s in sources:
  if s.text is not None:
   from .sources import canonical_text
   text,encoding,problem=canonical_text(work/s.raw_path,'.'+s.kind)
   if (text,encoding,problem)!=(s.text,s.encoding,s.problem):raise CaseError('Canonical source text changed: '+s.path)
 materials=read_json(work/'materials.json')
 if 'materials_sha256' not in manifest or sha(work/'materials.json')!=manifest['materials_sha256']:raise CaseError('Materials snapshot changed')
 if sha(work/'source-structure.json')!=manifest['source_structure_sha256']:raise CaseError('Source structure snapshot changed')
 if replay and read_json(work/'source-structure.json')!=source_structure(sources):raise CaseError('Source structure differs from original text')
 if replay and build(sources,work,manifest['issues'])!=materials:raise CaseError('Materials differ from native replay')
 return materials,sources

def decoded(work,rid,materials=None,sources=None):
 if materials is None:materials,sources=load(work)
 r=next((r for r in materials['records'] if r['id']==rid),None)
 if r is None or 'error' in r:raise CaseError('Unknown or unreadable record '+str(rid))
 byid={s.id:s for s in sources}
 return r,read_record(byid[r['cfg_source']],byid[r['dat_source']],Path(work))

def validate_plan(plan,materials,sources=()):
 if not isinstance(plan,dict) or not {'line','information','ends','summary'}<=set(plan) or set(plan)-{'line','information','ends','summary','methods'}:raise CaseError('Plan requires line/information/ends/summary, optional registered methods')
 if not isinstance(plan['line'],str) or not plan['line'].strip():raise CaseError('Line display name required')
 if not isinstance(plan['information'],list) or not plan['information']:raise CaseError('Information paragraphs required')
 if len(plan['ends'])!=2:raise CaseError('Template requires two explicit end slots; use record=null for an unavailable end')
 if set(plan['summary'])!={'waveforms','protection','conclusion'}:raise CaseError('Three summary paragraphs required')
 for end in plan['ends']:
  if not END_FIELDS<=set(end) or set(end)-END_FIELDS-{'native_scopes','parameter_values','protection_values','semantic_values','semantic_events','semantic_status'}:raise CaseError('Unexpected or missing end fields')
  for role,items in end.get('semantic_values',{}).items():
   from .native import ROLES
   if role not in {r for _,r in ROLES} or not isinstance(items,list):raise CaseError('Unknown semantic parameter role')
   for item in items:
    if not {'label','value','object','stage','time_basis','method','note','task'}<=set(item):raise CaseError('Semantic observation needs source and interpretation context')
    for key in ('value','unit'):
     if key in item and (not isinstance(item[key],str) or not re.match(r'^[fs]:[^#]+#',item[key])):raise CaseError('Semantic values must cite native sources')
  for item in end.get('semantic_events',[]):
   if not {'time','name'}<=set(item) or set(item)-{'time','name','phase','state','unit','time_basis'}:raise CaseError('Semantic event needs original event references')
   if 'time_basis' in item and (not isinstance(item['time_basis'],str) or not item['time_basis'].strip()):raise CaseError('Explain the original event time basis')
   if any(not isinstance(v,str) or not re.match(r'^[fs]:[^#]+#',v) for k,v in item.items() if k!='time_basis'):raise CaseError('Semantic events must cite native sources')
  if 'protection_values' in end:
   if 'protection' in end.get('native_scopes',{}):raise CaseError('Choose protection_values or native_scopes.protection, not both')
   if not isinstance(end['protection_values'],list):raise CaseError('protection_values must be a native event reference list')
   for item in end['protection_values']:
    if not isinstance(item,dict) or not {'time','name'}<=set(item) or set(item)-{'time','name','phase','state','unit','time_basis'}:raise CaseError('Event entry requires time/name and optional phase/state/unit references or time_basis')
    if 'time_basis' in item and (not isinstance(item['time_basis'],str) or not item['time_basis'].strip()):raise CaseError('Explain the original event time basis')
    if any(not isinstance(v,str) or not re.match(r'^[fs]:[^#]+#',v) for k,v in item.items() if k!='time_basis'):raise CaseError('Original event values require native references')
  if 'parameter_values' in end:
   from .native import ROLES
   values=end['parameter_values']
   if 'parameters' in end.get('native_scopes',{}):raise CaseError('Choose parameter_values or native_scopes.parameters, not both')
   if not isinstance(values,dict) or set(values)-{role for _,role in ROLES}:raise CaseError('parameter_values supports template parameter roles only')
   for role,items in values.items():
    if not isinstance(items,list):raise CaseError('Parameter values must be lists of native references')
    for item in items:
     if not isinstance(item,dict) or not {'label','value'}<=set(item) or set(item)-{'label','value','unit'}:raise CaseError('Parameter entry requires label/value and optional unit reference')
     if not isinstance(item['label'],str) or not item['label'].strip():raise CaseError('Parameter source label required')
     for key in ('value','unit'):
      if key in item and (not isinstance(item[key],str) or not re.match(r'^[fs]:[^#]+#',item[key])):raise CaseError('Original parameter values and units require native references')
     if role in EXTREMA_ROLES and not declares_maximum(item['value'],EXTREMA_ROLES[role],materials,sources):
      raise CaseError('This template role asks for a reported maximum; the reference is a fault or window value. Leave the entry absent, keep the original in review material, or cite evidence that itself declares a maximum: '+item['value'])
  if 'native_scopes' in end:
   selection=end['native_scopes']
   if not isinstance(selection,dict) or set(selection)-{'parameters','protection'} or any(not isinstance(v,list) or any(not isinstance(s,str) for s in v) for v in selection.values()):raise CaseError('native_scopes selects parameter/protection scope lists only')
  if not isinstance(end['name'],str) or not end['name'].strip():raise CaseError('End display name required')
  if set(end['text'])!=TEXT_KEYS:raise CaseError('All template narrative slots must be answered')
  if not isinstance(end['protection_line'],str):raise CaseError('Explicit protection message line filter required')
  if not isinstance(end['digital_channels'],list) or any(type(x)!=int for x in end['digital_channels']):raise CaseError('digital_channels must be an explicit integer list')
  if len(set(end['digital_channels']))!=len(end['digital_channels']):raise CaseError('Select each digital channel once')
  if end['record'] is None:
   if end.get('native_scopes') or end.get('parameter_values') or end.get('protection_values'):raise CaseError('Unavailable end has no native selections')
   if end['channels'] or end['measurements'] or end['figures'] or end['digital_ranges'] or end['digital_channels'] or end['protection_line'].strip():raise CaseError('Unavailable end must use empty selection fields')
   continue
  r=next((x for x in materials['records'] if x['id']==end['record'] and 'error' not in x),None)
  if r is None:raise CaseError('End record not readable')
  if not end['protection_line'].strip():raise CaseError('Explicit protection message line filter required')
  available={c['number'] for c in r['channels']}
  if set(end['channels'])!={'U','I'}:raise CaseError('Only U/I plot groups are supported')
  chans=[c for g in end['channels'].values() for c in g]
  if not chans or len(set(chans))!=len(chans) or not set(chans)<=available:raise CaseError('Select distinct existing report channels')
  for group,units in [('U',{'V','kV','mV'}),('I',{'A','kA','mA'})]:
   selected=[c for c in r['channels'] if c['number'] in end['channels'][group]]
   if not selected or any(c['unit'] not in units for c in selected) or len({c['unit'] for c in selected})!=1 or len({c['side'] for c in selected})!=1:raise CaseError('U/I groups require matching quantity, native unit and side; no silent conversion')
  digital={c['number'] for c in r['digital_channels']}
  if any(n not in digital for n in end['digital_channels']):raise CaseError('Selected digital channel does not exist in record')
  if not end['digital_channels'] and digital:raise CaseError('Select the target-line digital channels explicitly')
  if not end['measurements'] or 'fault' not in {m['id'] for m in end['measurements']}:raise CaseError('Explicit fault measurement window required')
  seen=set()
  for m in end['measurements']:
   if set(m)-{'id','samples','reference','record','angle_policy','angle_note'} or not {'id','samples','reference'}<=set(m) or m['id'] in seen or not re.fullmatch('[a-z][a-z0-9-]*',m['id']):raise CaseError('Invalid measurement request')
   if m.get('angle_policy','metadata_checked') not in {'metadata_checked','uncompensated'}:raise CaseError('Unknown angle policy')
   if m.get('angle_policy')=='uncompensated' and not m.get('angle_note','').strip():raise CaseError('Explain the use and limits of uncompensated angles')
   seen.add(m['id'])
   mr=next((x for x in materials['records'] if x['id']==m.get('record',end['record']) and 'error' not in x),None)
   if mr is None or not set(chans)<={c['number'] for c in mr['channels']}:raise CaseError('Measurement record/channel mismatch')
   if len(m['samples'])!=2 or any(type(x)!=int for x in m['samples']) or not 1<=m['samples'][0]<m['samples'][1]<=mr['samples']:raise CaseError('Sample window out of range')
   if m['reference'] not in chans:raise CaseError('Reference channel must be selected')
  if set(end['figures'])!={'fault','reclose'} or set(end['digital_ranges'])!={'fault','reclose'}:raise CaseError('Use template fault/reclose figures only')
  for v in [*end['figures'].values(),*end['digital_ranges'].values()]:
   if v is not None and (not isinstance(v,list) or len(v)!=2 or any(type(x) not in (float,int) or not math.isfinite(x) for x in v) or v[0]>=v[1] or v[0]<r['time_range_s'][0] or v[1]>r['time_range_s'][1]):raise CaseError('Plot range outside observed record extent')
  messages=[f for f in companion_fields(materials,r) if f['field']=='protection_event']
  if messages and 'protection_values' not in end and not any(end['protection_line'] in (f['value'].get('name') or '') for f in messages):raise CaseError('protection_line matches no HDR protection message')
  if 'native_scopes' in end:
   from .native import select_groups,protection_fields
   for kind in end['native_scopes']:
    groups=select_groups(end,materials,kind)
    if kind=='protection' and not any(protection_fields(end,g) for g in groups):raise CaseError('Selected blocks contain no target-line protection messages')

def calculate(work,plan,materials,sources):
 results={}
 for index,end in enumerate(plan['ends']):
  if end['record'] is None:continue
  r,(cfg,times,values,states,notes,basis)=decoded(work,end['record'],materials,sources)
  chans=[n for group in end['channels'].values() for n in group]
  for request in end['measurements']:
   r,(cfg,times,values,states,notes,basis)=decoded(work,request.get('record',end['record']),materials,sources)
   lo,hi=request['samples'];t=list(times[lo-1:hi]);rows=[];gaps=window_gaps(times,lo-1,hi-1,cfg.sample_rates)
   for n in chans:
    ci=next(i for i,c in enumerate(r['channels']) if c['number']==n);ch=r['channels'][ci];x=list(values[ci][lo-1:hi])
    row={'channel':n,'name':ch['name'],'unit':ch['unit'],'side':ch['side'],'fundamental_rms':None,'angle_deg':None,'relative_angle_deg':None,'uncorrected_angle_deg':None,'relative_uncorrected_angle_deg':None,'angle_basis':'timestamp_fit_uncompensated','reasons':[]}
    row.update(sample_rms=math.sqrt(math.fsum(v*v for v in x)/len(x)),sample_peak=max(map(abs,x)))
    try:
     if gaps:raise CaseError('WINDOW_CROSSES_GAP')
     fit=sine_fit(t,x,cfg.frequency);row['fundamental_rms']=fit['fundamental_rms']
     row.update(fit=fit,dc=fit['dc'],residual_rms=fit['residual_rms'],uncorrected_angle_deg=fit['angle_deg'])
     if ch['skew_us']!=0:row['reasons'].append('SKEW_UNKNOWN_OR_UNCOMPENSATED')
     if fit['fundamental_rms']<=1e-12:row['reasons'].append('ANGLE_UNDEFINED_FOR_NEAR_ZERO_QUANTITY')
     elif ch['skew_us']==0 or request.get('angle_policy')=='uncompensated':row['angle_deg']=fit['angle_deg']
     if fit['residual_rms']>max(1e-12,.1*fit['fundamental_rms']):row['reasons'].append('SINE_MODEL_RESIDUAL_EXCEEDS_10_PERCENT')
    except CaseError as exc:row['reasons'].append(str(exc))
    rows.append(row)
   ref=next(r for r in rows if r['channel']==request['reference'])
   for row in rows:
    if row['fundamental_rms'] is not None and row['fundamental_rms']<=1e-12:row['uncorrected_angle_deg']=None
   for row in rows:
    if row['angle_deg'] is not None and ref['angle_deg'] is not None:row['relative_angle_deg']=(row['angle_deg']-ref['angle_deg']+180)%360-180
    if row['uncorrected_angle_deg'] is not None and ref['uncorrected_angle_deg'] is not None:row['relative_uncorrected_angle_deg']=(row['uncorrected_angle_deg']-ref['uncorrected_angle_deg']+180)%360-180
    row['availability']={k:('unavailable' if row.get(k) is None else 'conditional' if 'angle' in k and ('SKEW_UNKNOWN_OR_UNCOMPENSATED' in row['reasons'] or 'SKEW_UNKNOWN_OR_UNCOMPENSATED' in ref['reasons']) else 'available') for k in ('sample_rms','sample_peak','fundamental_rms','angle_deg','relative_angle_deg','uncorrected_angle_deg','relative_uncorrected_angle_deg')}
   results[f'm:e{index}-{request["id"]}']={'record':r['id'],'record_name':r['display_name'],'samples':[lo,hi],'native_samples':[r['native_sample_numbers'][lo-1],r['native_sample_numbers'][hi-1]],'time_record_s':[t[0],t[-1]],'frequency_hz':cfg.frequency,'reference':request['reference'],'results':rows,'basis':basis,'angle_policy':request.get('angle_policy','metadata_checked'),'angle_note':request.get('angle_note','')}
 return results

def source_catalog(source):
 """Exact readable-source literals; no domain inference or value conversion."""
 result={'lines':source.text.splitlines()}
 if source.text.lstrip().startswith('<'):
  try:root=ET.fromstring(source.text)
  except ET.ParseError:return result
  def node_value(node):
   value={'@'+key:literal for key,literal in node.attrib.items()}
   if node.text and node.text.strip():value['#text']=node.text.strip()
   if node.tail and node.tail.strip():value['#tail']=node.tail.strip()
   counts={}
   for child in node:
    counts[child.tag]=counts.get(child.tag,0)+1
    value[child.tag+'['+str(counts[child.tag])+']']=node_value(child)
   return value
  result['xml']={root.tag:node_value(root)}
 return result

def source_structure(sources):
 """Complete text/element inventory, independent of domain field adapters."""
 result=[]
 for source in sources:
  item={'reference':'s:'+source.id,'path':source.path,'kind':source.kind,
        'raw_path':source.raw_path,'problem':source.problem,'content':None}
  if source.text is not None:
   item['text_path']='texts/'+source.id+'.txt'
   item['content']=source_catalog(source)
   if source.text.lstrip().startswith('<'):
    try:ET.fromstring(source.text)
    except ET.ParseError as exc:item['structure_error']=str(exc)
  result.append(item)
 return {'purpose':'Complete received source text and XML structure; no field whitelist, unit conversion or semantic interpretation. Cite content with reference plus # and a JSON pointer. Lines are zero-based.',
         'sources':result}

def catalog(materials,measures,sources=()):
 return {**{'f:'+f['id']:f['value'] for f in materials['fields']},**{'r:'+r['id']:r for r in materials['records']},
         **{'s:'+s.id:source_catalog(s) for s in sources if s.text is not None},**measures}

def method_catalog(plan, base=None):
 from .attempts import values
 result={}
 for method in plan.get('methods',[]):
  name=method['id']
  if not re.fullmatch('[a-z][a-z0-9-]*',name) or 'a:'+name in result:raise CaseError('Use distinct method IDs')
  path=Path(method['path']);path=Path(base)/path if base is not None and not path.is_absolute() else path
  result['a:'+name]=values(path)
 return result

def pointer(value,path):
 if path and not path.startswith('/'):raise CaseError('JSON pointer must start with slash')
 for key in path.lstrip('/').split('/') if path else []:
  key=key.replace('~1','/').replace('~0','~');value=value[int(key)] if isinstance(value,list) else value[key]
 return value

def pointer_context(value,path):
 """Literal attribute values on the referenced node's ancestors, plus a text line.

    Used to check what the native record itself says around a reference; it does
    not interpret the value or assign it to a template role.
 """
 parts=[p for p in path.lstrip('/').split('/') if p] if path else []
 found=[];node=value
 for key in parts:
  key=key.replace('~1','/').replace('~0','~')
  if isinstance(node,dict):
   found += [str(v) for k,v in node.items() if k.startswith('@') and isinstance(v,str)]
   if key in node:node=node[key]
   else:return found
  elif isinstance(node,list):
   try:node=node[int(key)]
   except (ValueError,IndexError):return found
  else:return found
 if isinstance(node,str):found.append(node)
 elif isinstance(node,dict):found += [str(v) for k,v in node.items() if k.startswith('@') and isinstance(v,str)]
 return found

def declares_maximum(reference,quantity,materials,sources):
 """True only when the native evidence itself states a maximum of that quantity."""
 eid,_,path=reference.partition('#')
 if eid.startswith('f:'):
  field=next((f for f in materials['fields'] if 'f:'+f['id']==eid),None)
  if field is None:return False
  value=field.get('value')
  return (field.get('field')=='native_measurement' and isinstance(value,dict)
          and value.get('statistic')=='maximum_reported' and value.get('quantity')==quantity)
 if not eid.startswith('s:'):return False
 source=next((s for s in sources if s.id==eid[2:]),None)
 if source is None or source.text is None:return False
 cached=source_catalog(source)
 try:pointer(cached,path)
 except (KeyError,ValueError,IndexError,TypeError):return False
 return bool(MAXIMUM_WORD.search(' '.join([path]+pointer_context(cached,path))))

def prose_forbidden(text):
 return [phrase for phrase in PROSE_FORBIDDEN if isinstance(text,str) and phrase in text]

def text_parts(text,cat):
 if not isinstance(text,str) or not text.strip():raise CaseError('Empty template narrative')
 if re.search(r'〈|〉|\[\[TODO|【待补',text):raise CaseError('Template placeholder remains')
 bad=prose_forbidden(text)
 if bad:raise CaseError('Report prose contains an internal artifact name or token ('+'、'.join(bad)+'); use the original file name and case wording')
 if QUANTITY.search(TOKENS.sub('',text)):raise CaseError('Numerical report quantities require evidence tokens')
 parts=[]
 for i,part in enumerate(TOKENS.split(text)):
  if not i%2:parts.append((part,None));continue
  eid,sep,path=part.partition('#')
  try:v=pointer(cat[eid],path)
  except (KeyError,ValueError,IndexError,TypeError) as exc:raise CaseError('Invalid report reference '+part) from exc
  if v is None or isinstance(v,(dict,list)):raise CaseError('Reference is not an available scalar: '+part)
  if isinstance(v,float) and not math.isfinite(v):raise CaseError('Non-finite reference')
  parts.append((format(v,'.6g') if isinstance(v,float) else str(v),part))
 return parts

def companion_fields(materials,record):
 cfg=next(s for s in materials['sources'] if s['id']==record['cfg_source'])
 stem=Path(cfg['path']).with_suffix('').as_posix().casefold()
 ids={s['id'] for s in materials['sources'] if Path(s['path']).with_suffix('').as_posix().casefold()==stem}
 return [f for f in materials['fields'] if f['source'] in ids]
