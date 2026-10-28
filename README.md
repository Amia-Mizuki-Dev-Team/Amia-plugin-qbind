# Amia-plugin-qbind

QBind 是 Amia 插件生态唯一的个人/群组身份绑定边界。它负责保存外部 Bot 身份与 canonical 身份之间的绑定状态，并向其他插件提供解析接口；它不负责 Economy、Profile、Send 或其他业务资产。

当前本地实现已完成 QBind V2 的 M0/M1 基础安全边界：个人和群绑定都使用 SQLite 状态存储和审计日志，确认请求绑定 Bot `self_id`、外部身份和有效期。生产配置已接入本地 QQ 邮箱认证：个人绑定验证码只发送到用户提交的 `<QQ号>@qq.com`，群绑定验证码只发送到发起绑定的群主/管理员本人已完成个人认证的 QQ 邮箱；两次验证码都通过后，个人和群才会进入 `verified` 并放行普通业务。不会把 Gensokyo `type=5` 的“请求成功”误当作 QQ/群号归属验证。真实 SMTP、后端和生产实机验收仍不能由本地单元测试代替。

## 重要信任边界

- Gensokyo `getid?type=5` 调用 `UpdateVirtualValue(oldRowValue, newRowValue)`：`oldRowValue` 是虚拟 user ID，`newRowValue` 是 canonical QQ。HTTP 成功只说明后端接受了映射更新，不证明用户拥有该 QQ 号。个人确认和同 QQ 的 QBind 重同步会调用此接口；SUPERUSER 可用 `批量重同步用户映射` 对当前生效的个人记录进行批量同步。
- 因此未启用邮箱模式时，个人确认后的记录状态仍是 `declared`，兼容后台可通过 `get_real_qq(user_id, self_id)` 使用该声明值；启用 `QBIND_EMAIL_REQUIRED=true` 后，`qbindcode` 会把声明提升为 `verified`，普通业务门禁也只接受 `verified`。`QbindIdentityResolver` 只把已验证个人身份作为 `canonical_user_id` 返回；跨群共享等高信任功能仍应要求 `verified`。
- 群内确认只能证明申请人能在当前虚拟群中发消息，不能单独证明手填真实群号属于当前虚拟群。启用邮箱模式时，`qgconfirm` 先把候选真实群号放入 `pending_verification`，再把验证码发到发起人本人已完成个人认证的 `<QQ号>@qq.com`；只有 `qgcode` 成功后才写入 `verified`，群业务不会在邮箱认证前放行。未启用邮箱模式时，普通 `qgconfirm` 仍兼容本地 `declared` 流程。
- QQ 邮箱验证码精确验证挑战中保存的 candidate QQ：先将原事件键的个人记录置为 verified，再预检候选事件键。type=5 明确返回 status=success 只表示后端接受映射更新，不证明 QQ 所有权。在当前部署假设（候选键等于映射后事件键）成立时，才会在同一 self_id 下原子创建 candidate 事件键到该 canonical 的 verified 别名；这不是适配器保证：适配器不提供映射 readback 或事件身份保证。原记录不会被替换。
- 失败恢复只从当前原 source `S = (self_id, event.user_id)` 查找：必须恰有一条 purpose/scope/actor/external/candidate/status 精确匹配、group 为空的 `verified` challenge，才重试同一 source 到 QQ 的 type=5 同步；不会从候选事件键或 `user_id == candidate` 回查历史 challenge。0 条、多条、已撤销目标、映射冲突、source 版本在同步期间变化或远端结果失败/未知时均 fail-closed，不创建别名；业务门禁仍只精确查询当前 `(self_id, user_id)`。
- QBind/QGroupBind 不直接读写 Gensokyo 的 idmap 数据库。批量命令 `批量重同步用户映射` 默认只输出汇总预览；追加 `执行` 后，才会串行同步 `declared`/`verified` 的生效个人记录。若跨 Bot 记录对同一虚拟 user ID 指向不同 QQ，命令会中止；整个流程仅调用个人 type=5 映射，不调用群组/频道映射 API。`type=11` 仅保留为可选的可信群/用户联合验证来源。
- 未传 `self_id` 的 `get_real_qq()` 仅保留旧版单参数兼容行为，不会扫描所有 Bot 命名空间。新代码应使用 `get_verified_qq(user_id, self_id)` 或 Core 的 resolver。

- 个人 type=5 映射没有 Bot scope，因此单次确认、邮箱认证和重同步在写入前检查同一外部 virtual ID 的 active canonical QQ；跨 Bot 目标不一致时阻止远端写入，相同 pair 可复用。此检查只保护映射一致性，不代表身份所有权验证。

## 指令

个人身份：

- `qbind <QQ号>`：创建个人声明并发送一次性确认请求。
- `confirm_bind <请求令牌>`：确认本人请求；按钮会自动携带令牌，纯文本 `confirm_bind` 仍兼容当前用户的最新待确认请求。
- `qbindcode <6位验证码>`：在邮箱模式下提交发送到该 QQ 邮箱的验证码，完成个人 `verified` 认证。
- `批量重同步用户映射`：SUPERUSER 命令；默认仅输出汇总预览，追加 `执行` 才会串行同步当前生效的 `declared`/`verified` 个人映射。
- `cancel_bind <请求令牌>`：取消本人待确认请求。
- `qunbind`：为当前 Bot 下自己的绑定创建解绑确认请求。解绑时会再次检查记录归属，命令中填入其他人的 QQ 号不会触发后端操作。

群组身份：

- `qgbind <真实群号>`：必须在群聊中由群主/管理员或 Bot SUPERUSER 发起当前虚拟群绑定。
- `qgconfirm <请求令牌>`：重新检查当前 Bot、群、发起人和权限后提交群映射声明。
- `qgcode <6位验证码>`：在邮箱模式下提交发送到操作者本人 QQ 邮箱的验证码，完成群 `verified` 认证。
- `qgcancel <请求令牌>`：取消当前群的待确认请求。
- `qgstatus`：查询当前虚拟群的状态。
- `qgunbind`：创建并确认当前群解绑；只撤销群映射，不删除成员个人档案。
- `qgrebind <真实群号>`：在严格权限和二次确认下申请换绑；旧映射和历史记录保留，新映射先保存为 `declared`。

当前群组状态包括：`declared`、`pending_verification`、`verified`、`conflict` 和 `revoked`。未启用邮箱认证时，`declared` 与个人 QBind 的声明状态对应，可通过普通群绑定门禁，并由 `GroupIdentityResolver` 提供给后台群功能；启用邮箱认证后，普通群业务门禁只接受 `verified`，`pending_verification` 不会被当作已绑定使用。同一 Bot 命名空间内，一个虚拟群只有一条有效映射，一个真实群号不能自动被两个虚拟群占用；不同 Bot 的映射通过 `self_id` 隔离。

启用邮箱认证时，实际群绑定流程是：

1. 用户先在私聊发送 `qbind <自己的QQ号>`，点击确认后，从自己的 QQ 邮箱收到验证码。
2. 用户发送 `qbindcode <6位验证码>`，个人绑定进入 `verified`。
3. 在目标虚拟群内由群主/管理员发送 `qgbind <真实群号>`，再点击按钮或发送 `qgconfirm <请求令牌>`。
4. QGroupBind 保存候选真实群号和群名为 `pending_verification`，验证码只发到发起人本人已认证的 `<QQ号>@qq.com`。
5. 发起人发送 `qgcode <6位验证码>`，群映射进入 `verified`；之后普通群业务才会放行，并可显示“你的档案已分享至 `<群名>`”。

未启用邮箱认证时，流程退化为 `qbind -> confirm_bind` 和 `qgbind -> qgconfirm` 的本地声明兼容模式；这只适合开发/迁移，不满足“本人 QQ 邮箱 + 操作者本人 QQ 邮箱”安全要求。

## 使用前身份门禁

QBind 已注册统一的消息前置门禁。除绑定、状态查询和帮助指令外，业务消息必须满足：

- 私聊：当前 Bot 命名空间下存在当前用户的个人绑定记录；启用邮箱模式时必须为 `verified`；
- 群聊：同时存在当前用户的个人绑定记录，以及当前虚拟群的群绑定记录；启用邮箱模式时两者都必须为 `verified`，未完成 `qbindcode` 或 `qgcode` 不会执行群业务。

任一条件缺失时，业务 matcher 不会继续执行，并只提示对应的 `qbind <QQ号>` 或 `qgbind <真实群号>` 流程。门禁只检查 Bot 指令；普通群聊不会触发绑定提示。PJSK 转发在入后台前也会显式检查个人/群映射，使用带当前 Bot `self_id` 的查询将 `user_id`、`sender.user_id`、`group_id` 替换为 QBind 声明的身份；HRK/Sakura 回包仍通过原事件的虚拟 ID 发送给 OneBot。

## 持久化与迁移

默认数据库为插件目录下 `data/qbind.sqlite3`，运行时文件由 `.gitignore` 排除。可以通过以下环境变量指定路径：

```text
QBIND_DB_PATH=H:\\Amia-Develop\\data\\qbind.sqlite3
QBIND_DATA_DIR=H:\\Amia-Develop\\data
QBIND_GENSOKYO_API_URL=http://127.0.0.1:15630
QBIND_GENSOKYO_TIMEOUT=5
QBIND_USE_MARKDOWN=true
QBIND_EMAIL_REQUIRED=true
QBIND_SMTP_HOST=smtpdm.aliyun.com
QBIND_SMTP_PORT=465
QBIND_SMTP_SSL=true
QBIND_SMTP_USER=verify@mizuki.top
QBIND_SMTP_FROM="Amia Verification" <verify@mizuki.top>
QBIND_SMTP_PASSWORD=<本机填写的阿里云 SMTP 密码>
QBIND_EMAIL_CODE_TTL_SECONDS=600
QBIND_EMAIL_MAX_ATTEMPTS=5
QBIND_SMTP_TIMEOUT=15
```

`QBIND_SMTP_PASSWORD` 只能写在本机 `.env`/`.env.dev`，不要提交到 Git、截图或发送到聊天中。QBind 不接受用户自定义邮箱地址：个人和群绑定的收件地址都由已经提交并完成绑定的 QQ 号自动转换为 `<QQ号>@qq.com`。SMTP 密码缺失或发信失败时，绑定请求会失败，不会伪造成功状态。

首次加载时，如果同目录存在旧 `binds.json`，QBind 会：

1. 先创建 `binds.json.migration-<UTC>.bak` 回滚副本；
2. 将旧映射导入 `legacy` Bot 命名空间，状态设为 `declared`；
3. 跳过旧实现中的 `QQ -> QQ` 反向占位行；
4. 对重复 canonical QQ 保留冲突审计，不自动合并身份；
5. 保留原始 JSON，不删除、不覆盖；
6. 在 SQLite `audit_log` 和 `storage_meta` 中记录迁移结果。

迁移不会猜测旧记录属于哪个 Bot，也不会自动升级为 `verified`。正式切换前应根据实际 `self_id` 逐项核对，必要时通过受信审核 API 重新建立命名空间内的绑定。

## Public API

```python
from nonebot import require

qbind = require("qbind")

# 兼容查询：可能返回 declared 或 verified
declared_or_verified = qbind.get_real_qq(user_id, self_id)

# 可信 canonical 查询：只有 verified 才返回 QQ
canonical_user_id = qbind.get_verified_qq(user_id, self_id)
group_status = qbind.get_group_binding_status(group_id, self_id)
canonical_group_id = qbind.get_real_group(group_id, self_id)

# 一次获取个人和群状态
context = await qbind.resolve_context_identity(
    self_id=self_id,
    user_id=user_id,
    group_id=group_id,
)

# 仅查询已经完成双侧可信认证的关系；参数使用真实 QQ/真实群号。
groups = qbind.get_user_groups(real_qq, self_id)
members = qbind.get_group_members(real_group_id, self_id)
```

## Amia-Sync 群/用户档案投影

QBind 还会保存一个受保护的用户↔群关系投影，供 Amia-Sync 读取。它不是
“抓取群成员列表”：只有用户个人绑定为 `verified`、当前群绑定为
`verified`，并且用户实际触发了一个已经通过 QBind/QGroupBind 门禁的 Bot
指令时，才会记录一条关系。普通聊天、未完成邮箱认证的用户、
`declared`/`pending_verification` 群都不会进入可信关系列表。

关系记录按当前 Bot `self_id` 隔离，同时保存虚拟 ID、真实 QQ/群号、群名、
首次/最近观察时间和来源。个人解绑或群解绑会向下游投影发送撤销事件；本地
QBind 数据库仍由 QBind 独占，Amia-Sync 插件只通过内部密钥保护的服务接口
同步，不直接读取 `qbind.sqlite3`。

当前 Amia-Sync Server 的读取边界是：

- `GET /v1/me/groups`：登录用户自己的已验证群列表（含真实群号和群名）；
- `GET /v1/me/groups/{真实群号}/users`：只有已验证属于该群的 App 用户才能读取该群的已验证用户列表；
- `GET /internal/v1/qbind/groups/{真实群号}/users?bot_scope=...` 和
  `GET /internal/v1/qbind/users/{真实QQ}/groups?bot_scope=...`：仅供内部服务使用，必须带 `X-MizukiSync-Internal-Key`。

Amia-Sync Bot 通过 `POST /internal/v1/qbind/memberships` 接收 QBind 的
幂等新增/更新和解绑撤销事件。服务端只保存经过边界清洗的关系投影，不接收
QQ 邮箱密码、Provider 凭据、群消息内容或设备密钥。

可信审核适配器必须在独立流程中完成证据核验后调用：

```python
qbind.approve_personal_binding(
    self_id=self_id,
    user_id=user_id,
    verified_by="trusted-reviewer-id",
    verification_source="external-proof-v1",
)

# Gensokyo/适配器为同一真实 QQ 生成第二个虚拟 user_id 时，
# 只能由可信审核流程显式添加别名；普通 qbind 声明不能复用已有 canonical。
qbind.approve_personal_alias(
    self_id=self_id,
    user_id=another_virtual_user_id,
    canonical_user_id=verified_qq,
    verified_by="trusted-reviewer-id",
    verification_source="external-proof-v1",
)
```

群绑定声明默认使用 `GensokyoGroupVerifier` 的本地流程。启用邮箱认证时，邮箱验证码成功后由 QGroupBind 自己调用本地状态转换进入 `verified`；如果部署环境还支持可信的 `type=11` 联合验证，也可以注册自定义 `GroupVerificationAdapter`，或由受信审核流程调用 `approve_group_binding_request()`。不应在普通聊天 handler 中因为用户输入了数字就直接调用这些审核 API。

其他插件不应读取 `qbind.sqlite3` 或 `binds.json`，也不应使用群号、昵称、数字号段或 OpenID 字符串相似性推断个人 canonical 身份。群级后台功能应通过 `GroupIdentityResolver` 读取已声明或已验证的群映射，并根据 `group_verification_status` 处理后续审核；个人档案只有在 `canonical_user_id` 已验证时才可以跨群共享。

## Gensokyo 兼容边界

本仓库只根据工作区内的 Gensokyo-NewQQ 源码快照确认了以下静态事实：`type=5` 接收数值 `oldRowValue`/`newRowValue`，调用 `idmap.UpdateVirtualValue()`，并返回 `{"status":"success"}`；`type=11` 通过虚拟群和虚拟 user 查询 `firstRealValue`/`secondRealValue`；`use_uin` 和 `idmap_isolation` 会影响命名空间/键格式。Release015、当前生产二进制、`type=11` 线上返回、`use_uin=true/false` 实机行为、Markdown/Keyboard 回调和真实 QQ 群归属证明均未在本地完成验证，不能以自动化测试 PASS 替代。

## 测试

在插件目录执行：

```powershell
& H:\Amia-Develop\.venv\Scripts\python.exe -m unittest discover -s tests -v
& H:\Amia-Develop\.venv\Scripts\python.exe -m py_compile __init__.py storage.py gensokyo.py group.py identity.py
```

测试覆盖本地状态转换、self_id 隔离、旧 JSON 迁移备份、个人声明/验证分离、个人确认超时/越权/冲突/解绑故障、群绑定权限/并发申请/跨群令牌/超时/冲突/解绑/重启恢复、个人+群使用前门禁、QQ 邮箱验证码哈希/错误次数/个人与群绑定认证流程，以及跨群共享 canonical 身份的本地矩阵。

`tests/test_acceptance_matrix.py` 对应附件第十阶段的可自动化部分（10.1–10.3）。这些测试不证明 Gensokyo 后端、真实 QQ 号所有权、真实 QQ 群归属、生产配置、Haruki 双 ID、真实 OneBot 连接或实机交互已经验证；第 10.4 项必须在目标环境完成。

## 维护边界

不在本插件中实现 Economy、Send、Profile 或其他业务逻辑，也不建立第二套账号绑定系统。

## Release010 发布说明

Release010 继续以 `self_id + user_id` 字符串组合作为外部身份，不把 OpenID 强制转换为数字 QQ。测试加载器已改为当前 `src/plugins` 目录结构；`binds.json`、数据库、配置、日志和真实绑定记录始终属于运行数据，不会上传。

## 代码核验与十月状态（2026-10-14）

QBind 负责外部会话身份到 canonical QQ 身份的映射；`identity.py` 中 `QbindIdentityResolver` 向 `amia-core` 输出 `ResolvedIdentity`。未绑定身份不能凭昵称推断真实账号，运行时绑定数据不得加入 Git。2026-10-01 至 2026-10-14 默认分支未发现新提交。
