# WebSub 租约通知与源站回补的连续采集资料服务

项目接收 websub-feed 来源资料，并新增带**租约代际**的回调接收与**源站回补**。原有目录只核对 lease, article, revision, withdrawal 的基本身份和引用；`capture_data.io` 冻结字节和请求来源，`capture_data.domain` 核对本领域记录结构，`capture_data.journal` 封存来源原文和版本，`capture_data.projection` 显示本地引用缺口。

新增模块：

- `capture_data.signature`：回调 HMAC 签名核对；
- `capture_data.lease`：订阅身份与租约代际，续租交叠时旧/新回调分开归属；
- `capture_data.callback`：签名、topic、内容摘要、租约有效期一起核对，只生成“待抓取”通知；
- `capture_data.source`：源站分页乱序接收、去重、缺口与连续水位、文章状态推导；
- `capture_data.report`：运营视图，列出未补齐空档、未证实通知与证据异常；
- `capture_data.store`：在原账本上补齐订阅/租约/回调/回补表。

Python 3.11 以上，无第三方依赖。样例均为合成，不访问真实站点。

```sh
python -m unittest discover -s tests -v
python -m compileall -q capture_data tests
python -m capture_data.cli inspect data/normal.json
```

## 运维流程（持久化账本）

```sh
# 登记订阅与租约
python -m capture_data.cli subscribe --db journal.sqlite3 \
  --topic https://publisher.example.test/feed --hub https://hub.example.test \
  --secret-env WEBSUB_SECRET --at 2026-11-17T10:00:00Z
python -m capture_data.cli lease --db journal.sqlite3 \
  --topic https://publisher.example.test/feed --hub https://hub.example.test \
  --lease-id lease-21 --expires 2026-11-17T11:00:00Z --at 2026-11-17T10:00:00Z

# 接收回调（--signature 为 X-Hub-Signature-256 原文）
python -m capture_data.cli callback --db journal.sqlite3 \
  --topic https://publisher.example.test/feed --body callback.json \
  --signature "sha256=..." --at 2026-11-17T10:01:00Z

# 回补一页源站资料
python -m capture_data.cli page --db journal.sqlite3 \
  --topic https://publisher.example.test/feed --body page.json --at 2026-11-17T10:02:00Z

# 换 hub、停用某代际、查看运营报告
python -m capture_data.cli set-hub --db journal.sqlite3 --topic ... --hub ... --secret-env ...
python -m capture_data.cli retire --db journal.sqlite3 --topic ... --generation 1
python -m capture_data.cli report --db journal.sqlite3 --topic ...
```

Dockerfile 提供 Linux 运行环境；`docker build -t source-capture .` 构建，`docker run --rm source-capture python -m unittest discover -s tests -v` 运行测试。

协议样例可通过 `python -m capture_data.cli inspect data/protocol-case.json` 查看。该命令仍只说明现有接收能力，不证明远端连续性。
