# Solo Runtime

Runtime 是仅使用 Python 标准库的任务启动器，不安装或导入 scheme、backtest、数据源或报告库。

```shell
uv sync
uv run solo-manage run --input-file /shared/tasks/123/input.json
```

每个任务准备独立的 `environment/pyproject.toml` 和 `environment/uv.lock`。研究包及其 scheme 依赖由锁文件选择，任务环境不需要安装 solo-runtime。正式锁文件只能引用发布包、wheel 或固定 Git 版本，不能引用可变源码目录。

Runtime 只读取 input.json 的两个字段，其他内容原样交给研究进程：

```json
{
  "environment": {"lockfile": "environment/uv.lock"},
  "output": "report"
}
```

相对路径以 input.json 所在目录为基准。完整因子、回测输入见 [factor.json](examples/factor.json)、[backtest.json](examples/backtest.json)，研究字段由任务所安装的 scheme 解释。

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
| reports | 报告名称到输出目录内相对文件名的映射 |
| report_sha256 | 文件名到文件 SHA256 的映射 |

scheme 在所有报告保存完成后原子写入 `run.json`。缺少清单、缺失文件、哈希不符或文件越出输出目录都视为失败；已有完成清单的目录禁止重跑覆盖。清单可额外包含研究参数、实际包版本等信息，Runtime 不解释这些字段，也不规定具体报告表结构。

同一 Runtime 可以启动不同 scheme 主版本的任务，前提是它们实现相同 CLI 和完成清单协议。更改此进程边界协议才需要调整 Runtime。Jupyter 的研究 SDK 已迁入 `scheme.apps`、`scheme.data`，详见 [scheme README](https://github.com/Genesis-Quant/solo-algo-scheme#readme)。
