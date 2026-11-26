# Windows 内网部署指南

[English](windows-deployment.md)

在一台常开的 x64 Windows 主机上集中部署 Silver Test Platform。同事通过浏览器访问主机的内网地址。主机运行 Web 服务、一个同时处理两条队列的 Huey Worker，以及可选的单进程实时协作服务。PostgreSQL 可以装在这台主机上，也可以使用内网数据库服务器。

## 获取发布包

仓库的 **Windows release** GitHub Actions 工作流会先运行 PostgreSQL 和前端 CI，再构建前端资源，并把应用和 Windows Python wheels 打成离线包。可以在 GitHub Actions 里选择分支并手动运行工作流，成功后在运行记录的 **Artifacts** 下载 `windows-x64` 预览产物。推送 `v主版本.次版本.补丁版本` 格式的 Git 标签后，工作流会在 GitHub Releases 发布 ZIP 和 `.sha256` 校验文件。标签也支持预发布后缀，例如 `v2.13.0-rc.1`。

把 ZIP 和同名的 `.zip.sha256` 一起复制到目标主机，或放到内网共享目录分发。目标主机另外安装 Python、PostgreSQL、WinSW 和实际执行所需的 Synopsys Silver，使用单位批准的安装包。

## 首次部署

### 1. 准备运行环境

- 在目标主机上为所有用户安装 **x64 CPython 3.10.18**，让 Windows 服务账号能够读取 Python 安装目录。如使用已批准的 uv，可以在有网环境执行以下命令。安装后记下实际的 `python.exe` 路径。[uv 安装 Python 文档](https://docs.astral.sh/uv/guides/install-python/)

  ```powershell
  uv python install 3.10.18 --install-dir C:\ProgramData\SilverPython --no-bin
  ```

- 准备 PostgreSQL 和客户端工具。PostgreSQL 16 与项目 CI 使用的版本一致。创建一个由应用数据库角色拥有的专用空库，把 `pg_dump.exe` 和 `pg_restore.exe` 所在目录加入管理员运行部署脚本时使用的 `PATH`。
- 准备 WinSW 2.x，例如 `WinSW.NET461.exe`。如需运行真实测试，在目标主机上安装 Synopsys Silver 并配置许可证。[WinSW 2.12.0 发布页](https://github.com/winsw/winsw/releases/tag/v2.12.0)

### 2. 校验发布包并初始化部署目录

用管理员身份打开 Windows PowerShell。把示例中的 ZIP 和 WinSW 路径换成实际路径，选择一个空的临时解压目录。先从可信发布渠道取得 `.sha256` 文件，校验通过后再执行包内的脚本。

```powershell
$package = 'C:\Install\silver-test-platform-v2.13.0-windows-x64.zip'
$setupDir = 'C:\Install\silver-setup'
$expected = ((Get-Content -LiteralPath "$package.sha256" -Raw).Trim() -split '\s+')[0]
$actual = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash
if ($actual -ine $expected) { throw 'SHA-256 校验失败，请重新获取发布包。' }

Expand-Archive -LiteralPath $package -DestinationPath $setupDir
Set-Location $setupDir
.\deployment\Manage-Silver.ps1 Initialize -WinSWPath 'C:\Install\WinSW.NET461.exe'
```

默认部署目录为 `C:\ProgramData\SilverTestPlatform`。如果要自定义目录，选择本机 NTFS 卷上的专用空目录，并在后续每次调用脚本时都传入相同的 `-Root`。

### 3. 配置应用并安装服务

编辑部署目录下的 `config\.env`。

| 配置项 | 填写方式 |
| --- | --- |
| `DATABASE_URL` | 填写应用的 PostgreSQL 连接地址。应用和 Huey 使用同一个数据库。`HUEY_DATABASE_URL` 可留空，显式填写时须与 `DATABASE_URL` 完全相同。 |
| `SECRET_KEY` | 填写本机独用、至少 32 个字符的随机密钥。 |
| `LM_ADMIN_PASSWORD` | 设置初始系统管理员密码。 |
| `RUNNER_BACKEND` | 首次验证可设为 `mock`。执行真实测试时设为 `silver` 并填写目标机的 `SILVER_HOME`。 |
| `HOST`、`PORT` | 默认 Web 端口为 `8080`。按实际监听端口给需要访问的内网客户端开放 Windows 防火墙入站规则。 |

把 `$python` 换成实际的 CPython 3.10.18 可执行文件路径，执行安装。需要实时协作时，在首次 `Install` 命令末尾加 `-Collab`，并配置 `COLLAB_WS_URL` 或 WebSocket 反向代理。后续升级沿用当前的协作设置。[实时协作配置](https://github.com/wyq0x55/slivertesttool/blob/main/README.md#real-time-collaboration-optional-third-process)

```powershell
$python = 'C:\Program Files\Python310\python.exe'
.\deployment\Manage-Silver.ps1 Install -Package $package -Python $python
.\deployment\Manage-Silver.ps1 Status
```

### 4. 从客户端验收

在另一台电脑上打开 `http://部署主机地址:8080`，登录后提交一条有代表性的测试，确认能执行并查看结果。使用真实 Silver 时，在无人交互登录的条件下，用实际的服务账号验证 Silver 启动、模型读取和许可证获取。

WinSW 默认使用 `NT AUTHORITY\LocalService`。该账号需要能够访问 Python、Silver、许可证和部署目录外的模型输入。如果需要专用服务账号，在 Windows「服务」管理器的「登录」选项卡中更改对应服务，并授予该账号配置读取、数据和日志目录写入，以及运行 Silver 所需的访问权限。脚本只在创建服务时设置默认账号。

## 部署目录与日常管理

| 路径 | 用途 |
| --- | --- |
| `config\.env` | 数据库连接、密钥和运行配置。 |
| `releases\` | 各版本的应用文件和独立 Python 虚拟环境。 |
| `data\instance`、`data\uploads`、`data\reports` | 模型、工作区、上传文件和报告等持久化数据。 |
| `logs\` | Web、Worker、协作及命令行日志。 |
| `backups\` | 升级前的数据库、业务文件和配置快照。 |

准备维护时，先让后台任务完成，再执行 `Stop`。用 `Start` 启动当前版本，用 `Status` 查看当前版本和服务状态。`Start` 会直接启动已安装的版本。把快照另存一份到受管理员权限保护的独立磁盘或内网服务器。

```powershell
.\deployment\Manage-Silver.ps1 Status
.\deployment\Manage-Silver.ps1 Stop
.\deployment\Manage-Silver.ps1 Start
```

## 迁移已有部署

在旧主机安排维护窗口，等后台任务完成后停止所有写入进程。备份应用数据库，并复制实例、上传和报告目录，包含已保存的模型版本和工作区。将数据库恢复到目标端的专用空库，把文件复制到新部署对应的 `data\` 子目录，再执行首次 `Install`。

旧记录中可能保存了绝对路径。尽量保留原路径，或在切换前检查并重新登记受影响的模型和输入。验收时抽查历史报告、模型快照，并运行一条新测试。新旧主机使用同一个数据库时，在切换期间保持旧主机的应用进程停止。

## 升级及中断恢复

安排维护窗口，让用户结束任务提交并关闭实时协作编辑页。等 Silver 运行、单元测试批次、AI 生成及导入导出任务结束。参照首次部署的校验和解压步骤处理新版 ZIP，然后进入新版临时解压目录，设置 `$package` 和 `$python` 为实际路径，运行以下命令。

```powershell
.\deployment\Manage-Silver.ps1 Update -Package $package -Python $python
.\deployment\Manage-Silver.ps1 Status
```

脚本会先准备新版离线环境，再停止 Web 和协作服务，复查后台任务已结束后停止 Worker。随后备份 PostgreSQL、业务文件和 `.env`，输出快照目录，执行一次 `manage.py bootstrap`，启动新版服务并检查 Web 版本、数据库连通性及已启用的协作端点。使用真实 Silver 时，再跑一条代表性测试后开放给用户。

如果升级中断，且 `Status` 显示有未完成的部署，查看 `logs\`，修复问题后执行 `Resume`。保留需要回退的版本目录及其对应快照。

```powershell
.\deployment\Manage-Silver.ps1 Resume
.\deployment\Manage-Silver.ps1 Status
```

## 回退到升级前快照

1. 如果新版仍在运行，等任务结束后执行 `Stop`，确认所有平台服务均已停止。保留新版数据库和快照之后写入的数据，便于后续核对。
2. 创建一个由应用数据库角色拥有的**全新空 PostgreSQL 数据库**。将 `backups\<快照目录>\.env` 复制回 `config\.env`，再把 `DATABASE_URL` 改为新库地址。如果设置了 `HUEY_DATABASE_URL`，清空它或改为完全相同的连接地址。
3. 从 `Status` 输出或 `backups\` 中找到快照目录名，替换示例值后执行回退。

   ```powershell
   .\deployment\Manage-Silver.ps1 Rollback -Backup '20261126-123456-a1b2c3d4'
   .\deployment\Manage-Silver.ps1 Status
   ```

脚本会在一个事务中恢复数据库，还原配套业务文件，把较新的文件保存在 `preserved-data-*` 目录，并启动快照记录的程序版本。恢复过程若中断，使用另一个全新的空数据库重试 `Rollback`。数据已恢复、只有服务启动失败时，修复后执行 `Resume`。确认可以登录、查看数据并完成一条测试，再恢复用户访问。
