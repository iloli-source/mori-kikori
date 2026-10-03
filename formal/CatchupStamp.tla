--------------------------- MODULE CatchupStamp ---------------------------
(* run_mori_catchup.sh（C）が run_mori_daily.sh（B）を起動して成功スタンプを書く流れと、
   手動実行（M: run_mori_daily.sh または mori_fetch.py）が実行ロックを取り合う競合の最小モデル。
   B はロックを取れないと本処理をせずに即終了する。その終了コードを
     SkipExitIsSuccess = TRUE : 0（修正前。catchup は成功と区別できない）
     SkipExitIsSuccess = FALSE: 専用コード 75（修正後。catchup はスタンプも失敗カウントも触らない）
   とする。ロック自体は原子的に扱う（取得手順の競合は LockReclaim.tla が別に検査する）。 *)
EXTENDS Naturals

CONSTANT SkipExitIsSuccess

VARIABLES runLock,     \* 実行ロックの保持者: "none" | "M" | "B"
          stamp,       \* logs/.last-success-date が当日になったか
          anySuccess,  \* 当日に本処理（取得）が成功完了したか
          pcC,         \* catchup: "start" -> "checked" -> "invoked" -> "done"
          pcM,         \* 手動実行: "idle" -> "running" -> "done"
          bResult,     \* C が起動した daily の結果: "none" | "running" | "skip" | "ok" | "fail"
          failCount    \* logs/.consecutive-failures
vars == <<runLock, stamp, anySuccess, pcC, pcM, bResult, failCount>>

TypeOK ==
  /\ runLock \in {"none", "M", "B"}
  /\ stamp \in BOOLEAN /\ anySuccess \in BOOLEAN
  /\ pcC \in {"start", "checked", "invoked", "done"}
  /\ pcM \in {"idle", "running", "done"}
  /\ bResult \in {"none", "running", "skip", "ok", "fail"}
  /\ failCount \in 0..1

Init ==
  /\ runLock = "none" /\ stamp = FALSE /\ anySuccess = FALSE
  /\ pcC = "start" /\ pcM = "idle" /\ bResult = "none" /\ failCount = 0

\* catchup の事前確認（修正前のみ存在）: 先行実行が見えれば何もせず終了、見えなければ進む
C_CheckLock ==
  /\ pcC = "start"
  /\ pcC' = IF runLock = "none" THEN "checked" ELSE "done"
  /\ UNCHANGED <<runLock, stamp, anySuccess, pcM, bResult, failCount>>

\* 手動実行がロックを取る（事前確認と起動の間に割り込み得る）
M_Acquire ==
  /\ pcM = "idle" /\ runLock = "none"
  /\ runLock' = "M" /\ pcM' = "running"
  /\ UNCHANGED <<stamp, anySuccess, pcC, bResult, failCount>>

\* 手動実行の完了（成功/失敗は非決定: 通信断や認証失効）
M_Finish ==
  /\ pcM = "running"
  /\ \E ok \in BOOLEAN : anySuccess' = (anySuccess \/ ok)
  /\ runLock' = "none" /\ pcM' = "done"
  /\ UNCHANGED <<stamp, pcC, bResult, failCount>>

\* catchup が daily を起動。ロックが取れれば本処理へ、取れなければ即終了（skip）
C_Invoke ==
  /\ pcC = "checked"
  /\ pcC' = "invoked"
  /\ IF runLock = "none"
       THEN runLock' = "B" /\ bResult' = "running"
       ELSE runLock' = runLock /\ bResult' = "skip"
  /\ UNCHANGED <<stamp, anySuccess, pcM, failCount>>

\* C が起動した daily の本処理が終わる
B_Finish ==
  /\ pcC = "invoked" /\ bResult = "running"
  /\ \E ok \in BOOLEAN :
       /\ bResult' = IF ok THEN "ok" ELSE "fail"
       /\ anySuccess' = (anySuccess \/ ok)
  /\ runLock' = "none"
  /\ UNCHANGED <<stamp, pcC, pcM, failCount>>

\* catchup が終了コードを解釈してスタンプ／失敗カウントを更新
C_Stamp ==
  /\ pcC = "invoked" /\ bResult \in {"skip", "ok", "fail"}
  /\ stamp' = ((bResult = "ok") \/ (bResult = "skip" /\ SkipExitIsSuccess))
  /\ failCount' = IF bResult = "fail" THEN failCount + 1 ELSE failCount
  /\ pcC' = "done"
  /\ UNCHANGED <<runLock, anySuccess, pcM, bResult>>

\* 全員終了後の停止（有限シナリオの終端をデッドロック扱いしないための stutter）
Finished == pcC = "done" /\ pcM \in {"idle", "done"} /\ UNCHANGED vars

Next == C_CheckLock \/ M_Acquire \/ M_Finish \/ C_Invoke \/ B_Finish \/ C_Stamp \/ Finished
Spec == Init /\ [][Next]_vars

\* 成功スタンプは「当日に本処理が成功完了した」事実の上にだけ立つ。
\* 破れると、その日の残りの毎時リトライが止まり、連続失敗カウンタも消える
StampImpliesSuccess == stamp => anySuccess

\* skip は失敗としても数えない（連続失敗通知の誤発火防止）
FailCountOnlyOnFail == failCount > 0 => bResult = "fail"
=============================================================================
