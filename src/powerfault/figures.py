"""Template figures from original samples; no smoothing or interpolation over gaps."""
from pathlib import Path
import math,csv
from PIL import Image,ImageDraw,ImageFont
from .core import decoded
from .model import CaseError

COLORS=['#244c9b','#b94036','#20805b','#834b9c']
def fonts(small_size=32):
 paths=[Path('C:/Windows/Fonts/simsun.ttc'),Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]
 p=next((p for p in paths if p.is_file()),None)
 if p is None:raise CaseError('A Chinese font is required for report figures (SimSun or Noto Sans CJK)')
 return ImageFont.truetype(str(p),small_size),ImageFont.truetype(str(p),38)

def shorten(draw,text,font,width):
 while draw.textlength(text,font=font)>width:text=text[:-2]+'…'
 return text

def shorten_tail(draw,text,font,width,keep=6):
 '''Truncate the middle, keeping the tail that distinguishes phase/quantity.'''
 if draw.textlength(text,font=font)<=width:return text
 tail=min(keep,len(text))
 while tail>3 and draw.textlength('…'+text[-tail:],font=font)>width:tail-=1
 suffix='…'+text[-tail:]
 head=''
 for i in range(len(text)-tail):
  if draw.textlength(text[:i+1]+suffix,font=font)>width:break
  head=text[:i+1]
 return head+suffix

def series_segments(times,values,lo,hi,gaps,frequency):
 current=[];result=[];dots=[]
 for j in range(lo,hi):
  if not math.isfinite(values[j]):
   if current:result.append(current);current=[]
   continue
  coarse=j>lo and (times[j]-times[j-1])*frequency>=.5
  if j+1 in gaps or coarse:
   if current:result.append(current);current=[]
  current.append((times[j],values[j]))
  if coarse:dots.append((times[j],values[j]))
 if current:result.append(current)
 return result,dots

def waveform(work,record,groups,span,path,label,materials,sources):
 r,(cfg,times,values,states,_,_)=decoded(work,record,materials,sources);small,title=fonts(30)
 panels=[]
 for key in ['U','I']:
  channels=[next(c for c in r['channels'] if c['number']==n) for n in groups[key]]
  phases=[c['number'] for c in channels if c['phase'] in {'A','B','C'}]
  if phases:panels.append((key,phases,'三相电压' if key=='U' else '三相电流'))
  for ch in channels:
   if ch['number'] not in phases:panels.append((key,[ch['number']],ch['name']))
 height=180+len(panels)*450
 im=Image.new('RGB',(1800,height),'white');d=ImageDraw.Draw(im);d.text((75,22),label,font=title,fill='black')
 gaps={g['sample'] for g in r['time_gaps']};chosen=[i for i,t in enumerate(times) if span[0]<=t<=span[1]]
 if not chosen:raise CaseError('No samples in figure range')
 lo,hi=chosen[0],chosen[-1]+1
 for panel,(key,nums,caption) in enumerate(panels):
  top=120+panel*450;bottom=top+300;left,right=160,1730
  vals=[v for n in nums for v in values[next(i for i,c in enumerate(r['channels']) if c['number']==n)][lo:hi] if math.isfinite(v)]
  scale=max(map(abs,vals),default=1) or 1;scale*=1.12
  xy=lambda t,v:(left+(t-span[0])/(span[1]-span[0])*(right-left),bottom-(v+scale)/(2*scale)*(bottom-top))
  for k in range(5):
   y=top+k*(bottom-top)/4;d.line((left,y,right,y),fill='#dddddd',width=1);d.text((10,y-18),format(scale-k*scale/2,'.3g'),font=small,fill='black')
   tx=span[0]+k*(span[1]-span[0])/4;x=xy(tx,0)[0];d.text((x-32,bottom+12),format(tx,'.4g'),font=small,fill='black')
  units=list(dict.fromkeys(next(c for c in r['channels'] if c['number']==n)['unit'] for n in nums))
  d.text((left,top-47),caption+' / '+','.join(units),font=small,fill='black')
  for k,n in enumerate(nums):
   ci=next(i for i,c in enumerate(r['channels']) if c['number']==n);color=COLORS[groups[key].index(n)%len(COLORS)]
   segs,dots=series_segments(times,values[ci],lo,hi,gaps,cfg.frequency)
   for seg in segs:
    if len(seg)>1:d.line([xy(t,v) for t,v in seg],fill=color,width=3)
   for t,v in dots:
    x,y=xy(t,v);d.ellipse((x-4,y-4,x+4,y+4),fill=color)
   name=shorten_tail(d,r['channels'][ci]['name'],small,355);d.text((left+k*395,bottom+65),name,font=small,fill=color)
 d.text((480,height-65),'时间 / s（相对本记录起点）；稀疏采样以点表示，缺口断线',font=small,fill='black')
 im.save(path)

def digital(work,record,span,path,label,materials,sources,channels):
 r,(cfg,times,values,states,_,_)=decoded(work,record,materials,sources);small,title=fonts(28)
 available={c['number']:c for c in r['digital_channels']}
 selected=[]
 for number in channels:
  if number not in available:raise CaseError('Unknown digital channel: '+str(number))
  selected.append(available[number])
 if not selected:raise CaseError('Explicit digital channel selection is empty')
 height=max(450,180+len(selected)*76);im=Image.new('RGB',(1800,height),'white');d=ImageDraw.Draw(im)
 d.text((50,20),label,font=title,fill='black');left,right=720,1730;gaps={g['sample'] for g in r['time_gaps']}
 for k,ch in enumerate(selected):
  ci=next(i for i,c in enumerate(r['digital_channels']) if c['number']==ch['number'])
  top=105+k*76;span_events=[e for e in r['events'] if e['channel']==ch['number'] and span[0]<=e['time_record_s']<=span[1]]
  state=ch.get('initial_state',0)
  for e in r['events']:
   if e['channel']==ch['number'] and e['time_record_s']<=span[0]:state=e['to']
  note='' if span_events else f'（恒{state}）'
  name=shorten_tail(d,ch['name']+note,small,650);d.text((25,top+3),name,font=small,fill='black')
  xy=lambda t,s:(left+(t-span[0])/(span[1]-span[0])*(right-left),top+45-32*s)
  for j in range(1,len(times)):
   if times[j]<span[0] or times[j-1]>span[1] or j+1 in gaps:continue
   a=max(times[j-1],span[0]);b=min(times[j],span[1]);s0,s1=states[ci][j-1],states[ci][j]
   d.line([xy(a,s0),xy(b,s0)],fill='#244c9b',width=3)
   if times[j]<=span[1]:d.line([xy(b,s0),xy(b,s1)],fill='#244c9b',width=3)
 for k in range(5):
  t=span[0]+(span[1]-span[0])*k/4;x=left+k*(right-left)/4;d.text((x-30,height-52),format(t,'.4g'),font=small,fill='black')
 d.text((25,height-52),'0/1原始状态；时间 / s',font=small,fill='black');im.save(path)
 return [c['number'] for c in selected]

def phasor(measurement,groups,path,label):
 small,title=fonts();im=Image.new('RGB',(1800,950),'white');d=ImageDraw.Draw(im);d.text((40,20),label,font=title,fill='black')
 valid=False
 for panel,key in enumerate(['U','I']):
  rows=[r for r in measurement['results'] if r['channel'] in groups[key]];cx=430+panel*900;cy=400;rad=265
  scale=max((r['fundamental_rms'] or 0 for r in rows),default=1) or 1
  d.ellipse((cx-rad,cy-rad,cx+rad,cy+rad),outline='#888888',width=2);d.line((cx-rad,cy,cx+rad,cy),fill='#bbbbbb',width=1);d.line((cx,cy-rad,cx,cy+rad),fill='#bbbbbb',width=1)
  d.text((cx-170,80),('电压' if key=='U' else '电流')+' 基波RMS相量',font=small,fill='black')
  for k,row in enumerate(rows):
   angle=row['relative_angle_deg'];mag=row['fundamental_rms'];color=COLORS[k%len(COLORS)]
   if angle is not None and mag is not None:
    valid=True;a=math.radians(angle);x=cx+rad*mag/scale*math.cos(a);y=cy-rad*mag/scale*math.sin(a);d.line((cx,cy,x,y),fill=color,width=5);d.ellipse((x-6,y-6,x+6,y+6),fill=color)
   val='不可用' if mag is None else format(mag,'.5g')+' '+row['unit'];ang='相角不可用' if angle is None else format(angle,'.4g')+'°'
   d.text((panel*900+35,715+k*47),shorten(d,row['name']+' '+val+' '+ang,small,865),font=small,fill=color)
 ref=next(r['name'] for r in measurement['results'] if r['channel']==measurement['reference'])
 d.text((45,905),'余弦有效值约定；相角参考：'+ref+'；U/I分别按各组最大幅值缩放',font=small,fill='black')
 if valid:im.save(path)
 return valid

def selected_csv(work,record,groups,path,materials,sources):
 r,(cfg,times,values,states,_,_)=decoded(work,record,materials,sources);nums=[n for g in groups.values() for n in g]
 indices=[next(i for i,c in enumerate(r['channels']) if c['number']==n) for n in nums]
 with path.open('w',encoding='utf-8-sig',newline='') as f:
  out=csv.writer(f);out.writerow(['file_row','native_sample','time_record_s']+[r['channels'][i]['name']+' ['+r['channels'][i]['unit']+']' for i in indices])
  for j,t in enumerate(times):out.writerow([j+1,r['native_sample_numbers'][j],t]+[values[i][j] if math.isfinite(values[i][j]) else '' for i in indices])
