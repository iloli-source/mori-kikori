---------------------------- MODULE RefetchSettle ----------------------------
(* ある1日（対象日 D）の文字起こしが遅れて確定する場合に、バックフィルが「いつまで再取得するか」の
   最小モデル。時刻 now は D からの経過日数。文字起こしは D+Delay 日までのどこかで確定し得る。
   日次実行は毎日成功するとは限らない（電源断・認証失効・通信断でその日は走らない／失敗する）。

   定数:
     Delay        文字起こしが確定するまでの最大日数（実運用 7、モデルでは小さくする）
     Window       --refetch-recent の日数（実運用 Delay + 1 = 8）
     MaxDay       モデルの打ち切り日
     UseFetchTime FALSE = 修正前: 「実行日から見て直近 Window 日」だけ再取得する
                  TRUE  = 修正後: 最後に取得した日が D + Window より前のファイルも再取得する
   対象日はバックフィル範囲（--days-back、既定30日）の内側にあると仮定する。 *)
EXTENDS Naturals

CONSTANTS Delay, Window, MaxDay, UseFetchTime

ASSUME Window > Delay /\ MaxDay > Window

VARIABLES now,        \* D からの経過日数（1 = 翌日）
          arrived,    \* 遅れていた文字起こしがサーバー側で確定したか
          file,       \* ローカルファイル: "none" | "empty"（空マーク／部分）| "data"（確定内容）
          lastFetch,  \* D を最後に取得成功した日（0 = 未取得）。実装ではファイルの更新時刻
          ranToday,   \* 当日の実行が成功完了したか
          runSawAll   \* 直近の成功実行の時点で文字起こしが確定済みだったか
vars == <<now, arrived, file, lastFetch, ranToday, runSawAll>>

TypeOK ==
  /\ now \in 1..MaxDay /\ arrived \in BOOLEAN
  /\ file \in {"none", "empty", "data"}
  /\ lastFetch \in 0..MaxDay
  /\ ranToday \in BOOLEAN /\ runSawAll \in BOOLEAN

Init ==
  /\ now = 1 /\ arrived = FALSE /\ file = "none" /\ lastFetch = 0
  /\ ranToday = FALSE /\ runSawAll = FALSE

\* 文字起こしの確定（D+Delay 日まで）
Arrive ==
  /\ ~arrived /\ now <= Delay
  /\ arrived' = TRUE
  /\ UNCHANGED <<now, file, lastFetch, ranToday, runSawAll>>

\* 日付が進む（その日に成功実行が無いまま進むこともある = 電源断や失敗）
Tick ==
  /\ now < MaxDay
  /\ now' = now + 1 /\ ranToday' = FALSE
  /\ UNCHANGED <<arrived, file, lastFetch, runSawAll>>

\* 対象日を取得対象に選ぶか
Selected ==
  \/ file = "none"                          \* 未取得（find_missing_dates）
  \/ now <= Window                          \* 実行日から見て直近 Window 日（--refetch-recent）
  \/ (UseFetchTime /\ lastFetch < Window)   \* 最後の取得が D + Window より前（まだ確定前の取得）

\* 日次実行が成功完了する
RunOk ==
  /\ ~ranToday
  /\ ranToday' = TRUE /\ runSawAll' = arrived
  /\ IF Selected
       THEN file' = (IF arrived THEN "data" ELSE "empty") /\ lastFetch' = now
       ELSE UNCHANGED <<file, lastFetch>>
  /\ UNCHANGED <<now, arrived>>

\* 打ち切り日での停止（有限シナリオの終端をデッドロック扱いしないための stutter）
Finished == now = MaxDay /\ UNCHANGED vars

Next == Arrive \/ Tick \/ RunOk \/ Finished
Spec == Init /\ [][Next]_vars

\* 確定後に成功した実行があれば、ローカルは確定内容を持っている（空マークのまま取り残されない）
NoStaleAfterSuccess == (ranToday /\ runSawAll) => file = "data"
=============================================================================
