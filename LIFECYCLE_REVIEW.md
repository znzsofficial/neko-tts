# TTS 生命周期并发复审（2026-10-22，本地）

## 结论与归因

- 宿主固定为 MaiBot `c602a2780f4f4dda4a6be46d67abc1cfb1ad420c`，SDK 为官方 PyPI `maibot-plugin-sdk==2.16.0`。结论来自精确源码，不以 main 文档或生产探针替代。
- 普通 self 配置 watcher / WebUI apply 有 Host 侧串行化，但**不存在涵盖 Runner 配置回调和卸载的串行契约**。通知超时后旧回调仍可能执行；停止时 Host 取消等待也不取消 Runner 中已启动的回调。因此并发可达，但不声称生产已经发生过泄漏。
- `50e65a1` 的两个 `_reset()` 可同时等待旧 engine.close，随后分别创建 engine，覆盖中间 engine；配置回调与卸载竞争还可能使已卸载实例重新 ready。丢失的是 engine 的所有权，若中间 engine 已被新业务使用，也可能遗留其 HTTP session。
- **这是既有隐患，不是 `50e65a1` 新引入的问题**：`git diff 15a5c06 50e65a1 -- plugin.py` 没有修改 `on_load/_reset/on_unload/on_config_update`，只修改 `_render` 的取消区分和命令发送诊断。本轮未发现需归因给该提交的新生命周期缺陷。

## 官方精确源码证据

以下链接全部固定同一宿主提交。GitHub raw 连接失败后改读官方 Contents API 的 base64 源码；路径/行号均来自解码后的原文件。

1. [integration.py:190-267](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/integration.py#L190-L267)：`apply_plugin_config` 持 `_plugin_file_update_lock`，await supervisor 配置通知；失败会抛出并离开锁。不能忽略这层串行化而说每次普通连续保存都会竞争。
2. [integration.py:1948-1980](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/integration.py#L1948-L1980)：watcher 仅把 plugin_id 加入 pending 集合，单个 `_drain_plugin_changes` 顺序调用 apply；失败被记录后继续下一轮。底层 FileWatcher `_run/_dispatch_changes` 自身也按批 await 回调，但不能为独立 Runner RPC 提供锁。
3. [supervisor.py:875-920](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/host/supervisor.py#L875-L920)：通知使用 `plugin.config_updated`，`timeout_ms=10000`；异常转为 False。
4. [rpc_server.py:179-244](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/host/rpc_server.py#L179-L244)：`send_request` 等待本地 future；超时仅 pop pending / metadata 并抛错，**不向 Runner 发送取消**。
5. [rpc_client.py:244-319](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/runner/rpc_client.py#L244-L319)：Runner 每个 request 都 `create_task(_handle_request(...))`；handler 直接 await，无按插件生命周期串行队列，也没有按 envelope.timeout_ms 包裹远端执行期限。
6. [runner_main.py:2392-2422](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/runner/runner_main.py#L2392-L2422)：配置 handler 先应用配置，再调用实例 `on_config_update`，不获取 `_reload_lock`。[757-766](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/runner/runner_main.py#L757-L766) 对 async handler 只是直接 await，没有 SDK 调度层隐式锁。
7. [integration.py:865-906](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/integration.py#L865-L906)：stop 取消 Host 配置 drain 后停止 supervisors。[supervisor.py:607-626](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/host/supervisor.py#L607-L626) abort 本地 pending 后调用 `_shutdown_runner`；[1935-1974](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/host/supervisor.py#L1935-L1974) 发送 prepare_shutdown / shutdown。[runner_main.py:2381-2389](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/runner/runner_main.py#L2381-L2389) shutdown 遍历 `_unload_plugin`，后者 [1670-1685](https://github.com/Mai-with-u/MaiBot/blob/c602a2780f4f4dda4a6be46d67abc1cfb1ad420c/src/plugin_runtime/runner/runner_main.py#L1670-L1685) 调用 on_unload。与尚存的 config RPC 之间没有共同锁。reload/unload_batch 的 `_reload_lock` 同样不约束 config handler。

Git blob IDs（核对 Contents API 返回）：

| 宿主文件 | blob SHA |
| --- | --- |
| integration.py | `0e5a40ba85c06db6479dfb8ef42f003d7eb5fb47` |
| host/supervisor.py | `5df8eb56633f877bb48c74f6de7d2e6c0aac87d0` |
| host/rpc_server.py | `96acf5730de758d33bf1bf79778a9f4145ef6f72` |
| runner/rpc_client.py | `1bbb9946441cbb505c0831f50bc63466912e8cc5` |
| runner/runner_main.py | `cba8fcd9cf5d8678fcf4591a561e8d5f1845f000` |
| ../config/file_watcher.py | `c1f4c6ac222daa8974880545ea5ab2b97ef128e7` |

SDK 精确分发依据：[PyPI 2.16.0 JSON](https://pypi.org/pypi/maibot-plugin-sdk/2.16.0/json) 与其中的官方 wheel：

- wheel SHA256：`30ed1c320fc9fc23a382b00b7309faddc9e5df317a4e3a5da3da46e3c065ddf6`（下载后实际校验一致）。
- `maibot_sdk/plugin.py` SHA256：`1158efa05018ae7073b147fa3d15ae3fb78a3abaa2555ee3df38fa4d3bee40e7`。
- 第453/461/469行的三个生命周期方法只是要求子类实现的 async 方法；基类初始化没有生命周期锁/队列，覆写方法也不经隐式包装。SDK 没有补足上述 Runner 缺口。

## 可达时序

1. 配置 A 已到 Runner，`on_config_update -> _reset` 正 await 任务退出或 engine.close。
2. 若耗时超过通知10秒，Host future 超时并释放 apply 锁；远端 A 仍在。后续不同配置 B 再到 Runner，产生第二个 task，可进入同一实例的 `_reset`。本地故障注入使用可控 close 等待模拟这个悬挂点，不代表真实 close 通常耗时10秒。
3. 更短的另一条路径：A 尚在执行时宿主停止；stop 取消 Host drain 等待，再发 shutdown，Runner 独立 task 对同一实例调用 on_unload。无需等待配置通知超时即可竞争。

## 最小修复与本地验收

- 实例单个 `_lifecycle_lock` 统一保护 load、self update、unload 和直接 `_reset`。锁内 helper 不重复获取锁；render / 后台 worker 不获取此锁，reset 可取消并等待它们退出，不引入锁反转。
- update 的 reset 与新 engine 创建在同一临界区；成功 close 后清空旧指针，避免重复关闭已结束的 engine。关闭中遭外部取消则保留旧指针与 not-ready 状态，下次生命周期可继续清理，取消不被吞掉。
- unload 标记阻止已排队/迟到的配置回调复活旧实例；显式 on_load 仍允许宿主重新激活实例。
- 六项新增回归：双配置更新、更新/卸载/迟到更新、卸载后更新、直接 reset 竞争/非递归锁、等待锁时外部取消、持锁 close 时外部取消及后续清理。现有 render/后台 worker/期限/外部取消回归保留。
- 将四项核心竞争回归对 `git show 50e65a1:plugin.py` 在内存执行：**4项均预期失败、0 error**（重复关闭、重复创建/卸载后复活）；当前实现全部通过。
- Windows/Python3.12：真实 SDK2.12 全套 **120通过、0 skip**；官方校验过的 SDK2.16 wheel 临时隔离导入同样 **120通过、0 skip**，未修改原 venv 依赖。
- `scripts/package.py`：10文件白名单打包/解压后真实 SDK2.12 导入成功。此审计文档和测试不进入发布ZIP。
- 不变：配置值/模型/发送模式/概率/音色/权限/Focus；未发模型/QQ请求，未访问或变更生产，未 push/deploy。源码可达性与合成并发回归不冒称生产端到端验收。
