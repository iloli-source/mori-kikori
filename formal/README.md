# formal/ — TLA+ モデル（mori-kikori）

日次実行（launchd / cron → `run_mori_catchup.sh` → `run_mori_daily.sh` → `mori_fetch.py`）と
手動実行の並行・クラッシュ・再実行にまつわる不具合を、小さな有界モデルで再現し、修正後のモデルが
通ることを TLC で確認したもの。**有界モデルをモデル検査しただけで、コードを形式検証したわけではない。**
モデルと実装の対応は下表の回帰テストで担保する。

## 実行

TLC（`tla2tools.jar`）と Java が必要。マニフェストは `formal/tlc-checks.json`
（1行 = spec・cfg・期待結果）。各 cfg を次のように実行する。

```bash
cd formal
java -cp /path/to/tla2tools.jar tlc2.TLC -config LockReclaimBugEmptyPid.cfg LockReclaim.tla
```

- `*Bug*.cfg` は対象の性質を1つだけ検査し、**違反（反例）が出ることが期待値**。違反が出なければ失敗。
- `*Fixed.cfg` は全性質が通ることが期待値。
- `TokenRefreshResidualCrash.cfg` は「修正後も残る窓」を記録するためのもので、違反が出ることが期待値。

## 追跡表（観測 → モデル/性質 → 反例 → 修正 → 回帰テスト）

| # | 観測・懸念 | モデル / 性質 | 反例（Bug cfg） | 修正 | 回帰テスト |
|---|---|---|---|---|---|
| 1 | 手動実行と重なった日に、取得していないのに成功スタンプが付く | `CatchupStamp.tla` / `StampImpliesSuccess` | `CatchupStampBug`: catchup が事前確認（先行実行なし）→ 手動実行がロック取得 → catchup が daily を起動 → daily はロックに弾かれ exit 0 → catchup がスタンプ。手動実行がその後失敗しても、その日の毎時リトライは止まり、連続失敗カウンタも消える | daily はロックを取れなければ exit 75。catchup は 75 を「未実施」として扱い、スタンプも失敗カウントも触らない（`run_mori_daily.sh` / `run_mori_catchup.sh`）。競合の入口だった事前確認は削除 | `tests/test_shell_locks.py`: `test_catchup_does_not_stamp_while_manual_run_holds_the_run_lock`, `test_catchup_stamp_and_failcount_by_exit_code`, `test_skip_does_not_reset_an_existing_failure_count` |
| 2 | mkdir + pid ファイル方式のロックで二重起動し得る | `LockReclaim.tla` / `MutualExclusion` | `LockReclaimBugEmptyPid`: a が mkdir → b は mkdir 失敗、pid が空なので残骸と判断 → a が pid を書き、読み直して本処理へ → b が rm -rf → mkdir → pid を書き、読み直して本処理へ（残骸が無くても起きる）。`LockReclaimBugStale`: 死んだプロセスの残骸を a・b が同時に回収（a の作り直したロックを b の rm が消す） | `runlock.py`（`flock(2)`）。保持者が終了すれば OS が解放するので残骸回収の手順が無い。2本のシェルスクリプトと `mori_fetch.py` が共有 | `tests/test_runlock.py::TestRunLock`, `tests/test_shell_locks.py`: `test_simultaneous_daily_runs_admit_exactly_one`, `test_daily_exits_75_and_does_nothing_while_another_run_holds_the_lock`, `test_lock_is_released_when_holder_is_killed` ほか |
| 3a | 手動の `mori_fetch.py` と日次実行が同じリフレッシュトークンを送り、片方が拒否される | `TokenRefresh.tla` / `NoRejectedRefresh` | `TokenRefreshBugConcurrent`: launchd がトークンを読んで送信（サーバーが回転）→ 保存前に manual が古いトークンを読んで送信 → invalid_grant（「認証が失効」と誤通知され、その実行は失敗） | `mori_fetch.py` の全モード（取得・`--login`・`--list-tools`）が実行ロックの中で動く。トークンはロック取得後に読み直される | `tests/test_runlock.py::TestMoriFetchTakesRunLock` |
| 3b | トークン更新の応答を受けた後・保存前に落ちると、保存済みトークンが無効になる | `TokenRefresh.tla` / `FileUsableAtRest` | `TokenRefreshResidualCrash`: 読む → 送信（サーバーが回転）→ クラッシュ。`tokens.json` には無効になった世代が残る | **クライアント側だけでは閉じられない**（サーバーの回転と手元の保存を1操作にできない）。窓を最小にするため、保存は fsync してから置換し、ディレクトリも同期する（`auth_store.py`）。落ちた場合は `--login` が必要 | `tests/test_auth_refresh.py::TestTokenPersistenceIsDurable` |
| 4 | 確定前に取得した日（空マーク／部分データ）が、再取得されないまま取り残される | `RefetchSettle.tla` / `NoStaleAfterSuccess` | `RefetchSettleBug`: 翌日に取得（空マーク）→ 文字起こしが確定 → 再取得の窓の日数だけ実行が成功しない（電源断・認証失効）→ 再開した実行は成功するが、対象日は「直近N日」の外なので再取得されない | 最後に取得した日（ファイルの更新時刻）が「対象日 + N 日」より前のファイルは、窓の外でも再取得する（`mori_fetch.py:find_unsettled_dates`）。内容を保持して書き換えない分岐でも更新時刻を進める | `tests/test_refetch_settle.py` |

### 不成立としたもの

- **空マークの判定と書き込みの間に別プロセスが書く**（`download_single_date` のサイズ確認 → 書き込み）:
  すべての入口が実行ロックを取るようになったため、2プロセスが同時にこの区間に入ることがない（#2・#3a の修正に含まれる）。
- **再取得の窓の内側にある空マーク**: 窓の内側の日は取得済みでも毎回再取得され、遅れて確定した内容で上書きされる。問題は窓の外に出た後だけ（#4）。

## 前提とモデル化していない境界

- **LockReclaim**: ロックファイルを人が削除した場合（別 inode への flock が並立する）はモデル外。
  運用上ロックファイルは消さない。NFS 等 flock が効かないファイルシステムは対象外。
  pid 再利用による「永久に起動しない」問題（活性）もモデル外だが、flock は pid を見ないので発生しない。
- **CatchupStamp**: ロックは原子的に扱う（取得手順の競合は LockReclaim が検査）。日付の変わり目は扱わない。
  スタンプ用の日付は catchup 開始時点のものなので、実行が 0 時を跨ぐと前日のスタンプになり、
  翌日もう一度実行されるだけ（取りこぼす方向には働かない）。
- **TokenRefresh**: 「サーバーは更新のたびにリフレッシュトークンを回転させ、古いものを即無効にする」と仮定。
  実際のサーバーに猶予期間がある場合、3a・3b は起きにくくなる（悪化はしない）。再利用検知で
  トークン系列ごと失効させる実装の場合、3a は一時的な失敗ではなく再ログインが必要になる。
  mcp SDK がプロセス内で行うトークン更新は同じロックの内側なのでモデルに含めていない。
- **RefetchSettle**: 対象日がバックフィル範囲（既定は過去30日、`--days-back`）の内側にあることが前提。
  それより長く止まっていた場合は範囲を広げて手動実行する。文字起こしが `Delay`（公称7日）より
  遅れて確定する場合は対象外。「最後に取得した日」はファイルの更新時刻で代用しているため、
  ファイルを手でコピー・編集して更新時刻が変わると判定も変わる。
