# 足球历史数据仓库 V2

本程序部署在 Ubuntu VPS 上，只读采集和分析两个公开数据源。

- 59itou：公开比赛、竞彩足球赔率和历史赛果。
- tiantianyouliao：公开比赛、赔率、停售赛果和客观模型字段。

不保存推荐、专家或会员信息；不解锁、不购买、不支付，也不执行模拟或自动下单。

## 自动工作

安装完成后，手机断开不会影响服务器：

1. 每 5 分钟采集当前比赛和赔率，只写入发生变化的赔率。
2. 首次安装后启动历史回填，以后每天从断点继续。
3. 两个网站的历史浏览器任务分别恢复各自断点，不重新处理已经入库的数据。
4. 页面到底、连续三页无新响应或达到单批上限时安全停止。
5. 比赛、赛果、赔率、模型字段和原始响应都按唯一键去重。
6. 原始 JSON 按内容哈希寻址并以 gzip 压缩；相同响应只保存一次。
7. 样本足够后自动按时间顺序训练和回测；样本不足则等待。
8. 每天校验备份数据库，每周执行无损存储维护。
9. 每 10 分钟检查两个数据源；连续失败 3 次后自动标为异常。
10. 每 5 分钟发布一份脱敏、只读的数据快照，手机状态页自动刷新。

## 手机状态页和只读接口

安装后访问：

    http://VPS公网IP:8787/

如果打不开，只需在云服务商安全组中放行入站 TCP 8787。页面显示两个网站各自的健康状态、连续失败次数、历史回填状态、仓库数量和近期比赛概率。

给 ChatGPT 读取时可以提供以下公开地址：

- `/api/v1/status`：数据源、回填、仓库和模型状态。
- `/api/v1/matches`：近期比赛的市场概率和已训练模型概率。
- `/api/v1/snapshot`：上述两部分的完整快照。

接口只读取定时生成的静态快照，不直接开放 DuckDB；不返回原始响应、服务器路径、配置、会员、推荐、购买或支付信息。

## 数据源适配层

59itou 和 tiantianyouliao 已拆成独立适配器。数据库、模型、状态页和只读接口只依赖统一字段。以后若其中一个网站失效，只需新增并注册对应适配器，不需要更换数据库或重训已有历史。第三数据源目前不启用，只有经过长期稳定性和字段测试后才会写入配置。

## 安装或升级

把压缩包完整解压到 `/opt/football-system`，然后以 root 运行：

    cd /opt/football-system
    bash scripts/install_vps.sh

安装程序会复用现有虚拟环境和浏览器，升级代码、迁移数据库并启用定时任务；不会重新解析以前上传的探针包。

## 查看状态

    cd /opt/football-system
    .venv/bin/football-analysis status

手动继续一批历史回填：

    .venv/bin/football-analysis backfill --max-actions 50 --max-replay 500

查看赔率变化与数据质量：

    .venv/bin/football-analysis report

## 数据位置

- 主数据库：`/var/lib/football-data/football.duckdb`
- 压缩原始响应：`/var/lib/football-data/raw/objects/`
- 回填断点：数据库的 `backfill_state` 表
- 模型与回测：`/var/lib/football-data/models/`
- 校验备份：`/var/lib/football-data/backups/`
- 最近采集摘要：`/var/lib/football-data/last-collection.json`
- 最近回填摘要：`/var/lib/football-data/last-backfill.json`
- 最近健康检查：`/var/lib/football-data/last-health.json`
- 只读公开快照：`/var/lib/football-data/public/snapshot.json`

历史覆盖范围以两个网站实际公开且允许读取的记录为准。公开页面遇到 4xx、5xx 或 WAF 拦截会立即停止，不会高频重试或绕过限制。状态命令会显示当前最早、最晚比赛日期和断点，不虚报多年数据已经完成。旧版本已经存入的 `source_c` 数据会保留为静态存档，但不再采集、显示或参与模型训练。
