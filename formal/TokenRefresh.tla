---------------------------- MODULE TokenRefresh ----------------------------
(* ensure_fresh_token（tokens.json のリフレッシュトークンを読む → トークンエンドポイントへ送る →
   応答の新しいトークンを tokens.json に保存）を複数プロセスが行うときの最小モデル。
   サーバーは更新のたびにリフレッシュトークンを回転させ、古いものを無効にすると仮定する
   （serverRT = いまサーバーが受け付ける世代）。

   定数:
     Procs        同時に走り得るプロセス（launchd の日次実行と手動の mori_fetch.py）
     UseLock      TRUE = 実行ロックの中で「読む → 送る → 保存」を行う（修正後）
     CrashAllowed TRUE = 応答受信後・保存前にプロセスが落ち得る（電源断・kill） *)
EXTENDS Naturals, FiniteSets

CONSTANTS Procs, UseLock, CrashAllowed

VARIABLES serverRT,  \* サーバーが有効とみなすリフレッシュトークンの世代
          fileRT,    \* tokens.json に保存されている世代
          held,      \* 各プロセスが読み込んだ世代
          resp,      \* 各プロセスが応答で受け取った世代（0 = 無し）
          lock,      \* 実行ロックの保持者: "none" | p
          pc         \* "idle" -> "read" -> "sent" -> "done" | "rejected" | "crashed"
vars == <<serverRT, fileRT, held, resp, lock, pc>>

MaxRT == Cardinality(Procs)

TypeOK ==
  /\ serverRT \in 0..MaxRT /\ fileRT \in 0..MaxRT
  /\ held \in [Procs -> 0..MaxRT] /\ resp \in [Procs -> 0..MaxRT]
  /\ lock \in Procs \cup {"none"}
  /\ pc \in [Procs -> {"idle", "read", "sent", "done", "rejected", "crashed"}]

Init ==
  /\ serverRT = 0 /\ fileRT = 0
  /\ held = [p \in Procs |-> 0] /\ resp = [p \in Procs |-> 0]
  /\ lock = "none"
  /\ pc = [p \in Procs |-> "idle"]

Release(p) == lock' = IF lock = p THEN "none" ELSE lock

\* storage.get_tokens(): 保存済みトークンを読む（UseLock ならロック取得後に読む）
Read(p) ==
  /\ pc[p] = "idle"
  /\ (UseLock => lock = "none")
  /\ lock' = IF UseLock THEN p ELSE lock
  /\ held' = [held EXCEPT ![p] = fileRT]
  /\ pc' = [pc EXCEPT ![p] = "read"]
  /\ UNCHANGED <<serverRT, fileRT, resp>>

\* client.post(token endpoint): 現行世代なら回転して新世代を返す。古い世代は invalid_grant
Send(p) ==
  /\ pc[p] = "read"
  /\ IF held[p] = serverRT
       THEN /\ serverRT' = serverRT + 1
            /\ resp' = [resp EXCEPT ![p] = serverRT + 1]
            /\ pc' = [pc EXCEPT ![p] = "sent"]
            /\ UNCHANGED lock
       ELSE /\ pc' = [pc EXCEPT ![p] = "rejected"]
            /\ Release(p)
            /\ UNCHANGED <<serverRT, resp>>
  /\ UNCHANGED <<fileRT, held>>

\* storage.set_tokens(): 新しいトークンを保存して終了
Persist(p) ==
  /\ pc[p] = "sent"
  /\ fileRT' = resp[p]
  /\ pc' = [pc EXCEPT ![p] = "done"]
  /\ Release(p)
  /\ UNCHANGED <<serverRT, held, resp>>

\* 応答を受けたが保存前に落ちる（ロックは OS が解放する）
Crash(p) ==
  /\ CrashAllowed /\ pc[p] = "sent"
  /\ pc' = [pc EXCEPT ![p] = "crashed"]
  /\ Release(p)
  /\ UNCHANGED <<serverRT, fileRT, held, resp>>

AtRest == \A p \in Procs : pc[p] \in {"idle", "done", "rejected", "crashed"}

\* 全員終了後の停止（有限シナリオの終端をデッドロック扱いしないための stutter）
Finished == (\A p \in Procs : pc[p] \in {"done", "rejected", "crashed"}) /\ UNCHANGED vars

Next == (\E p \in Procs : Read(p) \/ Send(p) \/ Persist(p) \/ Crash(p)) \/ Finished
Spec == Init /\ [][Next]_vars

\* 有効だったはずのトークンで拒否（invalid_grant → 「認証が失効」の誤通知・実行失敗）されない
NoRejectedRefresh == \A p \in Procs : pc[p] # "rejected"

\* 誰も更新中でないとき、保存済みトークンはサーバーの現行世代と一致する（次回の更新が通る）
FileUsableAtRest == AtRest => fileRT = serverRT
=============================================================================
