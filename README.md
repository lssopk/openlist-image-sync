# OpenList Image Sync

一个可部署在 Debian/Ubuntu 上的 Web 面板：配置一个 OpenList，再配置多个图片 API，服务会按各自的间隔抓取图片、去重并上传到 OpenList。

适合把多个公开图片接口、JSON 图片接口或 HTML 图片页面，统一保存到自己的 OpenList。

## 功能

- Web 登录保护，默认只开放一个管理面板。
- 保存 OpenList 地址、用户名、密码和默认写入目录。
- OpenList 密码使用 `APP_SECRET` 加密后保存到 SQLite。
- 支持同时添加多个图片来源，每个来源可独立设置抓取间隔。
- 自动识别以下来源：
  - 直接返回 `image/jpeg`、`image/png` 等图片响应的 API；
  - JSON 中的图片 URL；
  - HTML 中的 `img` 标签；
  - 纯文本中的图片 URL。
- 支持 JSON 路径、CSS 选择器、自定义请求头、来源子目录和文件名模板。
- SHA-256 去重，重复图片不会再次上传。
- 支持手动立即运行、OpenList 连接测试、运行日志和上传记录。
- Docker Compose 和 Python + systemd 两种部署方式。
- 默认阻止图片来源访问本机、回环地址和内网地址，降低 SSRF 风险。

## 一键安装（推荐）

适用于 Debian/Ubuntu 服务器。脚本会自动判断 Docker/Compose 是否已经安装：已安装时直接复用，不重复安装，也不要求手工编辑配置；未安装时才自动补齐依赖、安装 Docker 并启动服务：

~~~bash
curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install.sh | sudo bash
~~~

安装完成后，脚本会输出：

- 面板访问地址；
- 管理用户名；
- 首次生成的管理密码；
- 项目目录和日志命令。

默认安装目录为 `/opt/openlist-image-sync`，默认端口为 `8080`。脚本会自动生成面板密码和加密密钥，首次生成的面板密码只会在安装输出中显示，请立即保存。

如果服务器已经安装 Docker 和 Compose，也可以使用专用的一键命令。它会跳过 Docker 安装和 apt，不会触碰现有软件源：

~~~bash
curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install-docker-existing.sh | sudo bash
~~~

这个模式要求服务器已有 `docker compose` 或 `docker-compose`，然后自动完成拉取项目、生成 `.env`、构建和启动；整个过程不需要修改配置文件。

如果希望先审查脚本，再执行：

~~~bash
curl -fsSLO https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install.sh
less install.sh
sudo bash install.sh
~~~

也可以指定安装目录或端口。端口在安装后编辑 `/opt/openlist-image-sync/.env` 中的 `PORT`，然后重启容器：

~~~bash
sudo sed -i 's/^PORT=.*/PORT=9090/' /opt/openlist-image-sync/.env
cd /opt/openlist-image-sync
sudo docker compose up -d
~~~

一键安装要求：

- Debian 11/12+ 或较新的 Ubuntu；
- 能访问软件源、GitHub 和 Docker 安装源；
- 使用 root 或通过 `sudo bash` 运行；
- 服务器没有被占用的安装目录。

## 安装方式二：Docker Compose 手动部署（需要自定义配置时）

普通用户不需要手动部署，直接使用上面的“一键安装”即可。下面的方式适合希望先修改端口、限制来源地址或自行管理 `.env` 的用户：

~~~bash
git clone https://github.com/lssopk/openlist-image-sync.git
cd openlist-image-sync
cp .env.example .env
openssl rand -hex 32
nano .env
docker compose up -d --build
docker compose ps
~~~

至少修改 `.env` 中的：

~~~dotenv
APP_USERNAME=admin
APP_PASSWORD=请改成面板登录密码
APP_SECRET=请替换成随机长字符串
PORT=8080
~~~

生成 `APP_SECRET` 的示例：

~~~bash
openssl rand -hex 32
~~~

访问：

~~~text
http://你的服务器IP:8080
~~~

查看日志：

~~~bash
docker compose logs -f --tail=100
~~~

停止服务但保留数据：

~~~bash
docker compose down
~~~

## 安装方式三：Python + systemd（不使用 Docker）

如果服务器不方便运行 Docker，可以使用原生 Python 服务：

~~~bash
curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install-native.sh | sudo bash
~~~

脚本会安装 Python 虚拟环境、创建系统用户 `openlist-sync`、安装依赖、写入 systemd 服务并自动启动。

脚本不会直接改写 `/etc/apt/sources.list`。Debian 11 使用官方归档源，Debian 12+ 或 Ubuntu 在检测到旧的 `buster`、`backports`、过期 Release 等软件源时，会临时使用干净的官方源继续安装，因此不会被无关的旧源卡住。实际仍是 Debian 10/buster 的服务器已经停止维护，请先升级到 Debian 11 或更高版本。

常用管理命令：

~~~bash
sudo systemctl status openlist-image-sync
sudo systemctl restart openlist-image-sync
sudo journalctl -u openlist-image-sync -f
~~~

原生部署默认项目目录也是 `/opt/openlist-image-sync`，配置文件为 `/opt/openlist-image-sync/.env`。修改配置后执行：

~~~bash
sudo systemctl restart openlist-image-sync
~~~

## 安装方式四：已有项目目录时使用部署脚本

如果你已经把项目文件放到了服务器，可以直接运行：

~~~bash
cd /opt/openlist-image-sync
bash deploy.sh
~~~

`deploy.sh` 会自动创建 `.env`、生成随机面板密码和加密密钥、构建镜像并启动服务；已有的自定义 `.env` 不会被覆盖。

## 第一次配置

1. 打开面板并登录。
2. 在“OpenList 连接”中填写 OpenList 的完整地址，例如 `https://your-openlist.example.com`。
3. 填写 OpenList 用户名、密码和默认写入目录，例如 `/图片/API`。
4. 点击“保存连接设置”，再点击“测试连接并读取目录”。
5. 点击“添加来源”，配置一个图片 API。
6. 点击“立即运行”确认能正常抓取和上传，之后服务会按间隔自动运行。

OpenList 账号需要具备目标目录的创建目录和上传文件权限。

## API 来源示例

### 直接返回图片

接口响应的 `Content-Type` 为 `image/jpeg`、`image/png` 等时，选择“自动识别”或“直接把响应当图片”。

### JSON 返回图片地址

接口返回：

~~~json
{
  "data": [
    {"url": "https://example.com/a.jpg"},
    {"url": "https://example.com/b.png"}
  ]
}
~~~

提取方式选择“JSON 图片地址”，JSON 路径填写：

~~~text
$.data[*].url
~~~

如果不填写 JSON 路径，程序会自动寻找常见的 `url`、`src`、`image`、`photo` 等字段。

### HTML 页面

提取方式选择“HTML 图片标签”，CSS 选择器可以填写：

~~~text
img
~~~

或：

~~~text
.photo img
~~~

### API 需要请求头

请求头填写合法 JSON，例如：

~~~json
{
  "Authorization": "Bearer your-api-token",
  "User-Agent": "OpenListImageSync/1.0"
}
~~~

请求头会同时用于请求 API 和提取到的图片地址。请注意：不要把带有长期密钥的配置截图或提交到 GitHub。

## 文件名和目录

默认文件名模板：

~~~text
{date}_{index}_{hash8}.{ext}
~~~

可用变量：

- `{date}`：当前日期；
- `{time}`：当前时间；
- `{index}`：本次任务中的序号；
- `{hash8}` / `{hash}`：图片内容 SHA-256；
- `{ext}`：图片扩展名；
- `{source}`：来源名称；
- `{original}`：原始文件名。

最终路径为：

~~~text
默认写入目录 / 来源子目录 / 文件名
~~~

程序会在上传前逐级创建目录。删除面板中的来源只会删除本项目的任务和记录，不会删除 OpenList 中已经上传的图片。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_USERNAME` | `admin` | 面板登录用户名 |
| `APP_PASSWORD` | 无安全默认值 | 面板登录密码，必须修改 |
| `APP_SECRET` | 无安全默认值 | 加密 OpenList 密码和签名 Cookie，必须使用随机长字符串 |
| `PORT` | `8080` | 宿主机对外端口 |
| `DATA_DIR` | `/app/data` | SQLite 数据目录 |
| `ALLOW_PRIVATE_URLS` | `false` | 是否允许图片来源访问内网地址 |
| `MAX_DOWNLOAD_MB` | `30` | 单张响应/图片大小上限 |
| `REQUEST_TIMEOUT_SECONDS` | `45` | 单次 HTTP 请求超时时间 |

## 安全建议

- 面板首次登录后，使用强密码；不要复用 OpenList 密码。
- `.env` 含有面板密钥和 OpenList 加密数据，权限应保持为仅管理员可读。
- 公网使用时，建议通过 Caddy、Nginx 或 Traefik 配置 HTTPS，并限制管理面板访问来源。
- 默认不要开启 `ALLOW_PRIVATE_URLS`。只有当图片 API 确实在内网且你信任它时才开启。
- 只添加你有权访问和保存的图片 API，并遵守 API 的服务条款、版权和访问频率限制。
- 不要提交 `.env`、数据库文件或带鉴权信息的请求头到 GitHub。

## 备份、更新和卸载

Docker 部署至少备份下面两个文件/目录：

~~~bash
sudo cp /opt/openlist-image-sync/.env /安全备份目录/openlist-image-sync.env
sudo cp /opt/openlist-image-sync/data/app.db /安全备份目录/app.db
~~~

更新代码：

~~~bash
cd /opt/openlist-image-sync
sudo git pull --ff-only
sudo docker compose up -d --build
~~~

数据库在 `data/app.db` 中。保留 `data` 目录和 `.env`，更新代码不会丢失来源配置和历史记录。

卸载 Docker 服务但保留数据：

~~~bash
cd /opt/openlist-image-sync
sudo docker compose down
~~~

原生 systemd 服务卸载：

~~~bash
sudo systemctl disable --now openlist-image-sync
sudo rm /etc/systemd/system/openlist-image-sync.service
sudo systemctl daemon-reload
~~~

确认已经备份后，再自行移除项目目录。

## OpenList API 兼容说明

项目使用 OpenList 常用的登录、用户信息、目录读取、建目录和流式上传接口：

- `POST /api/auth/login`
- `GET /api/me`
- `POST /api/fs/list`
- `POST /api/fs/mkdir`
- `PUT /api/fs/put`

不同 OpenList 版本或反向代理配置可能对鉴权头、上传路径和权限有差异。如果连接测试失败，请先确认 OpenList 地址、账号权限、HTTPS 证书和反向代理是否允许上述接口。

## 本地开发和测试

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
.venv/bin/python -m py_compile app.py tests/test_extractors.py
~~~

本项目包含 GitHub Actions CI，会在推送和 Pull Request 时运行 Python 测试及前端 JavaScript 语法检查。

## 目录结构

~~~text
.
├── app.py                       # FastAPI 后端、调度器和 OpenList 客户端
├── static/index.html            # 单页管理面板
├── requirements.txt             # Python 依赖
├── Dockerfile                  # Docker 镜像
├── docker-compose.yml           # Docker Compose 部署
├── install.sh                  # Debian/Ubuntu Docker 一键安装
├── install-docker-existing.sh  # 已有 Docker 的纯一键安装
├── install-native.sh            # Python + systemd 一键安装
├── deploy.sh                   # 已有目录的 Docker 部署脚本
├── .env.example                # 配置模板
├── pytest.ini                  # pytest 导入路径配置
├── tests/                      # 自动化测试
└── data/                       # SQLite 数据目录（运行时生成）
~~~

## License

MIT License。详见 [LICENSE](LICENSE)。
