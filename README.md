# NAS 多网盘下载器

多网盘下载器是面向飞牛 fnOS 的原生第三方云盘下载工具。它把百度网盘和夸克网盘的目录浏览、文件搜索、下载任务、速度控制、磁盘保护与运行诊断集中在同一个本地 Web UI 中，并将文件直接保存到安装向导选择的 NAS 目录。应用自带对应 CPU 架构的 Linux Python 运行时和全部程序依赖，不调用 Docker，不拉取容器镜像，也不会在安装阶段在线编译环境。

当前安装包目标：

- CPU：x86_64 / amd64 或 ARM64 / aarch64
- 已核对系统：x86_64 fnOS 1.1.3107；ARM64 包已完成静态架构与安装包测试
- Web 端口：8686
- x86_64 安装包：`clouddl_x86.fpk`
- ARM64 安装包：`clouddl_arm64.fpk`
- 运行方式：fnOS 原生后台服务，无需 Docker

## 主要功能

- 百度网盘：BDUSS + STOKEN 登录、目录浏览、搜索、单文件及文件夹递归下载
- 夸克网盘：Cookie 登录、目录浏览、搜索、单文件及文件夹递归下载
- 文件夹下载保持原有多级目录结构
- 标准 HTTP Range 分片下载，分片大小可在 1–50MB 调整
- 小于 100MB 的文件自动使用 1MB 分片，大于 1GB 的文件至少使用 10MB 分片
- 默认每个文件 16 个连接，可在 1–64 之间调整
- 所有任务默认合计最多 30 条分片请求，可在 1–200 之间调整
- 默认同时下载 5 个任务，可在 1–50 之间调整
- 可选本机 Aria2 JSON-RPC 后端；服务不可用时自动回退到内置下载器
- 下载任务实时显示速度、连接数和预计剩余时间
- 全局下载限速，0 表示不限速
- 下载时段控制，支持跨午夜
- 同名文件支持报错、重命名、覆盖和跳过
- 默认保留 50GB 可用磁盘空间
- 日志查看、轮转、下载、清空和脱敏诊断包
- 设置按“账号与凭据、下载策略、日志与诊断”三个顶部页面分类
- 安装后可在“设置 > 下载策略”中更改下载保存目录，无需重装或重启
- 自动浅色/深色主题、桌面/平板/手机响应式界面
- 每个浏览器会话一次的 Web UI 启动动画
- 首次使用新手引导和强制免责声明
- 百度网盘首次凭据配置引导，完成后不再弹出，卸载重装后重新显示

所有普通设置都保存在应用配置目录，并立即作用于后续下载。敏感凭据使用独立文件保存；Aria2 RPC 密钥只写入、不通过 Web API 回显，诊断包也不会包含凭据和下载链接。

下载目录既可在安装向导中首次选择，也可在安装后的“设置 > 下载策略”中修改。新路径必须是 fnOS 上可写的绝对路径；保存后立即用于新任务，修改前已经开始的任务仍写入原目录。

## 安装

前提：

- x86_64 / amd64 或 ARM64 / aarch64 飞牛 NAS
- fnOS 1.1.3107 或兼容版本

步骤：

1. 在 fnOS 应用中心选择“手动安装”。
2. x86_64 设备上传 `clouddl_x86.fpk`；ARM64 设备上传 `clouddl_arm64.fpk`。
3. 按安装向导选择下载目录。
4. 安装后从桌面图标启动，或访问 `http://<NAS_IP>:8686`。
5. 首次进入完成五步新手引导，阅读并勾选免责声明。
6. 打开左下角“设置”，录入百度或夸克凭据。

FPK 已针对 fnOS 安装要求处理：

- 包内容直接位于归档根目录，没有多余外层文件夹
- Linux 生命周期脚本为 LF 换行并带可执行权限
- 安装向导和配置文件是有效 JSON
- 自带 Python 3.11 原生运行时和对应架构的 Linux 依赖
- 不包含 Docker 项目、Dockerfile 或在线依赖安装步骤
- 配置、凭据和日志写入 fnOS 应用数据目录
- 下载文件写入安装向导中选择的绝对路径

## 获取登录凭据

### 百度网盘

1. 在浏览器登录 `https://pan.baidu.com`。
2. 打开开发者工具的 Network 面板并刷新页面。
3. 查看任意百度网盘请求的 Cookie。
4. 分别复制 `BDUSS` 和 `STOKEN` 的值。
5. 在多网盘下载器的“设置 > 账号与凭据”中验证并保存。

### 夸克网盘

1. 在浏览器登录 `https://pan.quark.cn`。
2. 打开开发者工具的 Network 面板。
3. 查看任意夸克网盘请求并复制完整 Cookie。
4. 在多网盘下载器的“设置 > 账号与凭据”中验证并保存。

凭据具有账号访问能力，只应在可信的 NAS 和浏览器中录入。

## 默认下载策略

| 设置 | 默认值 |
|---|---:|
| 同名文件 | 无法确认相同则报错 |
| 磁盘保留空间 | 50GB |
| 总下载限速 | 0，不限速 |
| 单文件连接数 | 16 |
| 分片大小 | 5MB |
| 全局分片请求上限 | 30 |
| 下载并发数 | 5 |
| 指定下载时段 | 关闭 |
| 日志级别 | INFO |
| 日志保留天数 | 7 |
| 单个日志上限 | 10MB |

同名文件默认策略只在远端提供可靠哈希且与本地文件一致时直接完成；无法确认时保留本地文件，并将新任务标记为失败。

可用空间低于“当前文件剩余大小 + 保留空间”时，任务进入磁盘空间暂停状态；空间恢复后继续。

Aria2 集成为可选功能，不随 FPK 捆绑 Aria2。当前只允许连接 NAS 本机的 `localhost`、`127.0.0.0/8` 或 `::1` RPC 地址，避免将下载链接和 RPC 密钥发送到远程主机。运行 Aria2 的系统账号还必须拥有所选下载目录的读写权限。

## 数据与隐私

- 应用配置目录：设置、加密边界内的登录凭据、免责声明状态、任务信息和日志
- 用户选择的下载目录：已完成文件与下载过程中的临时文件
- 应用不会将凭据上传到开发者服务器
- Web API 不回显 Cookie、BDUSS 或 STOKEN 原文
- 日志和诊断包会过滤凭据、Cookie、签名参数与下载链接
- 卸载应用不会主动删除用户已下载的文件

应用以 fnOS 原生后台进程运行，监听 NAS 的 `8686` 端口。启用、停用和状态检查由 `cmd/main` 维护 PID 文件并发送标准 `TERM` 信号完成，不依赖 Docker 服务。

## 项目结构

```text
cloud-downloader/
├── manifest
├── ICON.PNG
├── ICON_256.PNG
├── cmd/
├── config/
├── wizard/
├── app/
│   ├── ui/
│   ├── runtime/
│   │   └── python/              # 默认 Linux x86_64 Python 3.11 运行时
│   └── service/
│       ├── vendor/              # 默认已预装 Linux x86_64 Python 依赖
│       └── src/
│           ├── app.py
│           ├── aria2_rpc.py
│           ├── config_store.py
│           ├── onboarding.py
│           ├── downloader.py
│           ├── diagnostics.py
│           └── static/
├── tests/
├── build_fpk.py
├── build_arm64_fpk.py
├── clouddl_x86.fpk
└── clouddl_arm64.fpk
```

## 本地验证与打包

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe build_fpk.py
.\.venv\Scripts\python.exe -m unittest -v tests.test_fpk_package

# ARM64 构建
python build_arm64_fpk.py
$env:FPK_PATH = (Resolve-Path ".\clouddl_arm64.fpk").Path
$env:EXPECTED_ARCH = "aarch64"
python -m unittest -v tests.test_fpk_package
```

`build_fpk.py` 调用飞牛官方 `fnpack` 进行校验和打包，生成符合 fnOS 规范、外层包含 `app.tgz` 的 FPK 安装包。

`build_arm64_fpk.py` 在隔离暂存目录中将运行时替换为官方 `python-build-standalone` 的 CPython 3.11 ARM64 构建，将 `pydantic-core` 替换为 PyPI manylinux2014 aarch64 轮，并将 fnOS manifest 架构标识设为 `aarch64`。脚本会检查包内所有 ELF 均为 AArch64。构建所需文件放在 `.arm64-build/downloads/`，文件名和官方 SHA-256 固定在脚本中。

安装包不在 NAS 上执行 Dockerfile、`apt-get`、在线 `pip install` 或镜像拉取。Python 运行时与依赖全部包含在 FPK 中。

## 免责声明

本应用不是百度网盘、夸克网盘或飞牛 fnOS 官方产品，使用的是第三方非官方接口。接口、限速、风控和账号政策可能变化，并可能造成登录失效、下载失败或账号限制。用户必须确保下载和使用的内容具有合法授权，并自行承担凭据、账号、数据、设备和网络风险。
