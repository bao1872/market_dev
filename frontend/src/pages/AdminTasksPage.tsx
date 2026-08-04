// 任务中心（受保护路由，admin only）
// [管理后台优化 PRD §8.3] 只负责执行层和基础设施层，不负责判断业务数据是否正确。
//
// P0：将原「任务与事件」页（AdminJobsPage）改名并迁移到 /admin/tasks，
//     保持原有「定时任务 / 策略计算 / Worker 心跳 / 事件 / 失败投递」能力，
//     与业务数据生产（/admin/data-production）职责分离。
import AdminJobsPage from './AdminJobsPage'

export default function AdminTasksPage() {
  return <AdminJobsPage />
}
