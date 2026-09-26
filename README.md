# Solo Runtime

Runtime 的 apps 与 DolphinScheduler 任务一一对应。各 app 使用锁定的独立环境调用 Scheme；Runtime 自身不安装或导入 Scheme。

```shell
uv sync
uv run solo-manage apps factor --input-file /shared/runs/123/input.json
uv run solo-manage apps model --input-file /shared/runs/456/input.json
uv run solo-manage apps optimize --input-file /shared/runs/457/input.json
uv run solo-manage apps control --input-file /shared/runs/458/input.json
uv run solo-manage apps execution --input-file /shared/runs/459/input.json
uv run solo-manage apps strategy --input-file /shared/runs/789/input.json
```

每个任务准备独立的 `environment/pyproject.toml` 和 `environment/uv.lock`。研究包及其 scheme 依赖由锁文件选择，任务环境不需要安装 solo-runtime。正式锁文件只能引用发布包、wheel 或固定 Git 版本，不能引用可变源码目录。

六个 app 分别对应同名 DolphinScheduler 工作流：`factor` 因子分析、`model` 策略建模、`optimize` 组合优化、`control` 订单风控、`execution` 算法下单、`strategy` 完整策略组装。命令入口位于 `manage/apps`，共享环境启动和产物校验位于 `utils/task.py`。

input.json 的 `kind` 必须与 app 同名：`factor`、`model`、`optimize`、`control`、`execution`、`strategy`。Scheme 根据 kind 调用各自的执行入口和参数模型。

报告类型独立保存在 run.json 的 `report_kind`，取自实际返回的报告对象。当前 factor 返回因子报告，其余入口暂时复用回测报告实现；将来替换某一入口的报告，不改变它的任务 kind 或工作流。

Runtime 随后读取以下启动字段，其余内容原样交给研究进程：

```json
{
  "environment": {"lockfile": "environment/uv.lock"},
  "output": "report"
}
```

相对路径以 input.json 所在目录为基准。完整因子、策略组装输入见 [factor.json](examples/factor.json)、[strategy.json](examples/strategy.json)，研究字段由任务所安装的 scheme 解释。

执行流程：

1. 执行 `uv sync --project <environment> --locked --no-editable --no-dev`。
2. 使用该环境的 `scheme run --input <input.json> --output <output>` 入口。不会退回使用 Worker 全局安装的命令。
3. 子进程标准输出、标准错误直接交给 DolphinScheduler；安装失败和研究失败均返回非零退出码。
4. 子进程成功退出后，核验 `run.json` 和它引用的所有报告文件。

协议版本 1 的完成清单字段：

| 字段 | 内容 |
|---|---|
| protocol | 固定为 1，与 scheme 包版本无关 |
| status | 成功时为 success |
| input_sha256 | 原始 input.json 文件的 SHA256 |
| lock_sha256 | 任务 uv.lock 的 SHA256 |
| input.kind | 本次任务类型，与 app 和工作流同名 |
| report_kind | 实际报告类型，由 Scheme 结果对象给出 |
| reports | 报告名称到输出目录内相对文件名的映射 |
| report_sha256 | 文件名到文件 SHA256 的映射 |

scheme 在所有报告保存完成后原子写入 `run.json`。缺少清单、缺失文件、哈希不符或文件越出输出目录都视为失败；已有完成清单的目录禁止重跑覆盖。清单可额外包含研究参数、实际包版本等信息，Runtime 不解释这些字段，也不规定具体报告表结构。

同一 Runtime 可以启动不同 scheme 主版本的任务，前提是它们实现相同 CLI 和完成清单协议。更改此进程边界协议才需要调整 Runtime。Jupyter 的研究 SDK 位于 `scheme.base`、`scheme.execute`、`scheme.data`，详见 [scheme README](https://gitee.com/genesis-quant/solo-algo-scheme#readme)。
