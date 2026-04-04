# BluRayRemuxKit

🚀 **[懒人导航：太长不看？点击直接跳转到一键运行命令](#不使用-docker-compose)**

自动化将蓝光原盘（BDMV）或 ISO 镜像批量 Remux 为 MKV，支持智能正片识别、BDInfo 深度解析与重构、以及交互式轨道编辑。

---

## 参数说明

| 参数 | 必选 | 默认值 | 说明 |
|---|---|---|---|
| `-i`, `--input` | **是** | — | 包含蓝光原盘（文件夹）或 ISO 文件的根目录，支持递归扫描多层子目录 |
| `-o`, `--output` | 否 | `./output` | 输出 MKV 文件的保存目录 |
| `--bdinfo-dir` | 否 | — | 外部 BDInfo 文本的统一存放目录 |
| `--skip-interactive` | 否 | — | 开启全自动静默模式，跳过所有手动确认和轨道编辑环节 |
| `--continue-on-error`| 否 | — | 容错模式，某个原盘处理报错时自动跳过并继续处理下一个 |
| `--commentary` | 否 | `keep` | 导评轨道处理策略（`keep` 保留 / `drop` 剔除 / `ask` 询问） |
| `--best-audio` | 否 | `no` | 最高规格音轨精简策略（`no` 默认 / `yes` 仅保留最高规格 / `ask` 询问） |
| `--simplify-subs` | 否 | `yes` | 外语字幕精简策略（`yes` 默认精简外语 / `no` 全部保留 / `ask` 询问） |

> **💡 路径参数提示：**
> 在使用 Docker 容器时，参数中的 `-i /input` 和 `-o /output` **通常不需要修改**，因为它们指向的是容器内部的固定绝对路径。只需要在挂载配置（`volumes`）中修改对应的宿主机（本地）路径即可。

---

## BDInfo 匹配与输入

### 匹配规则（按优先级）
1. **同名优先** — 在原盘同目录下查找 `{原盘名}.txt` 或 `{原盘名}_bdinfo.txt`
2. **向上回溯** — 从原盘所在目录开始，向上最多回溯 3 层查找 `bdinfo.txt`
3. **统一目录** — 在 `--bdinfo-dir` 参数指定的目录下查找同名 txt
4. **控制台粘贴兜底** — 如果以上位置都没找到 BDInfo，脚本会在交互终端中提示你直接粘贴完整 BDInfo 文本，并缓存成临时 txt 继续处理

### 推荐方式：直接在控制台粘贴
正常情况下不再需要手动新建 `txt` 文件。

使用方式：
1. 直接运行脚本或容器命令
2. 如果脚本没有在本地找到匹配的 BDInfo 文本，会在控制台提示你粘贴完整 BDInfo
3. 粘贴完成后，单独输入 `EOF` 结束
4. 脚本会自动缓存为临时 txt，并继续后续处理

### 可选方式：手动新建 txt
如果你希望复用同一份 BDInfo，或者想避免重复粘贴，也仍然可以手动新建 txt 文件。

利用终端的 `EOF` 语法，可以一步到位新建 txt 文件并写入 BDInfo。

**方式一：在当前目录下创建**
如果已经 `cd` 进入了原盘所在目录，直接执行：
```bash
cat << 'EOF' > "原盘名称.txt"
[在这里粘贴 BDInfo 扫描文本内容，支持直接粘贴多行]
[支持多行...]
EOF
```

**方式二：指定目标路径创建**
如果不想切换目录，可以直接指定绝对或相对路径（⚠️ **注意**：目标路径的文件夹必须已经存在，`cat` 不会自动创建目录）：
```bash
cat << 'EOF' > "/volume1/Media/BluRays/原盘名称.txt"
[在这里粘贴的 BDInfo 扫描文本内容，支持直接粘贴多行]
EOF
```

---

## Docker 使用指南

镜像已发布至 GHCR，支持 `linux/amd64` 和 `linux/arm64` 架构：

```bash
docker pull ghcr.io/chen8945/bluray-remuxkit:latest
```

### 前提条件
- 宿主机必须是 **Linux**。
- 已安装 Docker。
- 如需容器内挂载 ISO，宿主机需存在 `/dev/loop-control` 及可用的 loop 设备。

> **💡 提示**：Windows / macOS 用户建议直接跳转查看下方的 [本地运行](#本地运行) 使用单独的 Python 脚本。

### 不使用 Docker Compose

如果不想繁琐地配置 Compose 文件夹，直接在原盘目录下运行即可，这是最推荐的方式。

**操作流：**
1. 在终端中 `cd` 进入包含蓝光原盘的目录。
2. 直接粘贴执行以下命令：
3. 如果运行过程中提示缺少 BDInfo，就把完整 BDInfo 文本粘贴进控制台，并单独输入 `EOF` 结束。

```bash
docker run --rm -it \
  --network none \
  --init \
  --security-opt apparmor:unconfined \
  --security-opt seccomp:unconfined \
  --cap-add SYS_ADMIN \
  --device /dev/loop-control:/dev/loop-control \
  --device /dev/loop0:/dev/loop0 \
  --device /dev/loop1:/dev/loop1 \
  --device /dev/loop2:/dev/loop2 \
  --device /dev/loop3:/dev/loop3 \
  --device /dev/loop4:/dev/loop4 \
  --device /dev/loop5:/dev/loop5 \
  --tmpfs /tmp:exec \
  -v "$PWD":/input:ro \
  -v "$PWD/../Remux_Output":/output \
  ghcr.io/chen8945/bluray-remuxkit:latest \
  -i /input \
  -o /output \
  --continue-on-error \
  --commentary drop \
  --best-audio yes \
  --simplify-subs yes
```
> **巧妙的路径隔离**：通过 `-v "$PWD/../Remux_Output":/output`，输出文件会被自动生成在当前目录的**上一级目录**中。

### 使用 Docker Compose

如果更喜欢统一部署和管理，可以使用 Compose 模式。

**1. 准备目录结构：**
```text
BluRayRemuxKit/
├── docker-compose.yaml
├── input/          ← 放入蓝光原盘或 ISO
└── output/         ← Remux 输出目录
```

**2. 🌟 推荐运行参数：**
自动剔除导评、保留最高规格音轨并精简外语字幕、无报错中断：
```bash
docker compose run --rm remuxkit \
  -i /input \
  -o /output \
  --continue-on-error \
  --commentary drop \
  --best-audio yes \
  --simplify-subs yes
```

**3. 交互模式**（保留所有的手动确认和轨道编辑环节）：
```bash
docker compose run --rm remuxkit -i /input -o /output
```

**4. 全自动模式**：
```bash
docker compose run --rm remuxkit -i /input -o /output --skip-interactive
```

**5. 极致精简模式**（全自动+最高规格+无报错中断）：
```bash
docker compose run --rm remuxkit \
  -i /input \
  -o /output \
  --skip-interactive \
  --commentary drop \
  --best-audio yes \
  --simplify-subs yes \
  --continue-on-error
```

> ⚠️ **注意**：请务必使用 `docker compose run --rm` 而非 `docker compose up`，因为本容器设计为一次性处理任务，`--rm` 能确保任务结束后自动清理容器。

### ISO 挂载排错说明
容器内 ISO 挂载需要特定的高级权限（`docker-compose.yaml` 及上方单行命令已预置）：
- root 用户运行（`user: 0:0`）
- `SYS_ADMIN` capability
- loop 设备映射
- `apparmor:unconfined` + `seccomp:unconfined`

如果 ISO 挂载失败，请检查：
1. 宿主机物理机是否存在 `/dev/loop-control` 与若干 `/dev/loopN` 设备。
2. Docker 服务是否允许容器访问这些底层设备。
3. 系统中是否有其他进程（如其他挂载任务）占满了现有的 loop 设备。

---

## 本地运行

> **⚠️ 强烈建议**：对于 Windows 和 macOS 用户，直接运行本地 Python 脚本效率极高。这不仅能直接调用系统本地的 `mkvmerge` 进行满速混流，更能彻底避开 Docker 在跨平台环境下的 loop 设备挂载限制与繁琐的权限配置。

### 依赖准备
- **环境**：Python 3.10+
- **核心工具**：
  - [mkvmerge](https://mkvtoolnix.download/)（MKVToolNix）
  - [ffprobe](https://ffmpeg.org/)（FFprobe，可单独安装，不要求完整 FFmpeg）
- **Python 库**：`rich`、`pycountry`
- **可选增强输入依赖**：`prompt_toolkit`
  - 用于改善 SSH / Linux / Windows 下的交互输入体验，支持上下键历史、左右移动与更自然的命令行编辑
  - 未安装时，脚本会自动回退到原生 `input()`，不影响基本功能

### 安装依赖
```bash
pip install -r requirements.txt
```

`requirements.txt` 已包含 `prompt_toolkit`，安装后可直接获得增强交互输入体验；即使缺少该库，脚本仍会自动回退到原生 `input()`。

### 执行命令
```bash
python bluray_remux.py -i /path/to/BluRays -o /output
```
