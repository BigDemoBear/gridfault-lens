# 工具输入与引用

主流程用extract、analyze、compose、deliver，无需start-run或每轮record-round。将产品src加入PYTHONPATH后可用python -m powerfault。输出用新目录。

## 原生材料

extract CASE --output MATERIALS生成materials.json、manifest.json、raw及texts。record包含channels、digital_channels实际编号及events的原生区间/触发时间；保留解析error，不修补原件凑连续样本。读取冻结原文，不仅依赖自动字段。

同时生成source-structure.json，作为可选的原文字段与结构查看文件。sources各项列出reference（如s:s00003）、原始路径、冻结文本路径和content。content.lines保留全部原文行；合法XML的content.xml递归列出任意标签、全部属性（@属性名）、非空文本（#text）、尾文本（#tail）、空节点及带序号的子节点，不按业务字段名筛选。原文行保留空白和原始顺序；XML键使用解析后的名称，命名空间保留URI。没有语义归类、单位换算或完整性评分。二进制或不可读源也列入目录，但content为null；格式错误保留原文及structure_error。此文件只覆盖extract已接收的资料，不会扩大输入格式范围。

从某项content选到标量后，沿用该项reference加#及content内部的JSON指针引用，例如s:s00003#/xml/...或s:s00003#/lines/12；不在指针中添加content。原文中的名称、desc及祖先结构由模型结合任务解释。自动fields继续按原有规则生成，source-structure不替换它，也不改变后续流程。

## 独立分析

analyze MATERIALS request.json --output ANALYSIS。request含可选measurements/figures数组。下面仅示意，实际编号、窗口必须按原生资料替换：

```json
{
  "measurements":[{"id":"local-fault","record":"r001","channels":{"U":[1,2,3],"I":[4,5,6]},"samples":[45,72],"reference":1,"angle_policy":"uncompensated","angle_note":"只描述同端共同窗口，不判跨端方向。"}],
  "figures":[
    {"id":"local-wave","kind":"waveform","record":"r001","channels":{"U":[1,2,3],"I":[4,5,6]},"range":[0.02,0.09],"caption":"本端故障电压电流"},
    {"id":"local-digital","kind":"digital","record":"r001","channels":[1,2],"range":[0.02,0.09],"caption":"本端目标开关量"},
    {"id":"local-phasor","kind":"phasor","measurement":"local-fault","caption":"本端故障窗相量"}
  ]
}
```

ID以小写字母开头，后接字母/数字/短横线。samples为一基文件行、两端包含；range是相对本记录起点的秒。模拟量channels恰含U/I数组，一个可为空；同组单位、侧别须一致。每项可用不同记录/支路通道。reference必须是选中通道。

angle_policy默认metadata_checked：偏斜条件不足时保留可用幅值而不强给相角。若有用途可显式选uncompensated并说明限制。逐量availability/reasons及拟合残差不能忽略，null值不得填成可用。

输出measurements.json的键为m:local-fault等；analysis.json保存输入绑定和实际请求；figures保留图，samples导出选中模拟/数字量逐点CSV。多个分析目录一起纳入时测量ID不得重名。只改文字直接复用结果目录。

## 两站端与报告源稿

context.json含线路及恰好两个站端，每端records可有多组；缺端用空数组。一组录波不可归属两端。

```json
{"line":"目标线路","ends":[{"name":"本端站","records":["r001","r002"]},{"name":"对端未提供","records":[]}]}
```

可加variables指定用户要求的子集；省略默认15类，用list-variables查看。

report-source.md是模型写的Markdown，主章节为故障信息、录波图解析、综合总结。自行组织表格/缺项和单位解释，数值用引用而不手填带单位的数字：

- `{{f:c000001#}}`：原生标量字段；对象须引用具体路径，如`{{f:c000001#/magnitude}}`。
- `{{s:s00003#/xml/RecorderReport/Fault[1]/RelayActTime1[1]/@value}}`：冻结XML属性；同节点desc可另引作单位解释依据。
- `{{s:s00003#/lines/12}}`：冻结原文零基第12行，按原义展示。
- `{{r:r001#/events/0/time_record_interval_s/0}}`：原生事件区间边界。
- `{{m:local-fault#/results/0/fundamental_rms}}`：选窗基波；results顺序为请求U后接I。
- `{{m:local-fault#/results/0/relative_angle_deg}}`：共同参考相角，不能引用null。

JSON指针键中的/和~转义为~1和~0；XML同名节点序号从1开始。原值不自动换算。单位解释附原文依据；某周波值、选窗值不能冒充装置原记载最大值。

插图：`![图1 本端波形](analysis/ANALYSIS/figures/local-wave.png)`，ANALYSIS须是实际分析目录basename。也可链接`analysis/ANALYSIS/samples/r001.csv`。普通附件--attach PATH进入attachments/原名，无需方法登记。

compose MATERIALS report-source.md --context context.json --analysis ANALYSIS --output DRAFT。可重复--analysis/--attach。review.html展示原值、原文位置和数值条件。看实际成品后改源稿、compose新DRAFT，不改展开后的数字后重建清单。完成的限时方法目录可作为--attach纳入，使用a:登记ID引用result.json标量；它属于补充方法，不冒充内置计算。

## 已有交付包的一致性核对

`powerfault verify-markdown DELIVERY`用于命令行集成或已有交付包的技术检查，DELIVERY为实际目录。它重放所用计算，并核对来源、源稿展开、文件和本地链接的一致性；不重新完成一次AI故障分析，也不认证电气解释。日常阅读可直接打开报告与核查页，无需先运行该命令。
