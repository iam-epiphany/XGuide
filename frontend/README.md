# XGuide 前端

Vue 3 + Vite 单页应用，是 XGuide 校园个人 Agent 的学生端。前端只连接一个 XGuide FastAPI 后端，不再提供 Java/Python 双后端切换。

## 页面与能力

| 页面 | 用途 | 依赖接口 |
|---|---|---|
| **Today** | 汇总下一节课、今日事项、未来 7 天 DDL/考试和紧急提醒 | `/personal/today`、`/personal/reminders` |
| **Inbox** | 同步并查看经学生画像筛选的公开校园通知；可查看原文、标记状态、加入个人计划 | `/inbox/*`、`/student-profile` |
| **Chat** | 流式对话、知识检索、执行详情与 Trace 调试 | `/chat/stream`、`/search`、`/traces/*` |
| **个人数据弹窗** | 导入课表、维护待办/DDL/考试、修改通知筛选条件 | `/personal/schedule/*`、`/personal/todo/*`、`/student-profile` |

所有个人数据均由后端根据登录 Cookie 隔离。学生画像只在 XGuide 本地用于 Inbox 排序，不会随通知同步请求发送到外部校园网站。

## 开发运行

要求 Node.js 20+（项目当前锁定 npm 依赖）。先在仓库根目录启动后端和 Redis；后端默认监听 `8100`。

```powershell
Set-Location 'D:\Agent-Project\XGuide'
$env:ECHOGUIDE_SERVE_STATIC='0'
.\.venv\Scripts\python.exe -m api.main
```

另开一个 PowerShell 启动 Vite：

```powershell
Set-Location 'D:\Agent-Project\XGuide\frontend'
npm install
$env:VITE_PYTHON_API_URL='http://localhost:8100'
npm run dev
```

访问 `http://localhost:5175`。开发服务器会将 `/api/*` 请求代理到 `VITE_PYTHON_API_URL`；未设置时默认代理到 `http://localhost:8100`。

常用命令：

```powershell
npm run build       # 生成 dist/ 静态资源
npm run demo:capture # 运行 Playwright 演示截图脚本
```

## 部署

完整部署应从仓库根目录执行：

```powershell
Set-Location 'D:\Agent-Project\XGuide'
docker compose up -d --build
```

Nginx 提供前端并把 `/api/*` 转发给容器内的 XGuide 服务，统一入口为 `http://localhost:8088`。后端 Swagger 为 `http://localhost:8100/docs`（Docker Compose 也会映射该端口）。

无需单独运行 `frontend/docker-compose.yml`；它不是当前完整产品栈的推荐入口。
