# Advanced lab tests

The normal endpoint command and defaults are documented in [../README.md](../README.md).
The tests below require an explicitly configured disposable lab. They are never
selected by the default endpoint command. Use `./run.sh lab GROUP` from the repository root.
Factory-created buckets are removed in fixture teardown, including versions, markers,
uploads and test legal holds. Cleanup failures fail the run and write remaining names
to `remaining-test-buckets.json`. Forced process termination or an unreachable service
can prevent this cleanup.

## 1. 安裝與設定

需要 Python 3.11+、網路可達的 S3 endpoint；admin／replication 測試另需可用的
MinIO Client `mc`。使用你現有且驗證過的 mc；本套件不會自動下載或更新服務端。

```bash
cd suite
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.local.json
```

`requirements-tested.txt` 記錄本機 harness 使用的 client 套件版本；它不代表 dev 整合測試已通過。

編輯 `config.local.json`：

- `source.endpoint`：存活節點的 S3 endpoint；故障測試時不要選即將停止的節點。
- `expected_pools`、`expected_nodes`：實際的 pool／MinIO process 數，不是 erasure set 數。
- `expected_data=8`、`expected_parity=4`：每個 set 有 12 drives；每個 pool 可有多個 set。
- `expected_commit`：選填，檢查所有節點回報的 commit。仍需另外核對實際 binary SHA256；
  `DEVELOPMENT.GOGET` 或相同 commit 不足以識別未 commit 的修正。
- `expected_upgrade_commit`：選填，maintenance upgrade 後的候選版本 commit；
  `expected_commit` 是開始測試及 rollback 後的基準版本，兩者分開設定。
- `verify_tls`：預設 `true`；可填 CA bundle 路徑。不要為了通過測試而關閉 TLS 驗證。
- `mc_path`：mc executable 名稱或完整路徑。

帳密由環境變數提供，避免寫入 repo：

```bash
export TMINIO_TEST_ACCESS_KEY='你的 dev access key'
read -rsp 'Dev secret key: ' TMINIO_TEST_SECRET_KEY; echo
export TMINIO_TEST_SECRET_KEY
```

測試變數刻意使用 **`TMINIO_TEST_`** 前綴。不要改成 `MINIO_TEST_`：MinIO 會比較
各節點的 `MINIO_*` 環境變數，從 hook 啟動的節點若繼承額外變數，會被判定設定不一致。
正式模式的 dev server 不要設定 `CI` 或 `MINIO_CI_CD`；字串 `off` 仍會開啟 CI 模式。

## 2. 執行測試

完成一次設定後，日常完整執行只需要：

```bash
python run.py
```

**無參數會跑全部測試**：harness unit、topology、多執行緒 S3、ILM（包含 split object）、
admin resize、source／target fault recovery、所有 replication、rebalance、upgrade／rollback，
以及 `soak_seconds` 指定的 soak。設定檔固定預設為 **run.py 同目錄的 `config.local.json`**。
這個指令會執行已設定的停機、維運及恢復 hooks；只能指向隔離的 dev。

完整模式開始前會檢查 source／target、帳密環境變數、mc、所有 fault／maintenance hooks、
`pool_roots`／`target_pool_roots` 及兩個 cluster 的 logs 設定。
缺少必要設定會一次列出問題並以非零狀態退出，**不會先停服務或將缺項 skip 後算成功**。
設定範本故意保留 null／空值；請依第 3–8 節填入真實 dev 環境，不提供預設停機指令。
完整模式若有任何 skip、deselection、未執行測試或缺少結果證據，整次執行失敗。

要跑部分測試才加群組名稱，可一次指定多個：

```bash
python run.py replication
python run.py concurrency lifecycle
python run.py faults
python run.py movement
python run.py soak
python run.py unit
python run.py --list
```

可用群組：`unit`、`topology`、`concurrency`、`lifecycle`、`admin`、`faults`、`replication`、
`movement`、`soak`。群組自動啟用必要的 integration／admin／fault 能力，不需另外加 opt-in flags。
`unit` 和 `--list` 不需要設定檔或帳密，也不連線、不執行 diagnostics hooks。
其餘子集只檢查自己需要的設定；例如 `concurrency` 不需要 target、mc、pool roots 或 fault hooks；
`lifecycle` 包含 split fixture，因此需要 mc 和 source pool roots；`replication` 包含 target outage
及 split-version 案例，因此需要 target hooks 及 target pool roots。子集的 skip 也會讓 runner 失敗。

`max_seconds` 放在 config：`null` 時自動給功能測試 4 小時，再加選中的 `soak_seconds`；
只跑 `unit`／`soak` 時基礎額度是 15 分鐘。也可填正數覆寫；有 soak 時必須比 soak 時間長，
並為其他測試與收尾預留時間。範本的 soak 是 **300 秒短測**，不是 24 小時壓力驗證。

進階除錯仍可使用 `python run.py concurrency -- -k multipart -v`；
`--config`、`--artifacts`、`--max-seconds` 保留作覆寫。無群組完整模式只接受顯示用 pytest flags，
過濾測試必須明確指定群組。直接 `python -m pytest` 的舊 opt-in flags 仍可用於除錯，
但它不等於帶 parent watchdog、恢復及 log 檢查的完整 runner。

**同一個 dev cluster 一次只跑一份測試。不要使用 pytest-xdist `-n`。**
測試內已使用 `ThreadPoolExecutor`；多份 pytest 的 fault／global config 操作會互相干擾。

`mc admin config get` 的環境變數註解會與持久化設定分開解析，以便正確還原原值。
動態 worker 測試若發現 `MINIO_ILM_EXPIRATION_WORKERS` 覆蓋設定會先失敗；請在 dev
服務移除該覆蓋，才能驗證實際 resize。Source 的 `MINIO_DRIVE_NEWEST_POOL_MIN_FREE`
也必須移除：placement／lifecycle fixture 會在合法值 `1` 與 `99` 間切換，分別驗證留在
newest pool 與移至其他 pool，固定 ENV 會使 fixture 無效。

`run.py` 有獨立於 pytest 的總時間 watchdog。SDK 也設有 connect/read timeout，
並且關閉隱藏的自動重試，讓錯誤直接出現在結果中。正常 fault recovery 會在 `finally`
執行；pytest 被終止時，父程序會根據 pending journal 再試恢復。

## 3. 檢查項目與限制

| 測試 | 驗證內容 | 額外條件／限制 |
| --- | --- | --- |
| `test_s3_concurrency.py` | 多執行緒共享 key PUT／single、bulk DELETE；version rollback；精確版本 ID、GET/HEAD、SHA256；multipart complete/abort；legal hold | 預設 16 workers × 32 rounds；不能只看 HTTP 200 |
| `test_lifecycle.py` | 已證明在單一 pool 的 NC 到期後恰好保留 current＋10；EODM 不讓刪除物件復活 | mc、`pool_roots`；先以 `newest_pool_min_free=1` 建立並核對單 pool fixture |
| `test_split_lifecycle.py` | PUT／multipart 留在 newest pool 與空間門檻 spill；split current／version-ID 讀取；marker 放置與防復活；bulk markers 下保留 split 版本 | mc、`pool_roots`；每次寫入以 metadata hash 證明 pool，設定最後還原；不宣稱 merged cleanup 可回收空間 |
| `test_admin.py` | S3 流量期間縮減／增加 expiry workers，檢查保留版本 | admin 群組；修改整個 cluster 的 ILM config，最後恢復並讀回核對 |
| `test_faults.py` | 保持 quorum 的 node outage；失去 quorum 後恢復；所有已成功 PUT 的資料仍可讀 | faults 群組；需要 stop/start hooks 和 admin info 權限；連線中斷本身不算已證明 quorum loss |
| `test_replication.py` | 精確版本／marker／永久刪除、metadata/tags/Expires；>128MiB PUT+DELETE；target outage/backlog/恢復；replica-only marker 的 EODM grace 與 local control；從 replica 還原歷史 | 第二個獨立 cluster、mc；outage case 需要 target hooks；grace case 要求 target 有合法的正數 grace |
| `test_movement.py` | rebalance start/stop/resume 與升級/rollback 後資料核對 | movement 群組；完整 maintenance hooks；rebalance 需回報實際搬移版本數，不能 no-op 通過 |
| `test_topology.py` | 預期 pool/node/EC 配置、全節點健康、回報版本一致 | admin info 權限；實際 artifact hash 要另外留證 |

`history_versions` 預設 31；將它設為 **1003** 可涵蓋超過 1000 版本的清理批次。
`convergence_timeout` 預設 1800 秒。Compacted leaf 可能隔多個 scanner cycles 才重掃。
Fixture 建立期間被快取後，120 秒內沒有清理
不一定是 worker 卡死；仍須觀察掃描進度與最後的精確版本集合。請依 dev 的實際 cycle
時間設定上限；大規模 production 可能需要更久，也要相應調整 pytest timeout 與父程序
config 的 `max_seconds`。不要把尚未完成的清理視為成功。HEAD/LIST 也可能觸發 ILM，因此這是功能收斂測試，
不是 scanner-only 執行證明。

Object Lock 測試只涵蓋 legal hold 的基本拒刪；實際使用 retention、SSE/KMS、遠端 tier
時，仍須補對應真實服務的成功／失效測試。本套件未宣稱涵蓋這些整合。

### Split object 的實際放置證據

在 pytest 主機上提供每個 pool 的唯讀 drive 路徑，依 pool 順序排列。可以是原生路徑
或唯讀掛載；必須涵蓋每個 pool 的所有 erasure sets，建議掛載全部 drives。以下只是
路徑格式示例，不能直接當成完整 inventory；遺漏 set 或不可讀的 drive 會使放置證據不完整：

```json
"pool_roots": [
  ["/readonly/pool0/node1/drive1", "/readonly/pool0/node2/drive1"],
  ["/readonly/pool1/node1/drive1", "/readonly/pool1/node2/drive1"],
  ["/readonly/pool2/node1/drive1", "/readonly/pool2/node2/drive1"],
  ["/readonly/pool3/node1/drive1", "/readonly/pool3/node2/drive1"]
]
```

測試只讀取新建物件的 `xl.meta`，透過每次寫入前後的 SHA-256 差異判定 writer pool，
並在建立 delete marker 前確認資料已分散。原始 metadata 不會寫入 artifacts。
Split fixture 若未觀察到至少兩個 pool，或無法唯一判定寫入 pool，會直接失敗。
Source 必須能動態切換設定；磁碟可用比例也必須能讓 `1` 留在原 pool、`99` 觸發 spill。
空白磁碟若仍有超過 99% 可用空間，可能不會 spill，需先調整隔離 dev 的容量配置。
目錄可讀檢查不能自行證明 inventory 完整，執行者仍須核對所有 sets／drives 的對應。

## 4. 雙 cluster replication

將 `target` 改成：

```json
{
  "endpoint": "https://dev-backup.example:9000",
  "replication_endpoint": "https://dev-backup.internal:9000",
  "access_key_env": "TMINIO_TEST_TARGET_ACCESS_KEY",
  "secret_key_env": "TMINIO_TEST_TARGET_SECRET_KEY",
  "region": "us-east-1",
  "verify_tls": true
}
```

`replication_endpoint` 選填，供 source server 連到 target；`endpoint` 則由 pytest 連線。
另外設定兩個 target 帳密環境變數。測試用 fresh buckets 建立 source→target 規則，
包含 `delete`、`delete-marker`、`existing-objects`、`replica-metadata-sync`，不修改既有 buckets。
服務需要 admin replication 設定及新 bucket 管理權限。mc alias 放在 run 專用私有目錄，
正常結束會移除；若整台測試主機異常斷電，請移除遺留的 `mc-private` 後再分享 artifacts。

還原測試會建立新的 version IDs／時間，並保留 old→new 對照、內容與歷史順序。
它不聲稱一般 S3 replay 能原樣保留 version ID。測試的 EODM 與大物件案例，也不代表
「backlog 比 grace period 更老」的所有交錯都已證明安全。

`test_replication_resync_restores_missing_completed_marker` 另以 fresh buckets 模擬歷史損壞：
來源 marker 已是 `COMPLETED`，但目的端 marker 遺失並露出舊資料；測試只對該 bucket 的
單一 target ARN 明確執行 `mc replicate resync start`，要求 resync 完成且失敗數為零，
再逐一驗證原本 UUID、時間、內容、metadata、tags 與最新刪除狀態。這是待實測的診斷，
不代表本版本已修正 receiver／resync，也不代表歷史 marker 會自動修復。
這個測試不會對既有 bucket 自動執行修復；任何失敗仍須擋版或由發版負責人另行處理。

這個 functional case 只檢查正常完成的 resync，中斷／取消／walk error 與 worker 關閉的
並行路徑仍需另外驗證，不能由此推論沒有 channel panic 或 deadlock。

`test_replica_marker_grace_survives_eodm_control` 使用真實 source→target marker replication，
確認 target marker 帶有 `REPLICA` 狀態、source ModTime 仍在 grace 內，再啟用 EODM。
同 bucket 的一般 sole-marker control 必須先被移除，replica marker 仍須保留。這證明的是
grace 內保護，不是精確跨越兩天邊界，也不是 PUT 晚到 race 已重現。

## 5. Fault hooks

每個 hook 是 **argv JSON array**，不透過 shell 解譯，不提供預設 destructive 指令。
例如你先在 dev 管理主機寫好 `/opt/dev-minio/node-control.py`，再設定：

```json
"faults": {
  "node_down": ["python3", "/opt/dev-minio/node-control.py", "stop-one"],
  "node_up": ["python3", "/opt/dev-minio/node-control.py", "start-one"],
  "quorum_loss": ["python3", "/opt/dev-minio/node-control.py", "stop-quorum"],
  "quorum_restore": ["python3", "/opt/dev-minio/node-control.py", "restore-quorum"],
  "target_down": ["python3", "/opt/dev-minio/node-control.py", "stop-backup"],
  "target_up": ["python3", "/opt/dev-minio/node-control.py", "start-backup"],
  "hook_timeout": 60,
  "settle_seconds": 5,
  "recovery_timeout": 180
}
```

也可使用已設定 SSH 的 argv，例如 `["ssh", "dev-control", "/opt/tests/stop-one"]`。
請先手動驗證 hooks：

- 只操作隔離的 dev；`node_down` 必須保留每個受測 set 的 quorum。
- `quorum_loss` 必須真正降低 storage quorum，而不是只擋住 pytest 到 S3 的連線。
- recovery hooks 必須 **可重複執行**，已啟動時也回傳成功，且不要等待 MinIO daemon 結束。
- hooks 必須同步、有期限；不要另開 detached 的控制程序再立即回傳成功。
- hook 啟動服務時應交給 systemd／容器管理器；若直接背景啟動，必須分離 stdin/stdout/stderr。
- 不在 command line 或 stdout 放秘密；不要把暫時測試變數注入 server 的 `MINIO_*`。

中斷後可使用同一份設定與 artifacts 重試尚未完成的恢復：

```bash
python run.py --artifacts artifacts/原本的run目錄 --recover-only
```

主機被強制關機時父程序也無法恢復，仍須有人工復原步驟。
Executor 能清理已登記的本機 hook group；SSH 遠端已接受的命令、另外 daemonize 的服務
不等於可以撤銷。Ubuntu 同 process group 的後代由 PGID 清理；Windows 異常中斷後
若不能證明後代已停止，會保留 journal、阻擋自動恢復並要求人工檢查。
`--recover-only` 成功只表示恢復指令完成；仍需重新驗證節點、版本及內容。

### 跨 pool replica 永久刪除的專項回歸

`test_replica_permanent_delete_of_noncurrent_split_version` 需要 `target_pool_roots`
（格式與 `pool_roots` 相同，但指向 target）。測試讀取 `xl.meta` 的 UUID 證據，確認
舊／新版本確實落在不同 pool，才開始刪除。runner 在缺少路徑時預檢失敗，未建立 split 也會 fail。
可在隔離的 target dev 設定 `MINIO_DRIVE_NEWEST_POOL_MIN_FREE=99` 來建立 fixture，
實測後恢復正常值；仍要靠實際 metadata 證據確認 split，不可只看設定值。

## 6. 長時間 soak、logs 與放行證據

設定 `soak_seconds=86400`（24 小時），必要時 172800（48 小時）：

```bash
python run.py soak
```

至少涵蓋兩次實際 production 速度的完整物件覆查；24 小時不一定足夠。
cycle counter 增加兩次不代表所有 compacted leaves 都被重掃。
Soak 每 25 rounds 核對所有成功寫入的 data versions 後才明確刪除，避免 client 記憶體與
測試資料無限累積。`soak_verify_every` 可調；這不取代「大量長期保留版本」容量測試。
另請從監控系統留存 goroutines、heap、FD、CPU、p99、replication/ILM backlog；這套 client
的每 round 完成時間不是 production S3 p99 SLO。

在 config 分別指定 source 與 target **所有節點**的 server log 檔案（可用 glob）：

```json
"server_logs": ["/var/log/dev-minio/source-node-*.log"],
"target_server_logs": ["/var/log/dev-minio/target-node-*.log"],
"diagnostics_hook": ["python3", "/opt/dev-minio/collect-diagnostics.py"]
```

diagnostics hook 可用 `TMINIO_TEST_ARTIFACTS` 取得輸出目錄，收集 journal、goroutine／
mutex／block profile、服務退出與重啟紀錄。若 logs 在遠端，請讓 hook 匯出本次時間範圍，
並將 logs 設成匯出的固定路徑。匯出檔應在本次執行前不存在，或以 atomic replace 換檔；
不要原地覆寫比舊檔更大的內容，否則增量 offset 無法辨認覆寫的前半段。
不可只依 HTTP healthy 排除 crash/restart。
runner 掃描測試期間新增的 panic、fatal error、race、channel/map panic；無 log 路徑
在完整模式會預檢失敗；若由 diagnostics 匯出，結束後每個 pattern 都必須有可讀檔案，否則失敗。
子集未提供 logs 會明確記為 **NOT VERIFIED**，不會宣稱無 crash。程式無法從檔名證明每個 node
都已涵蓋，請另核對 log inventory 與節點清單；coverage 永遠只代表實際提供的檔案。

每次 run 保存 `junit.xml`、`results.json`、`runner.json`、bucket manifest、版本/hash
oracles、fault/recovery 證據。測試 buckets 預設保留以便調查；只針對該次
`buckets.jsonl` 的明確 bucket 名稱人工清除，不要對 cluster 使用全域清除指令。

擋版條件：任何 panic/race、錯誤版本／hash、誤刪／復活、未恢復的故障、恢復後不再
前進的工作、或必要測試未執行。release decision 還需實際 Ubuntu artifact、真實多主機
與磁碟拓樸、security scan triage、升級／rollback 和資料復原證據。

## 7. Rebalance 與升級／rollback hooks

`python run.py` 或 `python run.py movement` 都會執行 `test_movement.py`。
每個測試需要完整的 hook 組合；缺少任何必要 hook 會在預檢時失敗。
請先由 dev 環境管理者準備可重複執行的控制程式，再加入設定，例如：

```json
"maintenance": {
  "rebalance_start": ["python3", "/opt/dev-minio/maintenance-control.py", "rebalance-start"],
  "rebalance_stop": ["python3", "/opt/dev-minio/maintenance-control.py", "rebalance-stop"],
  "rebalance_resume": ["python3", "/opt/dev-minio/maintenance-control.py", "rebalance-resume"],
  "rebalance_wait": ["python3", "/opt/dev-minio/maintenance-control.py", "rebalance-wait"],
  "upgrade": ["python3", "/opt/dev-minio/maintenance-control.py", "upgrade"],
  "rollback": ["python3", "/opt/dev-minio/maintenance-control.py", "rollback"],
  "hook_timeout": 120,
  "rebalance_wait_timeout": 600,
  "recovery_timeout": 180,
  "workers": 4,
  "rounds_per_phase": 8
}
```

以上路徑是由管理者提供的程式，測試不下載或替換 server binary，也沒有預設維運命令。
hook 以 argv 執行，不經 shell 展開。測試傳入 `TMINIO_TEST_BUCKET` 與
`TMINIO_TEST_ARTIFACTS`，供控制程式識別本次 fixture 與證據目錄。
不要將測試專用變數命名為 `MINIO_*`，以免被啟動中的 MinIO 當成 server 設定。
所有 recovery hooks 都必須可重複執行；啟動 daemon 時應由 systemd／容器管理，
或明確重導向 daemon 的 stdin/stdout/stderr。

Rebalance 測試在開始前、開始後、resume 後分別要求所有 client workers 有已確認的 PUT。
接著核對原始版本 IDs、SHA-256、size、自訂 metadata、tags、Expires、delete markers
及 latest object 可見性，並重新讀取所有已確認的並行寫入。
`rebalance_wait` 必須等待本次工作完成，stdout 只輸出一個 JSON object：

```json
{"moved_versions": 42, "failed_versions": 0}
```

`moved_versions` 必須是正整數，`failed_versions` 必須為整數 0。空閒、未搬移任何版本的
rebalance 不算通過。數值必須取自本次實際工作，不能填固定成功值；控制程式可利用
`TMINIO_TEST_BUCKET` 追蹤測試資料。測試核對 hook 回報的進度及 S3 結果，實際 pool 落點
仍需由控制程式或 server 端證據確認。其餘 hooks 的 stdout 不會保存；進度 JSON 使用
暫存檔接收，避免背景 daemon 持有 pipe 讓等待卡住。

升級／rollback 是另一個測試：先保存每個 node 的 version／commit 身分與資料，
要求 upgrade 後所有原始 nodes 的身分確實改變，再由新版寫入新版本與 delete markers。
rollback 後必須恢復每個 node 原本的身分，並能正確讀取升級前及升級後寫入的資料。
自訂 binary 若使用完全相同的 version／commit 標記，這個 gate 無法確認升級發生，會失敗；
請讓待測 artifacts 帶有可區分且正確的 build metadata。

```bash
python run.py movement
```

開始 rebalance／resume／upgrade 前，測試會原子寫入
`maintenance-pending-recovery.json`，例如：

```json
{"upgrade": {"hook": "rollback", "bucket": "dev-release-example"}}
```

Rebalance 途中失敗會嘗試 stop，upgrade 無論成功或失敗都會嘗試 rollback。
runner 在 timeout／中斷後也會重試 journal 中的 recovery；失敗項目保留且整次 run 失敗。
可用同一份設定及 artifacts 執行 `--recover-only`。若測試主機或 runner 已無法運作，
請在控制主機執行 journal 對應的 recovery argv，確認原本服務身分與 fixture 資料後再處理
journal，並保留失敗證據。沒有 hooks 或沒有實際升級／搬移證據，不能計為 release 通過。

## 8. Ubuntu SSH／systemd 現成 fault adapter

若 dev nodes 已由 systemd 管理，可使用隨附的 `service_hook.py`，不用另外撰寫
node 控制程式。它只支援 stop／start 已明列的服務，不部署 binary、不執行 rollback，
也不刪除資料。先複製 `service_inventory.example.json` 為
`service_inventory.local.json`，填入每個 node 的 SSH alias 與 `.service` 名稱。
確認全部都是可停機的 dev targets 後，才把 `environment` 改為 `dev`；範本預設拒絕執行。
這個欄位是操作人的明確確認，程式無法自行判斷遠端是否為正式環境。

SSH alias 由控制主機的 `~/.ssh/config` 指定使用者、金鑰與 port。Adapter 強制
`BatchMode=yes`、`StrictHostKeyChecking=yes`，所以必須先建立可信的 known-hosts 紀錄，
並配置無互動登入。預設使用 `sudo -n systemctl`，需要對指定服務的 start／stop 權限；
若 SSH 使用者本來就有權限，可設 `"sudo": false`。Host 欄位只接受 alias／DNS 名稱，
不接受 `user@host`、shell 片段或 SSH options；服務名稱也經 allowlist 驗證。

Inventory 的 groups 對應如下，請依實際 erasure topology 調整成員：

| Group | 停機 action | 恢復 action | 用途 |
| --- | --- | --- | --- |
| `node` | `node_down` | `node_up` | 停部分 source nodes，仍保留 write quorum 與 S3 endpoint |
| `quorum` | `quorum_loss` | `quorum_restore` | 停足夠的 source nodes，確實使 erasure set 失去 quorum |
| `target` | `target_down` | `target_up` | 停 replication target 的服務；不影響 source |

在 `config.local.json` 的 `faults` 中填入以下 argv，將 `/opt/tminio-tests` 換成實際
`suite` 所在的絕對路徑，Python 路徑也須指向已安裝測試依賴的環境：

```json
{
  "node_down": ["/opt/tminio-tests/.venv/bin/python", "/opt/tminio-tests/service_hook.py", "--inventory", "/opt/tminio-tests/service_inventory.local.json", "node_down"],
  "node_up": ["/opt/tminio-tests/.venv/bin/python", "/opt/tminio-tests/service_hook.py", "--inventory", "/opt/tminio-tests/service_inventory.local.json", "node_up"],
  "quorum_loss": ["/opt/tminio-tests/.venv/bin/python", "/opt/tminio-tests/service_hook.py", "--inventory", "/opt/tminio-tests/service_inventory.local.json", "quorum_loss"],
  "quorum_restore": ["/opt/tminio-tests/.venv/bin/python", "/opt/tminio-tests/service_hook.py", "--inventory", "/opt/tminio-tests/service_inventory.local.json", "quorum_restore"],
  "target_down": ["/opt/tminio-tests/.venv/bin/python", "/opt/tminio-tests/service_hook.py", "--inventory", "/opt/tminio-tests/service_inventory.local.json", "target_down"],
  "target_up": ["/opt/tminio-tests/.venv/bin/python", "/opt/tminio-tests/service_hook.py", "--inventory", "/opt/tminio-tests/service_inventory.local.json", "target_up"],
  "hook_timeout": 120
}
```

每個 SSH 呼叫預設上限 20 秒（inventory 的 `timeout_seconds`），按 group 順序執行。
外層 `faults.hook_timeout` 必須大於「最大 group 成員數 × timeout_seconds」並保留啟動餘裕；
範本四個成員使用 120 秒。所有成員會先完成驗證才開始執行，恢復時即使某個 node 失敗，
仍會嘗試其餘 nodes。`systemctl start` 可重複執行；只有全組成功才回傳 exit 0。
輸出只包含 action、node 名稱、exit code／錯誤類別，不記錄 SSH stderr 或金鑰。
SSH timeout 不等於取消遠端已送出的 systemd job；中斷後須以相同 recovery action
重新確認服務狀態。上層測試仍會檢查 topology、S3 可用性及版本內容，不把 exit 0 當成 release 通過。

Adapter 的離線驗證不會啟動 SSH，也不需要任何 cluster：

```bash
python -m pytest test_service_hook_unit.py -q
```
