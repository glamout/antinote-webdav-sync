# Antinote WebDAV Sync

**[English](./README.md)** | 简体中文

[![Platform](https://img.shields.io/badge/platform-macOS%2014%2B-blue)](#)
[![Runtime](https://img.shields.io/badge/dependencies-zero%20(native%20C%20%2B%20Python%20stdlib)-brightgreen)](#)
[![License](https://img.shields.io/badge/license-MIT-S)](LICENSE)

一个专为 macOS 设计的超轻量级后台单向实时镜像服务，将 **[Antinote](https://antinote.io/)** 菜单栏便签中的纯文本笔记（新建、修改、改名、删除）实时同步为 **WebDAV Server** 指定目录下的独立 `.md` 文件（`1 条笔记 = 1 个 第一行文字.md`），两边状态严格保持一致。

## 核心特性

* **干净的 `第一行文字.md` 命名与 100% 纯文本保留**：
  * 自动提取笔记第一行非空文本作为文件名（自动过滤 `#` 标题前缀与非法路径字符；若多篇笔记首行同名，自动按 `第一行文字 (2).md` 编号消歧）。
  * 生成的 `.md` 文件 **100% 原样保留 Antinote 中的全部文字（包含第一行）**，不注入任何 YAML Frontmatter 属性。
* **智能过滤非目标笔记（兼容 Antinote 2.0）**：
  * 自动读取 SQLite + `.sqlite-wal` + `.sqlite-shm` 实时状态。
  * 默认自动过滤 [Antinote 2.0 内置的 9 个希腊神明常驻槽位笔记（Slotted Notes：`Aphrodite` ~ `Zeus`）](https://antinote.io/user-manual/slotted-notes)、新手引导教程笔记以及已移入回收站 [The Void](https://antinote.io/updates/antinote-2-0) 的软删除笔记。
* **远端 `.trash` 回收站保护与严格镜像对齐**：
  * 当在 Antinote 中删除笔记，或远端对账发现多余的孤儿 `.md` 文件时，自动通过 WebDAV `MOVE` 协议将其移入远端 `.trash` 子目录（若服务端不支持跨目录 `MOVE`，自动回退为 `PUT + DELETE`）。
  * 当在WebDAV端误删了仍在 Antinote 中存在的笔记时，下一次远端 `PROPFIND` 巡检会自动将其重新补传回 WebDAV。
* **单应用最小权限架构（`AntinoteWebDAVSync.app`）与极致低功耗**：
  * **无需给系统 `/usr/bin/python3` 开启完全磁盘访问权限**：由编译后的原生 C 守护程序 `AntinoteWebDAVSync.app`（常驻内存仅 **`~1.5 MB`**，空闲 CPU **`0.0%`**）直接以独立应用身份检测并复制沙盒容器数据库到普通目录 `mirror_db/`，再调用 Python 标准库执行增量同步。
  * **零第三方依赖**：仅使用 macOS 原生 `clang` 与内置 `python3` 标准库，无需安装 Homebrew 或任何 `pip` 包。

## 项目目录结构

```text
antinote-webdav-sync/
├── .gitignore                  # 忽略真实 config.json 与运行期缓存，防止密码泄露到 Git
├── LICENSE                     # MIT 开源许可证
├── README.md                   # 项目说明与配置文档
├── config.example.json         # 配置文件模板
├── install.sh                  # 一键编译、安装与升级脚本
├── uninstall.sh                # 一键卸载清理脚本
├── src/
│   ├── daemon.c                # 原生 C 容器数据库监听与镜像守护进程 (~1.5 MB RAM)
│   └── antinote_webdav_sync.py # 零依赖 Python WebDAV 单向增量镜像对账引擎
└── tests/
    └── test_webdav_sync.py     # 内置 Mock WebDAV Server 与 SQLite WAL 的自动化端到端测试套件
```

## 快速开始

### 1. 克隆仓库并填写配置

```bash
git clone https://github.com/<your-username>/antinote-webdav-sync.git
cd antinote-webdav-sync
cp config.example.json config.json
open -e config.json
```

在 `config.json` 中填入你的 WebDAV 服务器信息：

```json
{
  "webdav_url": "https://your-webdav-server.com/dav",
  "username": "your_username",
  "password": "your_password",
  "remote_folder": "/home/Obsidian Vault/Antinote",
  "trash_folder": ".trash",
  "use_trash_on_delete": true,
  "clean_orphan_files": true,
  "include_slotted": false,
  "include_archived": false,
  "poll_interval_seconds": 2,
  "audit_interval_seconds": 300,
  "timeout_seconds": 15,
  "verify_ssl": true,
  "antinote_db_path": ""
}
```
> **安全提示**：`config.json` 已加入 `.gitignore`，不会被 `git commit` 提交到远程仓库。

### 2. 运行一键安装脚本

```bash
bash install.sh
```

### 3. 授予 `AntinoteWebDAVSync.app` 单应用读取权限（仅首次需要）

由于 Antinote 的数据库位于 macOS 沙盒容器 `~/Library/Containers/com.chabomakers.Antinote/` 下：
1. 若运行 `install.sh` 后系统弹出 **“AntinoteWebDAVSync 想要访问其他 App 的数据”**，直接点击 **【允许】**。
2. 若未自动弹窗，打开 **系统设置 > 隐私与安全性 > 完全磁盘访问权限**，点击 `+` 号，按 `⌘ + ⇧ + G` 输入 `~/Applications/AntinoteWebDAVSync.app` 并开启开关（**仅需勾选该专属应用，无需给系统 `/usr/bin/python3` 开启权限**）。

## 配置参数说明与动态修改

安装完成后，唯一实际工作的运行目录为 **`~/Library/Application Support/AntinoteWebDAVSync/`**。你可以随时运行以下命令修改配置（保存后下一个检测周期自动生效，无需重启服务）：

```bash
open -e "$HOME/Library/Application Support/AntinoteWebDAVSync/config.json"
```

| 字段名称 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `webdav_url` | 必填 | WebDAV 服务器根地址（支持包含空格或中文路径，内部自动转义） |
| `username` / `password` | 必填 | WebDAV 账号与密码（或应用专用密码） |
| `remote_folder` | `"/Antinote"` | 远端同步的目标文件夹路径（不存在会自动通过 `MKCOL` 创建） |
| `trash_folder` | `".trash"` | 远端回收站目录。相对路径 `".trash"` 代表 `<remote_folder>/.trash` |
| `use_trash_on_delete` | `true` | `true` 时删除笔记移入远端 `.trash`；`false` 时直接永久 `DELETE` |
| `clean_orphan_files` | `true` | 是否自动清理远端目录中不属于当前 Antinote 笔记的多余 `.md` 文件 |
| `include_slotted` | `false` | 是否同步 Antinote 2.0 的 9 个希腊神明常驻槽位笔记（默认 `false` 过滤） |
| `include_archived` | `false` | 是否同步 Antinote 中已归档（Archived）的隐藏笔记 |
| `poll_interval_seconds` | `2` | 本地数据库变化检测间隔（秒），默认 2 秒内响应新建/修改/删除 |
| `audit_interval_seconds` | `300` | 远端 WebDAV `PROPFIND` 全量目录对账周期（秒），默认每 5 分钟核对一次 |
| `timeout_seconds` | `15` | 单次 WebDAV HTTP 请求超时时间（秒） |
| `verify_ssl` | `true` | 是否校验 HTTPS 证书（自建 NAS 自签名证书可设为 `false`） |
| `antinote_db_path` | `""` | 留空时在安装阶段自动探测并填入 Antinote `.sqlite` 绝对路径 |

## 常用运维与测试命令

* **查看后台运行日志与错误日志**：
  ```bash
  tail -n 30 /tmp/antinote-webdav-sync.log /tmp/antinote-webdav-sync.err
  ```
* **查看后台进程内存与 CPU 占用**：
  ```bash
  ps -o pid,%cpu,rss,comm -p $(pgrep -f AntinoteWebDAVSync) | awk 'NR==1{print $0, "MEM_MB"} NR>1{printf "%s %s %s %s (%.2f MB)
", $1, $2, $3, $4, $3/1024}'
  ```
* **运行自动化端到端测试套件**：
  ```bash
  python3 tests/test_webdav_sync.py
  ```
* **一键卸载服务**：
  ```bash
  bash "$HOME/Library/Application Support/AntinoteWebDAVSync/uninstall.sh"
  ```
