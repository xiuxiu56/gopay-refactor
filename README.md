# GoPay 本地控制台 Python 重构版

这是原 GoPay 精简版旁路建设的 Python 重构项目。旧项目保持原样；新数据、任务、认证和后续前端都位于本目录。

## 当前阶段

P4 已完成以下能力：

- Python 3.11 `src` 工程、FastAPI 应用和根目录 `main.py` 启动入口。
- SQLite WAL、外键、忙等待、Alembic 事务迁移和独立 AES-GCM 数据密钥。
- 单管理员设置、登录、Cookie 会话、CSRF、来源校验和登录限流。
- 旧账号、号码池、支付状态、支付任务和短信配置的预检与幂等迁移。
- SQLite 持久化任务队列、原子领取、固定 Worker 池、租约心跳和指数退避重试。
- 取消、人工重试、进程重启恢复，以及副作用任务的 `needs_review` 隔离。
- OTP 等一次性输入的加密保存、哈希去重、任务恢复和消费后密文清除。
- 账号、手机号、代理和 Snap 等资源可使用统一排他租约。
- GoPay、Midtrans 与 SMS-Activate 兼容短信平台的协议隔离适配层；不再调用旧 JSON 状态管理函数。
- 任务、账号 REST API 与基于 `change_log`、支持断线续传的 SSE 实时事件流。
- 参考 `iCloud-Privacy-Mail-v2` 的 Vue 3 管理前端：首次设置/登录、左侧导航、顶部状态、明暗主题、任务、账号、支付和设置页。
- GoPay 注册与已有账号登录可恢复状态机，包含一/二阶段 OTP、PIN 设置与 PIN 修改。
- SMSBower 与 Hero-SMS 独立配置加密保存，支持自动取号取码、旧验证码哈希去重，自动取码超时后切换页面手动 OTP。
- 批次任务持久化和批次级并发限制；账号流程按手机号、账号、短信激活和代理资源排他。
- Midtrans Snap 链接解析、GoPay 账号与支付资源租约、支付 OTP、两阶段 PIN 授权和扣款后状态核验。
- 支付中断的不确定副作用进入 `needs_review`，并通过只读远端复核任务确认最终结果。
- Vue 3 支付工作台支持账号选择、PIN 覆盖、OTP 提交、远端复核、SSE 实时刷新和自适应记录条数。
- 并发、恢复、认证、迁移、加密、API、SSE、账号与支付协议状态机自动化测试。

详细交付范围见 [P3 交付说明](docs/P3-交付说明.md) 和 [P4 交付说明](docs/P4-交付说明.md)。

## 开发启动

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/python main.py
```

默认地址为 `http://127.0.0.1:19081`。可直接覆盖监听参数：

```bash
.venv/bin/python main.py --host 127.0.0.1 --port 19081 --database data/app.db
```

也可以继续使用命令行入口：

```bash
.venv/bin/gopay-v2 db-upgrade
.venv/bin/gopay-v2 db-status
.venv/bin/gopay-v2 serve
```

首次运行后，通过 `/api/v1/auth/setup` 创建唯一的本地管理员。

## 任务接口

登录后可使用：

- `GET /api/v1/tasks/types`：查看已注册任务类型。
- `GET /api/v1/tasks`：分页和按状态查询任务。
- `POST /api/v1/tasks`：幂等创建任务。
- `GET /api/v1/tasks/{id}`：查看任务和事件。
- `POST /api/v1/tasks/{id}/cancel`：取消任务。
- `POST /api/v1/tasks/{id}/retry`：人工重新入队。
- `POST /api/v1/tasks/{id}/input`：提交 OTP 等一次性输入。
- `POST /api/v1/account-flows`：创建单个或批量注册/登录任务。
- `GET /api/v1/accounts`：查询账号公开字段。
- `POST /api/v1/payments`：使用 Midtrans Snap 链接创建可恢复支付任务。
- `GET /api/v1/payments`：查询支付意图公开摘要。
- `GET /api/v1/payments/{id}`：读取单个支付意图状态。
- `POST /api/v1/payments/{id}/reconcile`：创建只读远端状态复核任务。
- `GET /api/v1/settings/smsbower`：读取脱敏短信配置。
- `PUT /api/v1/settings/smsbower`：加密更新短信配置。
- `GET /api/v1/settings/hero-sms`：读取脱敏的 Hero-SMS 配置。
- `PUT /api/v1/settings/hero-sms`：加密更新 Hero-SMS 配置。
- `POST /api/v1/settings/hero-sms/test`：调用 `getBalance` 测试连接并返回余额。
- `GET /api/v1/realtime`：订阅 SSE 实时更新，支持 `Last-Event-ID`。

写接口要求携带登录 Cookie，并让 `X-CSRF-Token` 与 `gopay_v2_csrf` Cookie 一致。

当前注册的首批任务类型：

- `system.echo`：队列运行检查。
- `system.sleep`：固定并发和取消检查。
- `system.wait_input`：一次性输入暂停/恢复检查。
- `account.register`：GoPay 注册、OTP、钱包初始化和 PIN 设置。
- `account.login`：已有账号一/二阶段登录、PIN 设置或修改。
- `account.refresh`：刷新账号令牌和余额，结果直接持久化到 SQLite。
- `payment.execute`：执行可恢复 GoPay 支付并核验 Midtrans 最终状态。
- `payment.reconcile`：只读查询 Midtrans 远端状态，不重复执行支付副作用。

## 迁移旧数据

先执行只读预检：

```bash
.venv/bin/gopay-v2 import-legacy --source .. --dry-run
```

确认统计后执行迁移：

```bash
.venv/bin/gopay-v2 import-legacy --source .. --apply
```

迁移器不会修改旧 JSON/ENV 文件。相同文件摘要会跳过；源文件变化后，以确定性 ID 更新新数据库。

## 数据与备份

- `data/app.db`：唯一业务数据库。
- `data/app.db.key`：AES-GCM 数据密钥，必须和数据库成对备份。

数据库和密钥默认权限为 `0600`，数据目录默认权限为 `0700`。复制 `.env.example` 为 `.env` 后可调整 Worker、租约、重试、SSE 和旧协议源码路径。
