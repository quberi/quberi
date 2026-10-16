# Quberi 部署指南

本文档记录 Quberi 的本地开发部署和生产部署流程，包括这个项目里踩过的坑，避免以后重复排查。

---

## 一、架构总览

```
compose.yaml              — 基础服务栈（本地开发 + 生产共用）
deploy/compose.prod.yaml  — 生产环境覆盖层，只加一个 caddy 反代
deploy/Caddyfile          — Caddy 的站点配置（TLS + 反代规则）
deploy/certs/             — TLS 证书（gitignored，需要在服务器上手动生成/放置）
deploy/.env.prod.tmp      — 生产环境变量参考（gitignored，真正的生产 .env 在服务器上）
```

`compose.yaml` 定义的服务：`postgres`、`redis`、`qdrant`、`minio`（+`minio-init`）、`api`、`worker`（复用 `api` 镜像，跑 arq worker）、`web`。生产环境额外叠加一个 `caddy` 服务做 TLS 终止和反代（不修改任何已有服务）。

---

## 二、本地开发部署

### 1. 前置条件

- Docker Desktop（本机用的是 29.4.2 + BuildKit）。
- 根目录 `.env`（从 `.env.example` 复制并填真实值）。
- pnpm（`package.json` 声明 `packageManager: pnpm@10.9.0`，前端依赖用它管理，不要用 npm 装 workspace 根目录）。

### 2. 启动完整服务栈

```bash
make up      # 等价于: docker compose --env-file .env --profile app up -d --build
make ps      # 查看容器状态
make logs    # 跟踪 api 日志
make down    # 停止（保留数据卷）
make nuke    # 停止并删除数据卷（销毁本地数据，谨慎使用）
```

首次构建耗时（实测数据，供预期参考）：
- `api`：无缓存全新构建约 **407 秒**（要装 torch + transformers + sentence-transformers，这个量级是正常的，不是卡死）。源码改动后重建（torch 层走缓存）约 **68 秒**。
- `web`：用 pnpm + 锁文件构建约 **60 秒**。

### 3. 单独构建某个服务

```bash
docker compose --env-file .env --profile app build api
docker compose --env-file .env --profile app build web
```

### 4. 后端本地开发环境（跑 lint / 测试 / migration，不进容器）

```bash
cd apps/api
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"   # 装 fastapi 等运行依赖 + ruff/pytest 开发依赖
```

之后可以用：
```bash
make lint           # ruff check
make check-drift    # 校验 ORM 模型和最新 migration 是否一致
make test-api       # 跑后端测试（需要本机 5432 端口的 Postgres 可连，见下）
make migrate        # 对本机 Postgres 跑 alembic upgrade head
make check          # lint + check-drift + test（提交前建议跑一遍）
```

`make test-api` 需要一个独立的 `quberi_test` 数据库（不是开发数据库 `quberi`）：
```bash
docker exec quberi-postgres-1 psql -U quberi -d quberi -c "CREATE DATABASE quberi_test OWNER quberi"
```
测试用的连接串默认是 `postgresql://quberi:change-me-local-only@127.0.0.1:5432/quberi_test`（见 `apps/api/tests/conftest.py`），必要时用环境变量 `TEST_POSTGRES_DSN` 覆盖。测试代码里做了保护：DSN 不包含 `test` 字样会直接拒绝运行，防止手滑跑到开发库上。

### 5. 前端本地开发环境

```bash
cd apps/web
pnpm install
pnpm run dev      # 本地开发服务器
pnpm run test     # vitest
pnpm run build    # tsc --noEmit && vite build
```

---

## 三、生产部署

### 1. 服务器准备

- 一台跑 Docker 的服务器（当前生产节点是海外机器，`ai.quberi.com` / `api.quberi.com` 指向这台服务器）。
- 克隆本仓库到服务器（比如 `/root/quberi`）。
- 复制根目录 `.env.example` 为 `.env`，按 `deploy/.env.prod.tmp` 里的字段填生产值（`QUBERI_ENVIRONMENT=production`、真实域名、真实密钥等——这个 `.tmp` 文件本身是 gitignored 的参考文件，不是直接生效的配置）。

### 2. TLS 证书

`Caddyfile` 用的是 **Cloudflare Origin CA 证书**（不是 Let's Encrypt 自动签发），需要手动生成：
1. Cloudflare 控制台 → SSL/TLS → Origin Server → Create Certificate。
2. 把证书内容存成 `deploy/certs/cert.pem`，私钥存成 `deploy/certs/key.pem`。
3. Cloudflare 的 SSL/TLS 模式必须设置为 **Full (strict)**，否则边缘到源站这段会失败。

`deploy/certs/` 整个目录是 gitignored 的，每台服务器要单独放一份，不会随代码库同步。

### 3. 启动

```bash
cd /root/quberi   # 或实际的仓库路径
docker compose --env-file .env -f compose.yaml -f deploy/compose.prod.yaml --profile app up -d --build
```

`compose.prod.yaml` 是纯覆盖层，只新增一个 `caddy` 容器做 TLS 终止 + 反代到 `web:80` / `api:8000`，不改动任何已有服务的定义。

### 4. 验证

```bash
docker compose --env-file .env -f compose.yaml -f deploy/compose.prod.yaml --profile app ps
curl -I https://ai.quberi.com
curl -I https://api.quberi.com/health/live
```

---

## 四、已知坑 & 排错记录

### 4.1 本机 Docker 镜像加速站可能拦截小众镜像

**现象**：`docker pull pgsty/silo:latest`（MinIO 的社区维护替代镜像，见下）报 `403 Forbidden`，但生产服务器上同一个镜像能正常拉取，digest 完全一致。

**根因**：本机 `~/.docker/daemon.json` 配置了阿里云私有镜像加速站（`registry-mirrors: ["https://4jr5xld0.mirror.aliyuncs.com"]`），这个加速站对某些小众第三方仓库（比如 `pgsty/*`）返回 403，但镜像本身在 Docker Hub 上是健康的。

**排错方法**：
```bash
docker info | grep -i "Registry Mirrors"   # 看是否配置了加速站
cat ~/.docker/daemon.json
```

**修复**：从 `~/.docker/daemon.json` 里删掉 `registry-mirrors` 那一项，重启 Docker Desktop（`osascript -e 'quit app "Docker"'` 然后 `open -a Docker`；如果主进程没有真正退出，可能要手动 `kill` 残留的 `Docker Desktop Helper` 子进程再重新 `open`）。改动前记得备份原文件。

这不是每次都会复现的问题——如果镜像本来就在本地缓存里（比如生产服务器 9 月中就拉过、一直没重启过容器），不会触发；一旦要重新拉取才会暴露。

### 4.2 MinIO 官方镜像已被下架，用的是社区 fork

**背景**（2025-2026 年时间线）：
- 2025年10月：MinIO 停止发布社区版的预编译二进制/镜像。
- 2025年12月 → 2026年2月：社区版进入维护模式 → 标记"不再维护"。
- 2026年4月：社区版仓库被官方归档。
- 2026年9月：`minio/minio`、`minio/mc` 在 Docker Hub 上的仓库被官方**整个删除**。

**现状**：`compose.yaml` 里用的是 `pgsty/silo:latest`（`minio/minio` 的社区维护 fork，S3/Admin API 和磁盘格式保持兼容）和 `pgsty/mc:latest`（对应 `minio/mc` 的 fork）。这两个镜像**目前是健康的**（本文档撰写时验证过，Docker Hub 直连可正常拉取），但因为是个人/小团队维护的替代方案，不排除未来某天也会停更或被下架。如果哪天 `pgsty/*` 也拉不到了，去 https://github.com/pgsty/silo 看有没有新地址，或者评估换成真正的 S3/R2（`compose.yaml` 注释里也留了这条提示）。

**排错方法**（怀疑镜像失效时）：
```bash
docker pull pgsty/silo:latest   # 先排除是不是本机加速站问题（见 4.1）
docker image inspect pgsty/silo:latest --format '{{.Created}}'   # 看本地缓存是哪天拉的
```

### 4.3 web 的 Docker 构建曾经卡死在 `npm install`

**现象**：`apps/web/Dockerfile` 原来用 `npm install --include=dev`，没有锁文件（`package-lock.json` 不存在），每次构建都要重新解析完整依赖树；加了 vitest 全家桶依赖之后，容器里的 `npm install` 卡了 3 分钟以上都没跑完。

**修复**：改用 pnpm + 仓库根目录的 `pnpm-lock.yaml`（workspace 声明在 `pnpm-workspace.yaml`）。因为 `apps/web/Dockerfile` 需要读到根目录的锁文件，**`compose.yaml` 里 web 服务的 build context 从 `./apps/web` 改成了仓库根目录 `.`**（配合根目录新增的 `.dockerignore`，避免把 `apps/api/.venv`（~800MB）、根 `node_modules`、`.git` 等一并塞进构建上下文）。修复后 pnpm install 约 50 秒完成。

如果以后要改 `apps/web/Dockerfile`，记得：
- 构建上下文是仓库根目录，不是 `apps/web/`，Dockerfile 里 `COPY` 路径要带 `apps/web/` 前缀。
- `compose.yaml` 里对应服务要写 `dockerfile: apps/web/Dockerfile`（而不是默认的 `apps/web/Dockerfile` 相对路径）。

### 4.4 api 的 Docker 构建加了 BuildKit 缓存 + 可覆盖镜像源

`apps/api/Dockerfile` 里 `torch` 和其余依赖分两层装，都用了 `--mount=type=cache,target=/root/.cache/pip`，重复构建不会重新下载没变化的包。镜像源通过构建参数可覆盖：

```bash
docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple/ \
             --build-arg PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu \
             --build-arg TORCH_VERSION=2.14.1 \
             -t quberi-api apps/api
```

**踩过的坑**：曾经试过换成阿里云的 `mirrors.aliyun.com/pytorch-wheels/cpu`，结果发现那个页面是 JS 渲染的浏览页，不是标准 PEP 503 索引格式，pip 解析不了；而且它的版本号带 `+cpu` 本地标识符（`2.6.0+cpu`），跟官方索引的纯版本号（`2.6.0`）不一致。最终改回官方索引，实测连通性良好（1-2秒响应），无缓存全新构建约407秒是正常耗时，不需要为了"提速"去冒险换不可靠的镜像源。

### 4.5 曾经用过的临时手段：`.apioverlay/`（已废弃，不要再用）

这个会话早期，因为 api 完整构建总卡住，一度用 `docker cp` 把改好的 `.py` 文件直接热替换进运行中的容器，再拿一个 `.apioverlay/Dockerfile`（`FROM quberi-api:latest` + 若干 `COPY`）把这些热改"烘"进镜像，绕开重新跑 `pip install`。**这个目录现在已经删除**，因为 4.4 的修复让正规的 `docker compose build api` 本身就能在可接受时间内跑完。如果又看到类似的手段被提起或者复现类似文件，直接走正规构建，不要重新捡起这个模式——它会导致运行中的代码和 `apps/api/src` 下的源码逐渐脱节（这个会话里就因为这样，发现过 4 个文件的 overlay 版本落后于正式源码）。

---

## 五、常见操作速查

| 想做什么 | 命令 |
|---|---|
| 本地起全部服务 | `make up` |
| 只重建 api 镜像 | `docker compose --env-file .env --profile app build api` |
| 只重建 web 镜像 | `docker compose --env-file .env --profile app build web` |
| 重建单个容器（不重新构建镜像） | `docker compose --env-file .env --profile app up -d --no-build <service>` |
| 看某个容器日志 | `docker compose --env-file .env --profile app logs -f <service>` |
| 本机对开发库跑 migration | `make migrate` |
| 生成新 migration | `make revision m="说明文字"` |
| 提交前检查 | `make check` |
| 生产：启动/更新 | `docker compose --env-file .env -f compose.yaml -f deploy/compose.prod.yaml --profile app up -d --build` |
| 检查本机镜像加速站配置 | `docker info \| grep -i "Registry Mirrors"` |

---

*文档创建时间：2026年9月30日，随本次会话的部署问题排查一并整理。*
