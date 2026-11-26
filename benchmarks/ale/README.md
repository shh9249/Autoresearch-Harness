# AHC026 任务适配器（Windows native）

2026-11-25。范围：**候选源码 → 编译/检查 → 执行 → 官方评分 → 结构化结果**。只支持 AHC026；025/027 返回 unsupported_task。无模型API、算法切换、修复、重试、研究控制器或预算晋升政策。

## 依赖与上游

- Python 3.12+，仅标准库；无需安装当前因Cairo而无法导入的ALE Python包。
- 已核验的官方 AHC026 Windows gen.exe / vis.exe：`bench_setup_2026-11-25/ale/native/ahc026/tools_x86_64-pc-windows-gnu/`。
- 官方工具源： https://img.atcoder.jp/ahc026/lPQezTZx_windows.zip ，评分器不是自行移植。
- C++：共享 `bench_setup_2026-11-25/tools/llvm-mingw-20261117-ucrt-x86_64/bin/clang++.exe`，`-std=c++17 -O2`。
- 工具来源与原始任务包见 `bench_setup_2026-11-25/ale/README.md`，官方仓库及工具未改动。
- 核心逻辑从文件祖先定位工作区，工具路径可覆盖；没有硬编码用户名绝对路径。

## 输入契约

Python调用 `adapter.evaluate(request_dict)`；CLI为 `cli.py --request request.json`。

```json
{
  "task_id": "ahc026",
  "candidate_id": "candidate_001",
  "source_path": "autoresearch_rebuild/benchmarks/ale/samples/valid.cpp",
  "language": "cpp",
  "public_seed": 0,
  "config": {"run_timeout_seconds": 5}
}
```

- language仅支持 `cpp` / `python`。Python先独立进行语法编译检查，再运行候选。
- 选择 **public_seed**（0–49，缺省0），或提供 **input_path** 与可选 instance_id；二者不能同时提供。
- 显式输入必须符合AHC026 N=200、M=10、箱号1–200排列；适配器不会生成/读取私有seed。
- 相对路径按调用时工作目录解析；建议调度器传绝对路径。
- 配置可覆盖：`compiler`、`python`、`generator`、`scorer`、`run_root`，以及 `generate_timeout_seconds`（20）、`compile_timeout_seconds`（60）、`run_timeout_seconds`（5）、`score_timeout_seconds`（20）、`output_limit_bytes`（每阶段每输出流2MiB）、`source_limit_bytes`（1MiB）。
- 工作目录每次分配UUID，同candidate_id并发调用也互不覆盖。源文件复制进目录后执行；stdin、stdout、stderr分别记录。

## 输出契约与失败

完整结构见 `result.schema.json`。每次写入独立 `result.json`，返回同一个字典。关键字段：

- candidate/task/instance/seed/run标识；源码与输入SHA256；原始整数score、`score_direction=maximize`。
- 当前stage，分阶段command、exit_code、elapsed_seconds、stdout/stderr路径、超时和输出限额标记。
- artifacts、事件列表、toolchain路径及官方评分/生成器hash，错误code与message。
- 总耗时、开始/完成时间；无自动重试；适配器明确不决定B/T消耗。

| status | 含义 |
|---|---|
| success | 官方评分有效，score为正整数 |
| compile_error | C++编译或Python语法检查非零退出 |
| runtime_error | 候选运行非零退出 |
| timeout | 候选运行/编译超过设定wall-time；stage区分 |
| invalid_output | 官方评分器判无效，或候选修改了保存的输入 |
| output_limit_exceeded | 候选/编译stdout或stderr超过上限 |
| infrastructure_error | 缺工具、启动失败、生成/评分超时/异常、捕获或清理失败 |
| request_error | 请求、语言、源文件或显式实例不合法 |
| unsupported_task | 不是AHC026 |

**所有非success结果的score为null，不能默认为0分。** 官方vis对于无效输出可能正常退出并打印Score=0；本适配器保留该值在 official_reported_score，但状态仍为invalid_output、score仍为空。失败是否计入候选预算、是否重跑、如何晋升由上层协议决定。

CLI成功返回0，已执行但失败的结果返回2；stdout输出完整JSON。读取请求JSON失败属于CLI入参错误，尚未进入一次评测。

## 实际运行命令

从工作区根目录，在新的PowerShell执行，无须激活环境：

```powershell
& '.\bench_setup_2026-11-25\tools\python\cpython-3.12.16-windows-x86_64-none\python.exe' '.\autoresearch_rebuild\benchmarks\ale\cli.py' --request '.\autoresearch_rebuild\benchmarks\ale\request.example.json'
```

真实集成测试（约十几秒，运行手写、已检查候选，不调用API）：

```powershell
& '.\bench_setup_2026-11-25\tools\python\cpython-3.12.16-windows-x86_64-none\python.exe' '.\autoresearch_rebuild\benchmarks\ale\tests\run_smoke.py'
```

## 已验证

见 `tests/smoke_results.json` 与其引用的各个 result.json：

- C++有效候选与等价Python候选，public seed0都经官方评分得到 **7945**（越高越好）。只是接口验收，不是优化成绩。
- 编译错误、运行错误、运行超时、非法输出、输出超限、缺评分器、缺编译器均返回预期分类和null score。
- 同candidate_id两次并发执行，独立目录、同样有效分数。
- 显式输入模式有效；AHC025请求明确拒绝。
- 超时候选派生一个sleep子进程；Windows JobObject清理后确认该子进程已退出。

## 隔离与限制

- 隔离的是工作目录与结果文件；**不是操作系统安全沙箱**，没有文件/网络访问限制或候选进程内存硬限制。只用于已审查候选的本机验证。
- 每个子进程启动后立即尝试加入Windows kill-on-close JobObject，超时/结束时清理本次job；失败时按本次PID执行 `taskkill /T /F`，不按进程名结束其他任务。记录实际采用方式。
- JobObject在启动后挂接，存在极短挂接窗口；fallback对于脱离进程树的进程不是完整保证。正式不受信任候选执行仍需受控容器或其它隔离器。
- stdout/stderr有界捕获，单个源文件/输入有限额；候选任意创建其他文件的总磁盘占用目前没有硬限制。
- 默认5秒是本机wall-time保护，不等于题面官方2秒计时环境；未实现Docker内存、CPU配额或官方排行榜相对评分。
- 未验证Linux容器路径、编译器等效性或跨机器数值/计时一致性；当前是Windows native适配器。不得称为官方Docker全栈复现。
- 后续模型调用统一thinking disabled；本适配器没有模型调用，因此不设置或消费API配置。
