from pathlib import Path
import argparse,json
from . import __version__
from .core import extract,load
from .report import render,verify_report
from .markdown import verify_report as verify_markdown,record_review
from .model import CaseError
from .workflow import start, draft, record_round, deliver, variable_catalog
from .semantic import first_review, fill
from . import attempts
from . import analysis, authoring
from .waveform import decoding_session

def _main():
 p=argparse.ArgumentParser(description='原生取证与计算 → 模型成稿和自主纠错 → 一次核对交付')
 p.add_argument('--version',action='version',version=__version__);sub=p.add_subparsers(dest='command',required=True)
 q=sub.add_parser('extract',help='只提取故障线路模板所需的原生材料');q.add_argument('case',type=Path);q.add_argument('--output',type=Path,required=True)
 q=sub.add_parser('list-variables',help='查看限定分析变量与默认选择')
 q=sub.add_parser('analyze',help='按记录/通道/窗口独立计算绘图，无需报告表单');q.add_argument('materials',type=Path);q.add_argument('request',type=Path);q.add_argument('--output',type=Path,required=True)
 q=sub.add_parser('compose',help='展开模型编写的带引用源稿，保留两站端及端内多录波证据');q.add_argument('materials',type=Path);q.add_argument('source',type=Path);q.add_argument('--context',type=Path,required=True);q.add_argument('--analysis',type=Path,action='append',default=[]);q.add_argument('--attach',type=Path,action='append',default=[]);q.add_argument('--output',type=Path,required=True)
 q=sub.add_parser('start-run',help='兼容旧表单入口；轮数仅作自查建议，不限制纠错');q.add_argument('case',type=Path);q.add_argument('--output',type=Path,required=True);q.add_argument('--iterations',type=int,default=2);q.add_argument('--variables',nargs='+')
 q=sub.add_parser('first-review',help='可选兼容入口：汇集六类参数表缺项及AI疑点，非必经步骤');q.add_argument('run',type=Path);q.add_argument('report',type=Path);q.add_argument('--findings',type=Path)
 q=sub.add_parser('semantic-fill',help='可选兼容入口：接纳首轮原文补取结果；纠错用计划中的明确选值');q.add_argument('run',type=Path);q.add_argument('response',type=Path);q.add_argument('--output',type=Path,required=True)
 q=sub.add_parser('method-start',help='现有能力不足时登记一次补充方法，准备与执行默认共享120秒');q.add_argument('run',type=Path);q.add_argument('request',type=Path);q.add_argument('--output',type=Path,required=True);q.add_argument('--seconds',type=float,default=120)
 q=sub.add_parser('method-progress',help='保存补充方法当前步骤和已有结果');q.add_argument('folder',type=Path);q.add_argument('--step',required=True);q.add_argument('--note',required=True)
 q=sub.add_parser('method-execute',help='在原始截止时间内执行登记目录中的Python补充方法');q.add_argument('folder',type=Path);q.add_argument('script',type=Path)
 q=sub.add_parser('method-status',help='读取完成/暂停状态、日志与部分产物清单');q.add_argument('folder',type=Path)
 q=sub.add_parser('method-finish',help='登记补充方法结果和假设');q.add_argument('folder',type=Path);q.add_argument('result',type=Path)
 q=sub.add_parser('markdown',help='按表单生成Markdown；模型源稿入口用compose');q.add_argument('materials',type=Path);q.add_argument('plan',type=Path);q.add_argument('--run',type=Path,required=True);q.add_argument('--output',type=Path,required=True)
 q=sub.add_parser('record-round',help='登记本轮修改，最后一轮汇总变量并记录成文决定');q.add_argument('run',type=Path);q.add_argument('report',type=Path);q.add_argument('review',type=Path)
 q=sub.add_parser('deliver',help='deliver DRAFT REVIEW.json --output DELIVERY，完整核对一次后打包');q.add_argument('run',type=Path);q.add_argument('decision',type=Path,nargs='?');q.add_argument('--output',type=Path,required=True);q.add_argument('--materials',type=Path)
 q=sub.add_parser('verify-markdown',help='核对Markdown成品的原始快照、数值、链接和文件一致性');q.add_argument('report',type=Path)
 q=sub.add_parser('record-review',help='保存AI已完成的成文后核对记录（五项加修订与复查），并绑定具体报告');q.add_argument('report',type=Path);q.add_argument('audit',type=Path);q.add_argument('--result',choices=['passed','needs_revision'],required=True)
 q=sub.add_parser('report',help='按AI已核对的填写计划输出Word报告');q.add_argument('materials',type=Path);q.add_argument('plan',type=Path);q.add_argument('--output',type=Path,required=True)
 q=sub.add_parser('verify',help='核对Word成品的原始快照、参数、引用和文件一致性');q.add_argument('report',type=Path)
 a=p.parse_args()
 try:
  if a.command=='extract':r=extract(a.case,a.output)
  elif a.command=='analyze':r=analysis.run(a.materials,a.request,a.output)
  elif a.command=='compose':r=authoring.compose(a.materials,a.source,a.context,a.output,a.analysis,a.attach)
  elif a.command=='list-variables':r=variable_catalog()
  elif a.command=='start-run':r=start(a.case,a.output,a.iterations,a.variables)
  elif a.command=='first-review':r=first_review(a.run,a.report,a.findings)
  elif a.command=='semantic-fill':r=fill(a.run,a.response,a.output)
  elif a.command=='method-start':r=attempts.start(a.run,a.request,a.output,a.seconds)
  elif a.command=='method-progress':r=attempts.progress(a.folder,a.step,a.note)
  elif a.command=='method-execute':r=attempts.execute(a.folder,a.script)
  elif a.command=='method-status':r=attempts.status(a.folder)
  elif a.command=='method-finish':r=attempts.finish(a.folder,a.result)
  elif a.command=='markdown':r=draft(a.run,a.materials,a.plan,a.output)
  elif a.command=='record-round':r=record_round(a.run,a.report,a.review)
  elif a.command=='deliver':
   if (a.run/'report-source.md').is_file():
    if a.decision is None:raise CaseError('Supply the final REVIEW.json for this model-authored report')
    r=authoring.deliver(a.run,a.decision,a.output)
   else:r=deliver(a.run,a.decision,a.output,a.materials)
  elif a.command=='verify-markdown':r=authoring.verify_report(a.report) if (a.report/'report-source.md').is_file() or (a.report/'data/evidence/report-source.md').is_file() else verify_markdown(a.report)
  elif a.command=='record-review':r=record_review(a.report,a.audit,a.result)
  elif a.command=='report':r=render(a.materials,a.plan,a.output)
  else:r=verify_report(a.report)
  print(json.dumps(r,ensure_ascii=False,indent=2));return 1 if r.get('ok') is False else 0
 except (CaseError,OSError,KeyError,ValueError) as exc:p.exit(2,str(exc)+'\n')
def main():
 with decoding_session():return _main()
if __name__=='__main__':raise SystemExit(main())
