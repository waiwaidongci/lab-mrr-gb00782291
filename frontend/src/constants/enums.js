export const roles = {analyst: '分析员', reviewer: '复核员', viewer: '只读访客'};
export const states = {queued: '排队', running: '计算中', completed: '已计算', failed: '需修正'};
export const decisions = {approved: '已批准', returned: '已退回'};
export const actions = {import:'导入数据',ledger_import:'导入台账',ledger_snapshot:'合并台账',queue:'提交计算',completed:'完成计算',failed:'计算失败',approved:'批准发布',returned:'退回结果',schedule:'建立计划',cancel:'停用计划',delivered:'报告入箱'};
export const ledgerResults = {已采纳:'已采纳',撤下越界:'撤下越界',暂扣待移交:'暂扣待移交',重复忽略:'重复忽略',非归属场地:'非归属场地',已生效:'已生效',未生效:'未生效'};
