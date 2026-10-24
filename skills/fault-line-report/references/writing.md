# 最后一次核对与交付

先看实际报告和图，再核对修订。默认两轮自查无须每轮写JSON；最后只登记一次所选变量、依据和限制。

```json
{
  "report_sha256":"compose返回的实际报告哈希",
  "decision":{"can_report":true,"reason":"已审核资料支持什么范围，限制在哪里。"},
  "items":[{"variable":"fault_phase","status":"confirmed","note":"说明对象、阶段及判断依据。","evidence":["review.html#ref-1","analysis/ANALYSIS/figures/local-wave.png"]}],
  "issues":"未解决的资料缺口、冲突及未验证范围。"
}
```

示例只列一项，成功交付需覆盖context所选变量。每项必需variable/status/note/evidence；可按实际加object/stage/value/unit/time_basis，无关字段不用强填。同一变量可分对象和阶段多列。status为confirmed/unavailable/conflict/not_applicable，已核对或冲突必须有可打开的原生/计算证据。缺项说明已查什么及缺什么，不统一写待核查。

evidence为DRAFT内相对路径或HTML锚点。不能只引用模型报告、源稿或清单当事实依据。数值、对象、阶段、单位、侧别、时基及条件应在note或对应字段清楚表达。confirmed仅表示AI已核对，不保证人工采用。

deliver DRAFT review.json --output DELIVERY重放所纳入计算，核对源稿展开，按最终目录重写链接，检查整包后生成清单。失败按具体原因修改后重检；无候选数阻断，不重新分析第二遍能否成文。

依据不足时decision.can_report=false，说明具体障碍，允许只保留已核对部分。单项缺失不自动否决整个报告。最终数据保留原件、原文、选窗、样本、图、模型源稿和人工审查记录，供人核查AI工作真假。verify成功只证明来源/数值/文件/链接检查，不证明全部语义判断。
