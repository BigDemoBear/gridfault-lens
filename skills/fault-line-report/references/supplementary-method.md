# 按需补充

已有计算、绘图和普通附件不需要补充方法会话，直接analyze及compose --analysis/--attach。确需新脚本计算时保留原生输入、脚本、结果、假设和错误，分清内置复核与模型补充方法。

现有可选限时入口保留：start-run CASE --output RUN，然后method-start RUN request.json --output METHOD。request含variable（所选变量）、goal、existing_tools_insufficient、method、inputs（绝对文件路径数组）。只在已有能力不足且补充有价值时使用，不为打包普通图证走此入口。

默认准备和执行共用120秒截止时间；在METHOD内保存Python脚本，用method-execute METHOD SCRIPT执行，最多一次执行与一次修正；method-progress保存步骤，method-status读取日志及部分结果；method-finish METHOD result.json完成。result含summary、values、assumptions。超时保留已有材料、不重建会话重置限额。

完成目录作为compose --attach METHOD纳入一次，a:supplement#/values/...引用标量。核查页保留方法与原生输入记录；方法结果不自动获得内置数值算法的复核承诺。
