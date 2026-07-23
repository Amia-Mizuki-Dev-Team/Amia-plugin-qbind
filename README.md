# Amia-plugin-qbind

QQ 官方机器人身份绑定插件。

## 项目定位

QBind 将 Gensokyo 输出的虚拟身份映射为 canonical QQ 身份，供需要真实 QQ 归属的插件使用。它不负责业务资产、消息统计或个人卡片展示。

当前本地运行目录提供 `qbind`、`qunbind`、`get_real_qq()`、`is_bound()`、`ensure_bound()` 和 `IdentityResolver` 等公共能力。

## 绑定边界

- `qbind` 发起绑定，确认后写入映射；
- `qunbind` 清除绑定；
- `get_real_qq()` 和 `is_bound()` 提供同步查询；
- `ensure_bound()` 由需要绑定的具体命令显式调用；
- `IdentityResolver` 将虚拟会话身份解析为 Core 的 canonical 身份。

普通消息不会被全局绑定前置处理器拦截；只有实际需要绑定的命令才应主动检查绑定状态。

## 跨插件使用

其他插件应通过 NoneBot 加载依赖和 Core 协议协作，不应直接读取 QBind 数据库：

```python
from nonebot import require

core = require("amia_core")
require("qbind")
```

## 当前状态

主体已完成。当前维护重点是 Gensokyo 映射集成和实际 Bot 验证。

## 测试

在插件目录执行：

```text
python -m unittest discover -s tests -v
```

仓库只发布源码、测试和说明文档。运行时生成的 `binds.json`、数据库、日志、配置和凭据均不纳入版本控制。

## 维护边界

不在本插件中实现 Economy、Send、Profile 或其他业务逻辑，也不建立第二套账号绑定系统。
